"""Cliente de la cola durable en Postgres — el plano de control del sistema.

Garantías (con tests de integración que las protegen):
- `insertar_pendientes` es idempotente: re-catalogar no duplica (ON CONFLICT DO NOTHING).
- `claim` es atómico: N workers concurrentes jamás reciben la misma fila
  (FOR UPDATE SKIP LOCKED, patrón del diseño §3 de la propuesta).
- El claim usa LEASE, no cambia el estado: una fila con lease vencido vuelve a ser
  reclamable sola (worker muerto → otro la toma; patrón plaso ABANDONED).
- `transicionar` valida contra la máquina de estados de core.modelo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.modelo import Estado, RutaDecision, es_transicion_valida


class TransicionInvalida(Exception):
    """Intento de mover una fila fuera de la máquina de estados."""


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
) -> bool:
    """Mueve una fila de estado validando la máquina.

    Por defecto libera el lease; `conservar_lease=True` lo mantiene (un worker que
    pasa su fila a EN_PROCESO sigue siendo su dueño — y si muere, el lease vencido
    permite recuperarla como huérfana).
    Devuelve False si la fila ya no estaba en `de` (otro proceso ganó — no es error).
    """
    if not es_transicion_valida(de, a):
        raise TransicionInvalida(f"{de.value} → {a.value} no está permitida")
    liberar = "" if conservar_lease else " worker_id = NULL, lease_hasta = NULL,"
    cur = conn.execute(
        f"UPDATE archivos SET estado = %s, motivo = COALESCE(%s, motivo),{liberar}"
        " actualizado_en = now()"
        " WHERE archivo_id = %s AND estado = %s",
        (a.value, motivo, archivo_id, de.value),
    )
    return cur.rowcount == 1


def recuperar_huerfanos(conn: psycopg.Connection[Any]) -> int:
    """Filas EN_PROCESO con lease vencido (worker muerto) vuelven a PRECALIFICADO.

    Patrón plaso (ABANDONED → retry): nada queda atorado para siempre. Se llama al
    arrancar cada worker. Devuelve cuántas se rescataron.
    """
    cur = conn.execute(
        "UPDATE archivos SET estado = 'PRECALIFICADO', worker_id = NULL,"
        " lease_hasta = NULL, intentos = intentos + 1, actualizado_en = now()"
        " WHERE estado = 'EN_PROCESO' AND lease_hasta < clock_timestamp()"
    )
    return cur.rowcount


def registrar_persistencia(
    conn: psycopg.Connection[Any], archivo_id: str, hash_contenido: str
) -> bool:
    """El blob quedó a salvo: guarda su hash y transiciona EN_PROCESO → INDEXADO."""
    cur = conn.execute(
        "UPDATE archivos SET hash_contenido = %s, estado = 'INDEXADO',"
        " worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        " WHERE archivo_id = %s AND estado = 'EN_PROCESO'",
        (hash_contenido, archivo_id),
    )
    return cur.rowcount == 1


def marcar_error(
    conn: psycopg.Connection[Any], archivo_id: str, de: Estado, error_motivo: str
) -> bool:
    """Dead-letter: ERROR con motivo clasificado e intentos incrementados."""
    if not es_transicion_valida(de, Estado.ERROR):
        raise TransicionInvalida(f"{de.value} → ERROR no está permitida")
    cur = conn.execute(
        "UPDATE archivos SET estado = 'ERROR', error_motivo = %s, intentos = intentos + 1,"
        " worker_id = NULL, lease_hasta = NULL, actualizado_en = now()"
        " WHERE archivo_id = %s AND estado = %s",
        (error_motivo, archivo_id, de.value),
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
) -> bool:
    """Maneja un fallo TRANSITORIO (dependencia caída, I/O intermitente).

    Devuelve la fila con `lease_hasta = ahora + backoff` — el lease funciona como
    "no reclamar hasta", así el backoff no necesita scheduler. Agotado el tope de
    intentos → ERROR dead-letter con motivo "agotado:…".
    Devuelve True si quedó en reintento; False si fue a dead-letter.
    """
    if intentos_actuales + 1 >= intentos_max:
        marcar_error(conn, archivo_id, estado_actual, f"agotado:{motivo}"[:300])
        return False
    destino = estado_retorno if estado_retorno is not None else estado_actual
    if destino != estado_actual and not es_transicion_valida(estado_actual, destino):
        raise TransicionInvalida(f"{estado_actual.value} → {destino.value} no está permitida")
    conn.execute(
        "UPDATE archivos SET estado = %s, intentos = intentos + 1, error_motivo = %s,"
        " worker_id = NULL,"
        " lease_hasta = clock_timestamp() + make_interval(secs => %s),"
        " actualizado_en = now()"
        " WHERE archivo_id = %s AND estado = %s",
        (destino.value, motivo[:300], backoff_s, archivo_id, estado_actual.value),
    )
    return True


def renovar_lease(conn: psycopg.Connection[Any], worker_id: str, lease_segundos: int) -> int:
    """Heartbeat: extiende el lease de TODO lo que este worker tiene reclamado.

    Un worker sano procesando un archivo enorme no pierde su trabajo por lease
    vencido; un worker muerto deja de renovar y sus filas se rescatan solas."""
    cur = conn.execute(
        "UPDATE archivos SET lease_hasta = clock_timestamp() + make_interval(secs => %s)"
        " WHERE worker_id = %s AND lease_hasta IS NOT NULL",
        (lease_segundos, worker_id),
    )
    return cur.rowcount


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
