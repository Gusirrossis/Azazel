"""Autovacuum de `archivos` a su medida: que el disparo no crezca con la tabla.

Medido en la matriz el 25-09, antes de esta migración (`archivos` sin reloptions; los
globales vienen de `docker-compose.prod.yml`):

    autovacuum_vacuum_scale_factor 0.05 · threshold 50
    autovacuum_analyze_scale_factor 0.02 · threshold 50
    autovacuum_naptime 30s · cost_limit 200 · cost_delay 2ms
    853.382 filas vivas; 11 GB de heap + 8 GB de índices
    UPDATE HOT: 14.097 de 3.276.255 (0,4 %): cada UPDATE toca todos los índices
    reproceso de Matrix: ~26 UPDATE/s (526 en 20 s)

La tabla es una cola y su tamaño no es estable: llegó a 28,8 M de filas y se borraron
28,5 M. Con umbrales en porcentaje, el vacuum espera 42.719 tuplas muertas a 853 k
filas pero 1.440.050 a 28,8 M, y el ANALYZE 576.050 cambios antes de enterarse de
que la distribución de `estado` dio la vuelta. Aquí el disparo lo fija sobre todo una
cantidad de filas, y el porcentaje queda bajo:

    umbral           global a 853 k   aquí a 853 k   global a 28,8 M   aquí a 28,8 M
    vacuum               42.719          27.068         1.440.050          586.000
    analyze              17.118          14.267           576.050          154.000

El ANALYZE baja más porque su coste no crece con la tabla (lee como mucho 30.000
páginas con default_statistics_target 100); el del vacuum sí, porque cada pasada
recorre los índices enteros. Por lo mismo no se tocan los umbrales por INSERT: en esta cola cada
fila insertada se actualiza enseguida (PENDIENTE → PRECALIFICADO → ...), así que el
disparo por tuplas muertas llega antes y un vacuum más por inserción solo sumaría otra
pasada por los índices.

Qué NO arregla: el inflado actual. Lo dejó el DELETE masivo, no el autovacuum, y
el vacuum no devuelve ese espacio al disco (ver deploy/RUNBOOK_compactar_archivos.md).
Mientras la tabla siga inflada, bajar el umbral apenas cambia nada. En el log de
Postgres del 23 al 25-09, cada pasada sobre `archivos` tardó entre 24 min y 6,4 h,
casi todo leyendo ~1 M de páginas de índice a 0,3-4 MB/s. Durante una corrida, cada
pasada arranca en cuanto acaba la anterior (08:54:13 → 08:55:27), así que el límite
es lo que tarda la pasada y no cuándo arranca. Compactada, la tabla quedaría en
~1-1,5 GB entre heap e índices (estimación), dentro de los 8 GB de shared_buffers
(`NORM_PG_SHARED_BUFFERS`, medido en pg_settings).

Lock: `ALTER TABLE ... SET (autovacuum_*)` toma SHARE UPDATE EXCLUSIVE. No bloquea
SELECT, INSERT, UPDATE ni DELETE, pero choca con cualquier otro mantenimiento de la
tabla y ESPERA a que acabe: VACUUM y ANALYZE manuales, CREATE INDEX (también
CONCURRENTLY), REINDEX CONCURRENTLY, VACUUM FULL, CLUSTER. Ese es el riesgo real,
porque esta migración corre en el `command` de `api` antes de `norm api`: reconstruir o
reiniciar `api` durante el plan B del runbook (REINDEX CONCURRENTLY, horas) dejaría la
API sin arrancar, y `/buscar` caído, hasta que el REINDEX terminara. Por eso el
`lock_timeout` de abajo: pasados 30 s falla con «canceling statement due to lock
timeout» en el log de `api`, en vez de quedarse colgada sin rastro. La API sigue sin
arrancar hasta que se reintenta con la tabla libre, pero se ve por qué.

Contra un autovacuum no espera: Postgres lo cancela al pasar deadlock_timeout (1 s en
pg_settings). Tampoco sale gratis: la pasada cancelada se pierde entera, y en esta tabla
inflada eso son de 24 min a 6,4 h de IO ya gastadas (el 25-09 había una en «vacuuming
indexes» desde hacía 48 min). Uno anti-wraparound no se cancela y se esperaría, pero
`age(relfrozenxid)` era 14,8 M contra un límite de 200 M. Por las dos cosas se aplica
DESPUÉS de compactar, cuando ya no hay pasadas largas que tirar (runbook, paso 9).
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLA = "archivos"

#: Disparo = threshold + scale_factor * reltuples.
PARAMETROS: dict[str, int | float] = {
    "autovacuum_vacuum_threshold": 10_000,
    "autovacuum_vacuum_scale_factor": 0.02,
    "autovacuum_analyze_threshold": 10_000,
    "autovacuum_analyze_scale_factor": 0.005,
}

#: Cuánto espera el ALTER a otro mantenimiento de `archivos` antes de fallar.
LOCK_TIMEOUT = "30s"


def _alterar(sql: str) -> None:
    # LOCAL: vale solo para esta transacción. DEFAULT después devuelve el valor por
    # defecto (el del rol o la base si lo tienen; si no, el del servidor) a las
    # migraciones que corran detrás en el mismo `upgrade head`, que comparten
    # transacción (env.py no abre una por migración). Probado en la matriz el 25-09:
    # 30s → DEFAULT → 0.
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(sql)
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def upgrade() -> None:
    ajustes = ", ".join(f"{nombre} = {valor}" for nombre, valor in PARAMETROS.items())
    _alterar(f"ALTER TABLE {_TABLA} SET ({ajustes})")


def downgrade() -> None:
    # RESET, no SET a los valores de antes: antes no había reloptions, y devolverla
    # así hace que vuelva a mandar lo global, sea cual sea en ese momento.
    _alterar(f"ALTER TABLE {_TABLA} RESET ({', '.join(PARAMETROS)})")
