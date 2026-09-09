"""Índices para responder «¿tienes esta base indexada ENTERA?» sin escaneo secuencial.

Un contenedor y sus entradas se unen por `origen_contenedor->>'contenedor_archivo_id'`,
y sobre esa expresión no había índice ninguno. Medido en la luna de Lilith, con 377.000
filas en la tabla:

    LEFT JOIN archivos h ON h.origen_contenedor->>'contenedor_archivo_id' = c.archivo_id
      -> Parallel Seq Scan on archivos c   (Rows Removed by Filter: 75.343)
      Execution Time: 2491,029 ms

2,5 segundos, y crece con la tabla: al subir el tope de lotes por base el corpus pasa de
377.000 a ~950.000 filas, o sea del orden de 7 s. Para algo que se consulta en CADA
búsqueda eso no sirve — el barrido local que se quiere evitar tarda 68 s, y gastar 7 en
preguntar si hace falta se come buena parte del ahorro.

Dos índices PARCIALES, que es la lección de 0011: se paga sólo por las filas que
discriminan, no por la tabla entera.

- `ix_archivos_contenedor_padre` — sólo filas HIJAS (233.157 de 390.011 en la matriz).
  Lleva `estado` en la clave a propósito: el conteo por estado, que es justo lo que
  responde «completa o a medias», sale del índice sin volver a la tabla.
- `ix_archivos_raiz_ruta` — sólo filas RAÍZ. Encontrar el contenedor de una base por su
  ruta sin recorrer todo.

CONCURRENTLY dentro de `autocommit_block` por lo mismo que 0011: un `CREATE INDEX`
normal toma un lock que bloquea escrituras, y estos nodos tienen ingesta corriendo.
CONCURRENTLY no puede ejecutarse dentro de una transacción.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_HIJAS = "ix_archivos_contenedor_padre"
_RAICES = "ix_archivos_raiz_ruta"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_HIJAS} ON archivos"
            " ((origen_contenedor->>'contenedor_archivo_id'), estado)"
            " WHERE origen_contenedor IS NOT NULL"
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_RAICES} ON archivos"
            " (disco_id, ruta) WHERE origen_contenedor IS NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_RAICES}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_HIJAS}")
