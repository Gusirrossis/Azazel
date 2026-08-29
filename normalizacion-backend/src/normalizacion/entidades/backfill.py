"""Backfill de entidades desde el ÍNDICE ya existente (E4, primer paso).

Los datos que ya se indexaron en producción (Mac) todavía no han pasado por la
resolución de entidades. Este módulo recorre TODO el índice de OpenSearch, detecta
documentos que son PERSONA por traer una CURP/RFC válida en su texto, y los resuelve
con el MISMO motor idempotente de E1-E3 (anclas + fusión, sin duplicar).

Diseño:
  · Criterio "doc = persona": trae al menos una CURP o RFC VÁLIDA (regex liberal para
    encontrar candidatos + validador estricto con dígito verificador para filtrar).
  · Anclaje seguro en docs con varias personas: cada CURP válida es su propia entidad;
    el RFC solo ancla si el doc NO trae CURP (evita duplicar a la misma persona). Si el
    doc trae EXACTAMENTE una CURP y un RFC, se asume misma persona y el RFC enriquece.
  · NO se extraen nombre/email/teléfono del texto libre: asociarlos a la persona
    correcta requiere NER (E8). El backfill solo fija el ANCLA y lo que de ella se
    deriva (CURP → sexo, fecha y estado de nacimiento). Las fichas se enriquecen luego.
  · Incremental y reanudable: escaneo estable por `archivo_id` con search_after; el
    cursor de avance se guarda en la tabla `control`. Re-ejecutar es seguro (idempotente).

Limitación honesta: un escaneo por `archivo_id` (hash) completa UNA pasada sobre lo ya
indexado. Para capturar docs NUEVOS de forma continua se necesita un timestamp de
indexado o enganchar la resolución al pipeline de ingesta (el resto de E4).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from normalizacion.core import recursos
from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

from . import anclas
from .anclas import Ancla
from .modelo import calcular_entidad_id
from .pipeline import _upsert, construir_entidad
from .receta import obtener_receta

log = obtener_logger("entidades.backfill")

_CURSOR_CLAVE = "backfill_entidades_cursor"
_ESTADO_CLAVE = "backfill_entidades_estado"  # progreso/último resumen para la UI
_LOCK_ID = 0x42_4143_4B46  # advisory lock para serializar el backfill (un proceso a la vez)
_VERSION_RES = "backfill-anclas-v1"
# Solo el ancla: asignacion fila→campo mínima (nombre/email/tel del texto = E8).
_ASIGNACION = {"curp": "curp", "rfc": "rfc"}
_FUENTES = ("texto_indexable", "campos_extraidos", "nombre", "ruta_original")


@dataclass
class ResumenBackfill:
    docs: int = 0
    con_persona: int = 0
    sin_persona: int = 0
    anclas: int = 0
    entidades_nuevas: int = 0
    entidades_fusionadas: int = 0
    errores: int = 0
    cursor: str | None = None

    def como_dict(self) -> dict[str, Any]:
        return {
            "docs": self.docs, "con_persona": self.con_persona,
            "sin_persona": self.sin_persona, "anclas": self.anclas,
            "entidades_nuevas": self.entidades_nuevas,
            "entidades_fusionadas": self.entidades_fusionadas,
            "errores": self.errores, "cursor": self.cursor,
        }


def _aplanar(v: Any) -> Any:
    """Genera los escalares str/numéricos de un valor anidado (dict/list), recursivo."""
    if isinstance(v, bool):
        return  # los bool no aportan anclas, y "True"/"False" sería ruido
    if isinstance(v, (str, int, float)):
        yield str(v)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _aplanar(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _aplanar(x)


def _anclas_de_doc(doc: dict[str, Any]) -> list[Ancla]:
    """Anclas válidas del documento, únicas y en orden de aparición.

    MEMORIA (K15): escanea campo por campo en vez de armar un único texto gigante con
    `" ".join(...)`. Para un `texto_indexable` de ~100 KB ese patrón mantenía varias
    copias del texto vivas a la vez; por cientos de docs por lote eso disparaba la RAM.
    Aquí solo vive un campo cada vez.
    """
    encontradas: list[Ancla] = []
    vistas: set[tuple[str, str]] = set()
    for clave in _FUENTES:
        for escalar in _aplanar(doc.get(clave)):
            for a in anclas.buscar_en_texto(escalar):
                if (a.tipo, a.valor) not in vistas:
                    vistas.add((a.tipo, a.valor))
                    encontradas.append(a)
    return encontradas


def personas_de_doc(doc: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Extrae las filas-persona (anclas) de un documento indexado + su procedencia.

    La regla de agrupación vive en `entidades.anclas`, compartida con el worker: la
    resolución en vivo y el backfill del histórico tienen que decidir IGUAL, o el
    mismo documento produciría entidades distintas según por dónde entró.
    """
    filas = anclas.filas_persona(_anclas_de_doc(doc))
    procedencia = {
        "fuente": "backfill_indice",
        "archivo_id": doc.get("archivo_id"),
        "ruta": doc.get("ruta_original"),
        "disco_id": doc.get("disco_id"),
    }
    return filas, procedencia


def _leer_cursor(conn: psycopg.Connection[Any]) -> str | None:
    f = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CURSOR_CLAVE,)).fetchone()
    return f[0] if f else None


def _guardar_cursor(conn: psycopg.Connection[Any], cursor: str) -> None:
    conn.execute(
        "INSERT INTO control (clave, valor) VALUES (%s, %s)"
        " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
        (_CURSOR_CLAVE, cursor),
    )


def _buscar_lote(cliente: Any, alias: str, cursor: str | None, lote: int) -> list[dict[str, Any]]:
    cuerpo: dict[str, Any] = {
        "size": lote,
        "sort": [{"archivo_id": "asc"}],
        "query": {"match_all": {}},
        "_source": [*_FUENTES, "archivo_id", "disco_id"],
    }
    if cursor:
        cuerpo["search_after"] = [cursor]
    return cliente.search(index=alias, body=cuerpo)["hits"]["hits"]


def _borrar_cursor(conn: psycopg.Connection[Any]) -> None:
    conn.execute("DELETE FROM control WHERE clave = %s", (_CURSOR_CLAVE,))


def _indice_existe(cliente: Any, alias: str) -> bool:
    try:
        return bool(cliente.indices.exists(index=alias))
    except Exception:
        return False


def _resolver_fila(
    conn: psycopg.Connection[Any], receta: Any, fila: dict[str, str],
    procedencia: dict[str, Any], resumen: ResumenBackfill,
) -> None:
    ent = construir_entidad(receta, _ASIGNACION, fila)
    if ent is None:
        return
    eid = calcular_entidad_id(receta.tipo, ent["ancla_tipo"], ent["ancla_valor"])
    resultado = _upsert(
        conn, eid, receta.tipo, ent["ancla_tipo"], ent["ancla_valor"],
        ent["campos"], receta.version, _VERSION_RES, procedencia,
    )
    resumen.anclas += 1
    if resultado == "nueva":
        resumen.entidades_nuevas += 1
    else:
        resumen.entidades_fusionadas += 1


def backfill_desde_indice(
    config: Config, *, lote: int = 500, max_docs: int | None = None,
    reiniciar: bool = False, on_progress: Callable[[ResumenBackfill], None] | None = None,
) -> ResumenBackfill:
    """Recorre el índice y resuelve entidades de los docs que traen CURP/RFC.

    `max_docs` acota una corrida (para un botón "procesar siguiente lote"); sin él,
    drena hasta el final. `reiniciar` borra el cursor y empieza de cero. El cursor se
    persiste por lote y cada fila va en su propio savepoint, así que una corrida
    interrumpida (o una fila envenenada) no tumba el lote y re-ejecutar no duplica.
    Un advisory lock serializa: dos backfills a la vez no se pisan el cursor.

    Limitación (una PASADA): el orden por archivo_id (hash) completa el barrido de lo
    YA indexado; para capturar docs nuevos re-corre con `reiniciar=True` (rescan
    completo, idempotente). El modo continuo es el enganche al pipeline (resto de E4)."""
    from normalizacion.core.indexador.opensearch import crear_cliente

    cliente = crear_cliente(config)
    receta = obtener_receta("persona")
    resumen = ResumenBackfill()
    lote = max(1, min(lote, 2000))

    with psycopg.connect(config.postgres_dsn) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,)).fetchone()[0]:
            conn.rollback()
            raise RuntimeError("ya hay un backfill de entidades en curso (otro proceso lo tiene)")
        try:
            if reiniciar:
                _borrar_cursor(conn)
            cursor = _leer_cursor(conn)
            conn.commit()  # cierra el tx del lock/cursor; cada lote abre el suyo
            if not _indice_existe(cliente, config.indice_alias):
                log.warning("backfill_sin_indice", alias=config.indice_alias)
                return resumen
            while True:
                # Throttle adaptativo (K15): el backfill recorre el índice trayendo el
                # texto completo de cada doc; bajo presión de memoria espera a que la RAM
                # se recupere antes de pedir el siguiente lote (no satura junto a la ingesta).
                recursos.esperar_si_presion(config, etiqueta="backfill")
                hits = _buscar_lote(cliente, config.indice_alias, cursor, lote)
                if not hits:
                    break
                with conn.transaction():  # un tx por lote (cursor + filas juntos)
                    for hit in hits:
                        doc = hit["_source"]
                        resumen.docs += 1
                        cursor = (hit.get("sort") or [None])[0] or doc.get("archivo_id")
                        filas, procedencia = personas_de_doc(doc)
                        if not filas:
                            resumen.sin_persona += 1
                            continue
                        resumen.con_persona += 1
                        for fila in filas:
                            try:
                                with conn.transaction():  # savepoint por fila
                                    _resolver_fila(conn, receta, fila, procedencia, resumen)
                            except Exception as exc:  # fila envenenada → dead-letter, sigue
                                resumen.errores += 1
                                log.warning("backfill_fila_fallida", error=str(exc)[:200])
                    if cursor:
                        _guardar_cursor(conn, cursor)
                resumen.cursor = cursor
                if on_progress:
                    on_progress(resumen)
                if max_docs is not None and resumen.docs >= max_docs:
                    break
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
            conn.commit()
    log.info("backfill_completo", **resumen.como_dict())
    return resumen


# ---------------------------------------------- ejecución en SEGUNDO PLANO (UI)
# El backfill recorre el índice y resuelve entidades: trabajo pesado que NO debe
# correr dentro del hilo de la petición HTTP —bloquearía y engordaría el proceso de
# la API, justo lo que tumba el panel—. Se lanza en un hilo daemon gobernado por K15
# y el front consulta el avance por `estado_backfill` (patrón del pipeline).

_HILO: threading.Thread | None = None
_PARAR = threading.Event()


def _guardar_estado(config: Config, *, ejecutando: bool, resumen: ResumenBackfill,
                    error: str | None = None) -> None:
    """Persiste el avance/último resumen (best-effort: si la BD parpadea, no rompe)."""
    valor = {
        "ts": datetime.now(UTC).isoformat(),
        "ejecutando": ejecutando,
        "error": error,
        **resumen.como_dict(),
    }
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO control (clave, valor) VALUES (%s, %s)"
                " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
                (_ESTADO_CLAVE, json.dumps(valor, ensure_ascii=False)),
            )
            conn.commit()
    except Exception:
        pass


def estado_backfill(config: Config) -> dict[str, Any]:
    """Estado del backfill para la UI: si está corriendo y el último resumen."""
    corriendo = _HILO is not None and _HILO.is_alive()
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            fila = conn.execute(
                "SELECT valor FROM control WHERE clave = %s", (_ESTADO_CLAVE,)
            ).fetchone()
    except psycopg.Error:
        return {"ejecutando": corriendo, "disponible": False, "ultimo": None}
    ultimo = json.loads(fila[0]) if fila else None
    return {"ejecutando": corriendo, "disponible": True, "ultimo": ultimo}


def lanzar_en_fondo(
    config: Config, *, lote: int = 500, max_docs: int | None = None, reiniciar: bool = False,
) -> dict[str, Any]:
    """Arranca el backfill en un hilo daemon (uno a la vez) y vuelve de inmediato.

    Devuelve {lanzado, motivo}. No arranca si ya hay uno corriendo, o si el
    gobernador (K15) ve que la RAM no da para una pasada (mejor posponer que tumbar
    el panel: la UI reintenta)."""
    global _HILO
    if _HILO is not None and _HILO.is_alive():
        return {"lanzado": False, "motivo": "ya hay un backfill en curso"}
    if not recursos.cabe_tarea(config):
        return {"lanzado": False, "motivo": "memoria insuficiente ahora mismo; reintenta en un momento"}

    def _correr() -> None:
        resumen_inicial = ResumenBackfill()
        _guardar_estado(config, ejecutando=True, resumen=resumen_inicial)
        try:
            r = backfill_desde_indice(
                config, lote=lote, max_docs=max_docs, reiniciar=reiniciar,
                on_progress=lambda parcial: _guardar_estado(
                    config, ejecutando=True, resumen=parcial
                ),
            )
            _guardar_estado(config, ejecutando=False, resumen=r)
        except Exception as exc:  # nunca tumbar el hilo: registra el motivo para la UI
            log.warning("backfill_fondo_error", error=str(exc)[:200])
            _guardar_estado(config, ejecutando=False, resumen=resumen_inicial, error=str(exc)[:200])

    _PARAR.clear()
    _HILO = threading.Thread(target=_correr, name="backfill-entidades", daemon=True)
    _HILO.start()
    return {"lanzado": True, "motivo": "backfill iniciado en segundo plano"}
