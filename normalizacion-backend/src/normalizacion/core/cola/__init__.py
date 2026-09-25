"""Cliente de la cola durable en Postgres — el plano de control del sistema.

Garantías (con tests de integración que las protegen):
- `insertar_pendientes` es idempotente: re-catalogar no duplica (ON CONFLICT DO NOTHING).
- `claim` es atómico: N workers concurrentes jamás reciben la misma fila
  (FOR UPDATE SKIP LOCKED, patrón del diseño §3 de la propuesta).
- El claim usa LEASE, no cambia el estado: una fila con lease vencido vuelve a ser
  reclamable sola (worker muerto → otro la toma; patrón plaso ABANDONED).
- `transicionar` valida contra la máquina de estados de core.modelo.
- Un worker solo escribe filas que siguen siendo SUYAS (`worker_id=`): si su lease
  venció y otro la re-reclamó, su escritura no hace nada en vez de pisar al dueño.
- Ninguna operación de mantenimiento de leases espera por una fila bloqueada
  (SKIP LOCKED): así no puede cerrar un ciclo de bloqueos con otro worker.
- Un deadlock no deshace en silencio el trabajo del lote: `TransaccionCola` rehace,
  tras el rollback, lo que iba sin confirmar.
"""

from __future__ import annotations

import contextlib
import functools
import os
import random
import socket
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.modelo import Estado, RutaDecision, es_transicion_valida
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("cola")


class TransicionInvalida(Exception):
    """Intento de mover una fila fuera de la máquina de estados."""


def identificador_worker(rol: str) -> str:
    """worker_id ÚNICO por proceso: `rol@host:pid`.

    El valor por defecto era la constante "worker-1". En la matriz, /sup.sh lanzó 6
    `norm worker` sin id y los 6 se llamaron igual: el heartbeat de uno renovaba (y
    bloqueaba) las filas que los otros cinco tenían a medio procesar. Esos 6 fueron
    las víctimas de 178 de los 187 deadlocks de la corrida de Matrix.rar, y el ciclo
    con id compartido se reproduce en Postgres 3 de 3 veces. El pid solo no basta:
    dentro de Docker dos contenedores repiten pid; el hostname de un contenedor es su id.
    """
    return f"{rol}@{socket.gethostname()}:{os.getpid()}"


def _solo_si_es_suya(worker_id: str | None) -> tuple[str, tuple[Any, ...]]:
    """Filtro SQL de dueño para las escrituras de un worker (vacío si no se pide)."""
    if worker_id is None:
        return "", ()
    return " AND worker_id = %s", (worker_id,)


# Solo lo que Postgres aborta SIN aplicar nada y se puede repetir tal cual. Ojo: en
# psycopg 3.3 estas NO heredan de `errors.TransactionRollback` (todas cuelgan de
# OperationalError), así que capturar la base no atraparía ninguna. Fuera quedan 40002
# (integridad) y 40003 (no se sabe si el commit se aplicó): repetirlas a ciegas puede
# duplicar. Y un OperationalError genérico (conexión caída) tampoco se reintenta aquí.
_REINTENTABLES = (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure)


def con_reintento[T](
    conn: psycopg.Connection[Any],
    operacion: Callable[[], T],
    *,
    que: str,
    confirmar: bool = True,
    rehacer: Callable[[], object] | None = None,
    intentos: int = 5,
    espera_base_s: float = 0.1,
) -> T:
    """Ejecuta `operacion` (y confirma, salvo `confirmar=False`). Ante un deadlock o un
    fallo de serialización: rollback, espera corta con jitter y otra vez.

    Un único deadlock en renovar_lease mató un worker del pipeline y dejó la corrida 7
    FALLIDA sin mover a frío, verificar ni evaluar la puerta; los 6 extras sobrevivían
    solo porque un bucle `sh` los relanzaba. Agotados los intentos la excepción sube:
    un worker que no puede escribir en la cola no sigue como si nada.

    Postgres aborta la transacción ENTERA de la víctima: lo que iba sin confirmar en
    `conn` antes de `operacion` también se pierde. `rehacer` lo vuelve a aplicar tras
    el rollback y antes de repetir `operacion` (ver `TransaccionCola`).
    """
    intento = 0
    while True:
        intento += 1
        try:
            if intento > 1 and rehacer is not None:
                rehacer()
            resultado = operacion()
            if confirmar:
                conn.commit()
            return resultado
        except _REINTENTABLES as exc:
            conn.rollback()
            if intento >= intentos:
                raise
            espera = espera_base_s * (2 ** (intento - 1)) * (1 + random.random())
            log.warning(
                "cola_reintento_por_bloqueo",
                operacion=que,
                intento=intento,
                error=type(exc).__name__,
                espera_s=round(espera, 3),
            )
            time.sleep(espera)


class TransaccionCola:
    """Escrituras de un worker en la transacción de `conn` que sobreviven a un deadlock.

    Repetir solo la sentencia que falló no bastaba: Postgres ya había deshecho con ella
    todo lo anterior sin confirmar. Con un deadlock en el cierre del lote se perdían los
    pasos a EN_PROCESO de todas sus filas, registrar_persistencia no encontraba ninguna
    y el lote acababa con 0 de 5 procesadas, sus 5 documentos ya en el índice y las 5
    filas retenidas 300 s por un lease vivo; con el deadlock en el tercer transicionar,
    las dos primeras quedaban igual (medido en Postgres 16 inyectando el deadlock). Por
    eso cada escritura sin confirmar se anota y, tras el rollback, se rehacen todas en
    su orden antes de repetir la que falló. Confirmar vacía la lista.

    Si al rehacerla una escritura da otro resultado (la fila dejó de ser del worker
    entre medias), se registra: lo que el worker ya contó con el primero no se deshace.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn
        self._sin_confirmar: list[tuple[str, Callable[[], Any], Any]] = []

    def escribir(
        self,
        que: str,
        operacion: Callable[..., Any],
        *args: Any,
        confirmar: bool = True,
        **kw: Any,
    ) -> Any:
        paso = functools.partial(operacion, self.conn, *args, **kw)
        resultado = con_reintento(
            self.conn, paso, que=que, confirmar=confirmar, rehacer=self._rehacer
        )
        if confirmar:
            self._sin_confirmar.clear()
        else:
            self._sin_confirmar.append((que, paso, resultado))
        return resultado

    def _rehacer(self) -> None:
        for que, paso, antes in self._sin_confirmar:
            ahora = paso()
            if ahora != antes:
                log.warning("cola_rehecho_distinto", operacion=que, antes=antes, ahora=ahora)


@dataclass(frozen=True)
class FilaCatalogo:
    """Fila ligera que produce el walker o T3 (señales T0 viven en estas columnas).

    `origen_contenedor` (path spec estilo plaso) solo existe para entradas internas:
    {"cadena": ["caja.zip", "interna.zip", "dato.csv"], "profundidad": 2,
     "contenedor_archivo_id": "..."}.
    """

    archivo_id: str
    disco_id: str
    ruta: str  # relativa a la raíz del disco; virtual con "!" para entradas internas
    nombre: str
    extension: str | None
    tamano: int
    mtime: datetime
    prioridad: int = 0  # urgencia inicial (contenedores y sus entradas van primero)
    origen_contenedor: dict[str, Any] | None = None


@dataclass(frozen=True)
class FilaReclamada:
    """Fila entregada por `claim` a un trabajador (incluye lo que decidió el filtro)."""

    archivo_id: str
    disco_id: str
    ruta: str
    nombre: str
    extension: str | None
    tamano: int
    mtime: datetime
    estado: Estado
    intentos: int
    origen_contenedor: dict[str, Any] | None = None
    tipo_real: str | None = None
    puntaje: int | None = None
    senales: dict[str, Any] | None = None
    motivo: str | None = None
    version_filtro: str | None = None
    hash_contenido: str | None = None


def upsert_disco(conn: psycopg.Connection[Any], disco_id: str, punto_montaje: str) -> None:
    """Registra (o re-registra) un disco de origen."""
    conn.execute(
        "INSERT INTO discos (disco_id, punto_montaje) VALUES (%s, %s)"
        " ON CONFLICT (disco_id) DO UPDATE"
        " SET punto_montaje = EXCLUDED.punto_montaje, actualizado_en = now()",
        (disco_id, punto_montaje),
    )


def disco_existe(conn: psycopg.Connection[Any], disco_id: str) -> bool:
    """¿Este disco ya está registrado? Decide si su id puede recibir namespace de
    nodo (⚙K16): un disco ya catalogado JAMÁS cambia de id — todos sus `archivo_id`
    dependen de él."""
    return (
        conn.execute("SELECT 1 FROM discos WHERE disco_id = %s", (disco_id,)).fetchone()
        is not None
    )


def insertar_pendientes(conn: psycopg.Connection[Any], filas: list[FilaCatalogo]) -> int:
    """Inserta un lote como PENDIENTE. Idempotente: devuelve cuántas eran NUEVAS."""
    if not filas:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO archivos"
            " (archivo_id, disco_id, ruta, nombre, extension, tamano, mtime,"
            "  prioridad, origen_contenedor)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (archivo_id) DO NOTHING",
            [
                (
                    f.archivo_id,
                    f.disco_id,
                    f.ruta,
                    f.nombre,
                    f.extension,
                    f.tamano,
                    f.mtime,
                    f.prioridad,
                    Jsonb(f.origen_contenedor) if f.origen_contenedor is not None else None,
                )
                for f in filas
            ],
        )
        return cur.rowcount


def actualizar_total_disco(conn: psycopg.Connection[Any], disco_id: str) -> int:
    """Sincroniza discos.total_catalogado y lo devuelve."""
    fila = conn.execute(
        "UPDATE discos SET total_catalogado ="
        " (SELECT COUNT(*) FROM archivos WHERE disco_id = %s), actualizado_en = now()"
        " WHERE disco_id = %s RETURNING total_catalogado",
        (disco_id, disco_id),
    ).fetchone()
    return int(fila[0]) if fila else 0


def claim(
    conn: psycopg.Connection[Any],
    *,
    worker_id: str,
    estado: Estado,
    lote: int,
    lease_segundos: int,
    solo_sin_hash: bool = False,
) -> list[FilaReclamada]:
    """Reclama hasta `lote` filas en `estado` de forma atómica (SKIP LOCKED + lease).

    No cambia el estado: marca worker_id + lease_hasta. Una fila es reclamable si su
    lease es NULL o ya venció — así un worker muerto nunca retiene trabajo para siempre.
    `solo_sin_hash`: filtra a filas aún no persistidas (el mover-frío reclama COLD
    pendientes sin re-reclamar lo ya movido).
    """
    filtro_hash = " AND hash_contenido IS NULL" if solo_sin_hash else ""
    # El RETURNING de un UPDATE no garantiza orden: el CTE re-ordena el lote para
    # que el worker también procese DENTRO del lote por prioridad (txt, 7z, rar…).
    filas = conn.execute(
        f"""
        WITH reclamadas AS (
            UPDATE archivos
               SET worker_id = %s,
                   lease_hasta = clock_timestamp() + make_interval(secs => %s),
                   actualizado_en = now()
             WHERE archivo_id IN (
                   SELECT archivo_id FROM archivos
                    WHERE estado = %s
                      AND (lease_hasta IS NULL OR lease_hasta < clock_timestamp()){filtro_hash}
                    ORDER BY prioridad DESC, archivo_id
                      FOR UPDATE SKIP LOCKED
                    LIMIT %s
             )
            RETURNING archivo_id, disco_id, ruta, nombre, extension, tamano, mtime,
                      estado, intentos, origen_contenedor, tipo_real, puntaje,
                      senales, motivo, version_filtro, hash_contenido, prioridad
        )
        SELECT archivo_id, disco_id, ruta, nombre, extension, tamano, mtime,
               estado, intentos, origen_contenedor,
               tipo_real, puntaje, senales, motivo, version_filtro, hash_contenido
          FROM reclamadas
         ORDER BY prioridad DESC, archivo_id
        """,
        (worker_id, lease_segundos, estado.value, lote),
    ).fetchall()
    return [
        FilaReclamada(
            archivo_id=f[0],
            disco_id=f[1],
            ruta=f[2],
            nombre=f[3],
            extension=f[4],
            tamano=f[5],
            mtime=f[6],
            estado=Estado(f[7]),
            intentos=f[8],
            origen_contenedor=f[9],
            tipo_real=f[10],
            puntaje=f[11],
            senales=f[12],
            motivo=f[13],
            version_filtro=f[14],
            hash_contenido=f[15],
        )
        for f in filas
    ]


def transicionar(
    conn: psycopg.Connection[Any],
    archivo_id: str,
    de: Estado,
    a: Estado,
    *,
    motivo: str | None = None,
    conservar_lease: bool = False,
    worker_id: str | None = None,
) -> bool:
    """Mueve una fila de estado validando la máquina.

    Por defecto libera el lease; `conservar_lease=True` lo mantiene (un worker que
    pasa su fila a EN_PROCESO sigue siendo su dueño — y si muere, el lease vencido
    permite recuperarla como huérfana).
    `worker_id`: solo si la fila sigue siendo de ese worker. Sin esto, un worker cuyo
    lease venció a mitad de lote seguía moviendo filas que otro ya había re-reclamado:
    dos workers sobre las mismas filas, y el heartbeat de uno contra el transicionar
    del otro cierra un ciclo (reproducido en Postgres; ver renovar_lease).
    Devuelve False si la fila ya no estaba en `de` o ya no es suya (otro proceso
    ganó — no es error).
    """
    if not es_transicion_valida(de, a):
        raise TransicionInvalida(f"{de.value} → {a.value} no está permitida")
    liberar = "" if conservar_lease else " worker_id = NULL, lease_hasta = NULL,"
    dueno, params_dueno = _solo_si_es_suya(worker_id)
    cur = conn.execute(
        f"UPDATE archivos SET estado = %s, motivo = COALESCE(%s, motivo),{liberar}"
        " actualizado_en = now()"
        f" WHERE archivo_id = %s AND estado = %s{dueno}",
        (a.value, motivo, archivo_id, de.value, *params_dueno),
    )
    return cur.rowcount == 1


def recuperar_huerfanos(conn: psycopg.Connection[Any]) -> int:
    """Filas EN_PROCESO con lease vencido (worker muerto) vuelven a PRECALIFICADO.

    Patrón plaso (ABANDONED → retry): nada queda atorado para siempre. Se llama al
    arrancar cada worker. Devuelve cuántas se rescataron.

    SKIP LOCKED: una fila que alguien tiene bloqueada la está tocando un proceso VIVO,
    así que no es huérfana. Esperarla, además, cerraba ciclos con los heartbeats de
    otros workers (2 de los 187 deadlocks de Matrix fueron recuperar_huerfanos contra
    renovar_lease) y dejaba al worker que arranca parado tras un archivo ajeno.
    """
    cur = conn.execute(
        "UPDATE archivos SET estado = 'PRECALIFICADO', worker_id = NULL,"
        " lease_hasta = NULL, intentos = intentos + 1, actualizado_en = now()"
        " WHERE archivo_id IN ("
        "   SELECT archivo_id FROM archivos"
        "    WHERE estado = 'EN_PROCESO' AND lease_hasta < clock_timestamp()"
        "    ORDER BY archivo_id"
        "      FOR UPDATE SKIP LOCKED"
        " )"
    )
    return cur.rowcount


def registrar_persistencia(
    conn: psycopg.Connection[Any],
    archivo_id: str,
    hash_contenido: str,
    *,
    worker_id: str | None = None,
) -> bool:
    """El blob quedó a salvo: guarda su hash y transiciona EN_PROCESO → INDEXADO.
    `worker_id`: solo si la fila sigue siendo de ese worker (ver `transicionar`)."""
    dueno, params_dueno = _solo_si_es_suya(worker_id)
    cur = conn.execute(
        "UPDATE archivos SET hash_contenido = %s, estado = 'INDEXADO',"
        " worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        f" WHERE archivo_id = %s AND estado = 'EN_PROCESO'{dueno}",
        (hash_contenido, archivo_id, *params_dueno),
    )
    return cur.rowcount == 1


def marcar_error(
    conn: psycopg.Connection[Any],
    archivo_id: str,
    de: Estado,
    error_motivo: str,
    *,
    worker_id: str | None = None,
) -> bool:
    """Dead-letter: ERROR con motivo clasificado e intentos incrementados.
    `worker_id`: solo si la fila sigue siendo de ese worker (ver `transicionar`)."""
    if not es_transicion_valida(de, Estado.ERROR):
        raise TransicionInvalida(f"{de.value} → ERROR no está permitida")
    dueno, params_dueno = _solo_si_es_suya(worker_id)
    cur = conn.execute(
        "UPDATE archivos SET estado = 'ERROR', error_motivo = %s, intentos = intentos + 1,"
        " worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        f" WHERE archivo_id = %s AND estado = %s{dueno}",
        (error_motivo, archivo_id, de.value, *params_dueno),
    )
    return cur.rowcount == 1


def montajes(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """disco_id → punto de montaje (para resolver rutas relativas de la cola)."""
    filas = conn.execute(
        "SELECT disco_id, punto_montaje FROM discos WHERE punto_montaje IS NOT NULL"
    ).fetchall()
    return {f[0]: f[1] for f in filas}


def guardar_precalificacion(
    conn: psycopg.Connection[Any],
    archivo_id: str,
    *,
    puntaje: int,
    ruta: RutaDecision,
    tipo_real: str | None,
    senales: dict[str, Any],
    motivo: str,
    version_filtro: str,
    prioridad: int | None = None,
) -> bool:
    """Persiste la decisión del filtro de forma atómica y auditable.

    Composición de transiciones válidas en un solo UPDATE:
    PENDIENTE→PRECALIFICADO (HOT) o PENDIENTE→PRECALIFICADO→COLD (COLD).
    `prioridad`: si no se pasa, `prioridad = puntaje` (lo más útil primero); el
    precalificador la pasa explícita para que el orden por extensión (K3
    `prioridad_extensiones`, valores >100) sobreviva la transición. Libera el lease.
    Nota: reprocesar-errores y rescore-frio conservan la prioridad previa de la fila.
    """
    estado_final = Estado.PRECALIFICADO if ruta is RutaDecision.HOT else Estado.COLD
    cur = conn.execute(
        "UPDATE archivos SET estado = %s, puntaje = %s, ruta_decision = %s, tipo_real = %s,"
        " senales = %s, motivo = %s, version_filtro = %s, prioridad = %s,"
        " worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        " WHERE archivo_id = %s AND estado = 'PENDIENTE'",
        (
            estado_final.value,
            puntaje,
            ruta.value,
            tipo_real,
            Jsonb(senales),
            motivo,
            version_filtro,
            prioridad if prioridad is not None else puntaje,
            archivo_id,
        ),
    )
    return cur.rowcount == 1


def fallo_transitorio(
    conn: psycopg.Connection[Any],
    archivo_id: str,
    *,
    estado_actual: Estado,
    estado_retorno: Estado | None,
    motivo: str,
    intentos_actuales: int,
    intentos_max: int,
    backoff_s: float,
    worker_id: str | None = None,
) -> bool | None:
    """Maneja un fallo TRANSITORIO (dependencia caída, I/O intermitente).

    Devuelve la fila con `lease_hasta = ahora + backoff` — el lease funciona como
    "no reclamar hasta", así el backoff no necesita scheduler. Agotado el tope de
    intentos → ERROR dead-letter con motivo "agotado:…".
    Devuelve True si quedó en reintento; False si fue a dead-letter.
    `worker_id`: solo si la fila sigue siendo de ese worker (ver `transicionar`), y
    entonces None si ya no lo era: no se tocó, y no es ni reintento ni error. Antes
    devolvía True igual y el worker contaba un transitorio sobre una fila ajena.
    """
    if intentos_actuales + 1 >= intentos_max:
        marcada = marcar_error(
            conn, archivo_id, estado_actual, f"agotado:{motivo}"[:300], worker_id=worker_id
        )
        return None if worker_id is not None and not marcada else False
    destino = estado_retorno if estado_retorno is not None else estado_actual
    if destino != estado_actual and not es_transicion_valida(estado_actual, destino):
        raise TransicionInvalida(f"{estado_actual.value} → {destino.value} no está permitida")
    dueno, params_dueno = _solo_si_es_suya(worker_id)
    cur = conn.execute(
        "UPDATE archivos SET estado = %s, intentos = intentos + 1, error_motivo = %s,"
        " worker_id = NULL,"
        " lease_hasta = clock_timestamp() + make_interval(secs => %s),"
        " actualizado_en = now()"
        f" WHERE archivo_id = %s AND estado = %s{dueno}",
        (destino.value, motivo[:300], backoff_s, archivo_id, estado_actual.value, *params_dueno),
    )
    return None if worker_id is not None and cur.rowcount != 1 else True


def renovar_lease(
    conn: psycopg.Connection[Any],
    worker_id: str,
    lease_segundos: int,
    *,
    archivo_ids: Sequence[str] | None = None,
) -> int:
    """Heartbeat: extiende el lease de las filas del lote EN CURSO (`archivo_ids`) que
    siguen siendo de este worker; sin `archivo_ids`, de todas las suyas. Un worker sano
    procesando un archivo enorme no pierde su trabajo por lease vencido; un worker
    muerto deja de renovar y sus filas se rescatan solas.

    Antes era `UPDATE … WHERE worker_id = %s` sin más: bloqueaba TODAS las filas con
    ese worker_id, en el orden en que las encontrara el plan, y ESPERABA a cada una.
    El ciclo medido en la matriz (187 deadlocks en 30 h; 279 de las sentencias
    implicadas eran este UPDATE):
      · worker_id compartido ('worker-1' en los 6 extras): A y B tienen sin confirmar
        el transicionar de su fila en curso. El heartbeat de A bloquea filas del lote
        de B que B aún no tocó y se pone a esperar la que B tiene en curso; B pasa a
        su siguiente fila —ya bloqueada por A— o lanza su propio heartbeat, que espera
        la de A. Ciclo: renovar contra renovar, o renovar contra transicionar.
      · ids únicos (pipeline-wN): Y se atasca en un archivo largo, su lease vence y X
        re-reclama el resto del lote; Y vuelve y transiciona una fila que ya es de X
        (transicionar no miraba el dueño). El heartbeat de X espera esa fila mientras
        Y espera otra que X tiene bloqueada. Es el par del deadlock que mató a
        pipeline-w3 y la corrida 7 (renovar_lease contra transicionar, entre dos
        workers del mismo pipeline); que fuera ESTE camino no se pudo cruzar con el
        pid porque log_lock_waits estaba apagado, pero el ciclo se reproduce en
        Postgres 3 de 3 veces con el código anterior.
    Ahora: en orden de archivo_id y con SKIP LOCKED — un UPDATE que no espera ninguna
    fila no puede ser parte de un ciclo — y, como hace el worker, acotado a su lote
    (`archivo_id = ANY`). Sobre 853k filas con la mezcla de estados de producción el
    planificador lo resuelve por `ix_archivos_lease`, también en el plan genérico, con
    coste ~17. Una fila que otro tiene bloqueada no se renueva en este latido; si es por
    un re-claim ya no es de este worker, y si no, la renueva el siguiente.
    """
    if archivo_ids is not None and not archivo_ids:
        return 0
    del_lote, params_lote = (
        (" archivo_id = ANY(%s) AND", (sorted(archivo_ids),)) if archivo_ids else ("", ())
    )
    cur = conn.execute(
        "UPDATE archivos SET lease_hasta = clock_timestamp() + make_interval(secs => %s)"
        " WHERE archivo_id IN ("
        "   SELECT archivo_id FROM archivos"
        f"   WHERE{del_lote} worker_id = %s AND lease_hasta IS NOT NULL"
        "    ORDER BY archivo_id"
        "      FOR UPDATE SKIP LOCKED"
        " )",
        (lease_segundos, *params_lote, worker_id),
    )
    return cur.rowcount


def liberar_sin_tocar(
    conn: psycopg.Connection[Any],
    worker_id: str,
    archivo_ids: Sequence[str],
    *,
    estado: Estado,
) -> list[str]:
    """Devuelve a la cola, sin esperar a nadie, las filas reclamadas por `worker_id` que
    siguen en `estado` (el worker aún no las empezó). Es seguro aunque el worker vuelva:
    sus escrituras exigen dueño, así que se las salta. Devuelve las liberadas."""
    if not archivo_ids:
        return []
    filas = conn.execute(
        "UPDATE archivos SET worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        " WHERE archivo_id IN ("
        "   SELECT archivo_id FROM archivos"
        "    WHERE archivo_id = ANY(%s) AND worker_id = %s AND estado = %s"
        "    ORDER BY archivo_id"
        "      FOR UPDATE SKIP LOCKED"
        " )"
        " RETURNING archivo_id",
        (sorted(archivo_ids), worker_id, estado.value),
    ).fetchall()
    return sorted(f[0] for f in filas)


#: Lo más largo medido para UN archivo legítimo es la extracción de Matrix.rar a la
#: caché: 23 min 36 s (del nacimiento de su directorio en la caché, 06:35:23, al
#: marcador de completo, 06:58:59, el 2026-09-23). Esa extracción la hizo unar, que dejó
#: en 0 B la entrada de 21,4 GB: con unrar escribe 53,5 GB en vez de ~32, y a igual ritmo
#: serían unos 40 min (deducido, sin medir). Una hora sigue por encima: pasada, el archivo
#: se trata como colgado. Soltar antes el resto del lote no ahorra la extracción, porque
#: quien tome filas del mismo contenedor espera en su candado
#: (`contenedores._candado_extraccion`), y marcaría `lote_retenido` en extracciones
#: legítimas.
UMBRAL_LOTE_RETENIDO_S = 3600.0


class Latido:
    """Heartbeat del lote en curso desde un HILO con su propia conexión (autocommit).

    El latido iba entre archivo y archivo, así que un solo archivo más largo que el
    lease (300 s) lo dejaba vencer para el resto del lote. Y los hay: la primera
    entrada que se pide de un RAR o 7z grande extrae el archivo COMPLETO a la caché
    dentro de `_abrir_fuente`, y quien pida otra entrada del mismo contenedor espera ahí
    a que termine: Matrix.rar tardó 23 min 36 s (ver UMBRAL_LOTE_RETENIDO_S). Con el
    lease vencido otro worker re-reclama el resto del lote y empieza el solapamiento que
    cierra ciclos (ver renovar_lease). Este hilo renueva cada `lease/3` pase lo que pase
    en el hilo principal.

    Conexión propia a propósito: la del worker está en medio de su transacción y la
    de control la usa el hilo principal. Un fallo del latido NUNCA mata al worker: se
    registra, se reconecta en el siguiente y, en el peor caso, el lease vence como
    antes.

    Un proceso vivo pero COLGADO (una lectura en estado D, un extractor sin timeout)
    seguiría renovando su lote para siempre sin dejar rastro: medido con lease de 3 s y
    un archivo parado 12 s, otro worker no pudo reclamar ni una fila en todo el cuelgue.
    Por eso el hilo principal avisa con `empezar` de cada archivo que abre, y si uno
    pasa de `umbral_retenido_s` el latido registra `lote_retenido` (y lo repite cada
    umbral) y devuelve a la cola las filas del lote que aún no se tocaron. La fila en
    curso sigue retenida mientras el proceso viva: la tiene bloqueada su transacción.
    """

    def __init__(
        self,
        conectar: Callable[[], psycopg.Connection[Any]],
        worker_id: str,
        lease_segundos: int,
        *,
        intervalo_s: float | None = None,
        umbral_retenido_s: float = UMBRAL_LOTE_RETENIDO_S,
        estado: Estado = Estado.PRECALIFICADO,
    ) -> None:
        self._conectar = conectar
        self._worker_id = worker_id
        self._lease = lease_segundos
        self._intervalo = intervalo_s if intervalo_s is not None else lease_segundos / 3
        self._umbral_retenido = umbral_retenido_s
        self._estado = estado
        self._candado = threading.Lock()
        self._ids: tuple[str, ...] = ()
        self._sin_tocar: set[str] = set()
        self._en_curso: str | None = None
        self._desde = 0.0
        self._avisar_en = 0.0
        self._conn: psycopg.Connection[Any] | None = None
        self._parar = threading.Event()
        self._hilo = threading.Thread(target=self._bucle, name=f"latido-{worker_id}", daemon=True)

    def vigilar(self, archivo_ids: Iterable[str]) -> None:
        """Fija el lote cuyo lease hay que mantener (vacío = ninguno)."""
        ids = tuple(sorted(archivo_ids))
        with self._candado:
            self._ids = ids
            self._sin_tocar = set(ids)
            self._en_curso = None

    def empezar(self, archivo_id: str) -> None:
        """El hilo principal entra en `archivo_id`: deja de estar sin tocar y su reloj
        empieza a contar."""
        ahora = time.monotonic()
        with self._candado:
            self._sin_tocar.discard(archivo_id)
            self._en_curso = archivo_id
            self._desde = ahora
            self._avisar_en = ahora + self._umbral_retenido

    def latir(self) -> int:
        if not self._ids:
            return 0
        try:
            if self._conn is None or self._conn.closed:
                self._conn = self._conectar()
            self._soltar_si_retenido(self._conn)
            renovadas = renovar_lease(
                self._conn, self._worker_id, self._lease, archivo_ids=self._ids
            )
            self._conn.commit()  # no-op en autocommit; necesario si no lo es
            return renovadas
        except Exception as exc:
            log.warning("latido_fallido", worker=self._worker_id, error=str(exc)[:200])
            self._cerrar()
            return 0

    def _soltar_si_retenido(self, conn: psycopg.Connection[Any]) -> None:
        ahora = time.monotonic()
        with self._candado:
            if self._en_curso is None or ahora < self._avisar_en:
                return
            en_curso, desde, sin_tocar = self._en_curso, self._desde, sorted(self._sin_tocar)
        liberadas = set(liberar_sin_tocar(conn, self._worker_id, sin_tocar, estado=self._estado))
        conn.commit()
        with self._candado:
            self._sin_tocar -= liberadas
            self._ids = tuple(i for i in self._ids if i not in liberadas)
            if self._en_curso == en_curso:
                self._avisar_en = ahora + self._umbral_retenido
        log.warning(
            "lote_retenido",
            worker=self._worker_id,
            archivo_id=en_curso,
            segundos=round(ahora - desde),
            filas_liberadas=len(liberadas),
            filas_sin_tocar=len(sin_tocar) - len(liberadas),
        )

    def _bucle(self) -> None:
        try:
            while not self._parar.wait(self._intervalo):
                self.latir()
        finally:
            self._cerrar()

    def _cerrar(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    def __enter__(self) -> Latido:
        self._hilo.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._ids = ()
        self._parar.set()
        self._hilo.join(timeout=30)


def registrar_movimiento_frio(
    conn: psycopg.Connection[Any], archivo_id: str, hash_contenido: str
) -> bool:
    """El blob COLD quedó a salvo en el almacén frío. La fila SIGUE en COLD
    (reversible: re-puntuable); solo registra el hash y libera el lease."""
    cur = conn.execute(
        "UPDATE archivos SET hash_contenido = %s, worker_id = NULL, lease_hasta = NULL,"
        " actualizado_en = now() WHERE archivo_id = %s AND estado = 'COLD'",
        (hash_contenido, archivo_id),
    )
    return cur.rowcount == 1


# ------------------------------------------------------------------ control operativo


def fijar_pausa(conn: psycopg.Connection[Any], pausado: bool) -> None:
    """`norm pausar/reanudar`: bandera global que TODOS los loops respetan."""
    conn.execute(
        "INSERT INTO control (clave, valor) VALUES ('pausado', %s)"
        " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
        ("true" if pausado else "false",),
    )


def sistema_pausado(conn: psycopg.Connection[Any]) -> bool:
    fila = conn.execute("SELECT valor FROM control WHERE clave = 'pausado'").fetchone()
    return bool(fila and fila[0] == "true")


def reprocesar_errores(
    conn: psycopg.Connection[Any], motivo_como: str | None = None
) -> dict[str, int]:
    """Dead-letter → de vuelta a su etapa: PENDIENTE si nunca se puntuó, COLD si su
    ruta era fría, PRECALIFICADO si iba por el camino HOT. Resetea intentos."""
    filtro = " AND error_motivo LIKE %s" if motivo_como else ""
    parametros: tuple[Any, ...] = (motivo_como,) if motivo_como else ()
    filas = conn.execute(
        f"""
        UPDATE archivos SET
            estado = CASE
                WHEN puntaje IS NULL THEN 'PENDIENTE'
                WHEN ruta_decision = 'COLD' THEN 'COLD'
                ELSE 'PRECALIFICADO'
            END,
            intentos = 0, worker_id = NULL, lease_hasta = NULL, actualizado_en = now()
        WHERE estado = 'ERROR'{filtro}
        RETURNING estado
        """,
        parametros,
    ).fetchall()
    destinos: dict[str, int] = {}
    for (estado,) in filas:
        destinos[estado] = destinos.get(estado, 0) + 1
    return destinos


def rescore_frio(conn: psycopg.Connection[Any], disco_id: str | None = None) -> int:
    """Re-puntuar el frío con el filtro vN vigente: COLD → PENDIENTE (reversibilidad
    del diseño — lo rescatado se promueve a HOT al pasar de nuevo por el filtro)."""
    filtro = " AND disco_id = %s" if disco_id else ""
    parametros: tuple[Any, ...] = (disco_id,) if disco_id else ()
    cur = conn.execute(
        "UPDATE archivos SET estado = 'PENDIENTE', intentos = 0,"
        " worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        f" WHERE estado = 'COLD'{filtro}",
        parametros,
    )
    return cur.rowcount


# Contenedores PRESERVADOS íntegros sin explorar: viven en HOT (no en COLD), así
# que rescore_frio NO los toca. Cuando se instala la herramienta que faltaba
# (p. ej. `unar` para RAR) o se suben los guards K4, este es el mecanismo para
# remandarlos al embudo.
MOTIVOS_REEXPLORABLES = (
    "formato_no_soportado",  # RAR sin unar, imagen de disco, tar exótico…
    "contenedor_sin_explorar",
    "contenedor_corrupto",  # corrupto o con contraseña (re-intentar no daña)
    "profundidad_maxima",  # se explora si subió el guard de anidación
)


def reexplorar_preservados(
    conn: psycopg.Connection[Any], disco_id: str | None = None
) -> int:
    """Devuelve a PENDIENTE los contenedores preservados sin explorar para
    re-precalificarlos con las herramientas/guards vigentes.

    A diferencia de rescore_frio (COLD→PENDIENTE), estos están en HOT
    (PRECALIFICADO/INDEXADO/HECHO) con su motivo de preservación — por eso
    necesitan su propio camino. Limpia la precalificación previa; el blob ya
    guardado se conserva (el dedup del worker evita recopiarlo) y al re-explorar
    sus piezas internas se encolan como filas nuevas."""
    filtro = " AND disco_id = %s" if disco_id else ""
    motivos = list(MOTIVOS_REEXPLORABLES)
    parametros: tuple[Any, ...] = (motivos, disco_id) if disco_id else (motivos,)
    cur = conn.execute(
        "UPDATE archivos SET estado = 'PENDIENTE', puntaje = NULL,"
        " ruta_decision = NULL, motivo = NULL, version_filtro = NULL,"
        " senales = '{}'::jsonb, intentos = 0, worker_id = NULL,"
        " lease_hasta = NULL, actualizado_en = now()"
        f" WHERE motivo = ANY(%s){filtro}",
        parametros,
    )
    return cur.rowcount


def conteos_por_estado(conn: psycopg.Connection[Any]) -> list[tuple[str, str, int]]:
    """(disco_id, estado, cuenta) — para `norm estado` y métricas."""
    filas = conn.execute(
        "SELECT disco_id, estado, COUNT(*) FROM archivos"
        " GROUP BY disco_id, estado ORDER BY disco_id, estado"
    ).fetchall()
    return [(f[0], f[1], int(f[2])) for f in filas]
