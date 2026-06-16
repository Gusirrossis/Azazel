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

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

from . import normalizadores as N
from .modelo import calcular_entidad_id
from .pipeline import _upsert, construir_entidad
from .receta import obtener_receta

log = obtener_logger("entidades.backfill")

# Regex LIBERALES para encontrar candidatos; el validador (dígito verificador / formato)
# es el que decide. Lookarounds para no cortar dentro de una cadena alfanumérica mayor.
_SCAN_CURP = re.compile(r"(?<![0-9A-ZÑ])[A-ZÑ]{4}\d{6}[HM][A-ZÑ]{5}[0-9A-ZÑ]\d(?![0-9A-ZÑ])")
_SCAN_RFC = re.compile(r"(?<![0-9A-ZÑ&])[A-ZÑ&]{4}\d{6}[0-9A-ZÑ]{3}(?![0-9A-ZÑ])")

_CURSOR_CLAVE = "backfill_entidades_cursor"
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


def _texto_de_doc(doc: dict[str, Any]) -> str:
    """Concatena los campos del doc donde puede aparecer una CURP/RFC (en MAYÚSCULAS)."""
    partes: list[str] = []
    for clave in _FUENTES:
        v = doc.get(clave)
        if isinstance(v, str):
            partes.append(v)
        elif isinstance(v, dict):  # campos_extraidos: valores estructurados
            partes.extend(str(x) for x in v.values() if isinstance(x, (str, int)))
    return " ".join(partes).upper()


def _anclas_validas(texto: str) -> tuple[list[str], list[str]]:
    """Devuelve (curps_validas, rfcs_validas) únicas, en orden de aparición."""
    curps: list[str] = []
    for m in _SCAN_CURP.finditer(texto):
        n = N.validar_curp(m.group())
        if n.valido and n.valor and n.valor not in curps:
            curps.append(n.valor)
    rfcs: list[str] = []
    for m in _SCAN_RFC.finditer(texto):
        n = N.validar_rfc(m.group())
        if n.valido and n.valor and n.valor not in rfcs:
            rfcs.append(n.valor)
    return curps, rfcs


def personas_de_doc(doc: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Extrae las filas-persona (anclas) de un documento indexado + su procedencia.

    Regla de anclaje (evita duplicar a la misma persona en docs multi-persona):
      · hay CURP(s): una fila por CURP; si es 1 CURP + 1 RFC, el RFC va en esa fila.
      · no hay CURP pero sí RFC(s): una fila por RFC.
    """
    curps, rfcs = _anclas_validas(_texto_de_doc(doc))
    filas: list[dict[str, str]] = []
    if curps:
        for c in curps:
            fila = {"curp": c}
            if len(curps) == 1 and len(rfcs) == 1:
                fila["rfc"] = rfcs[0]
            filas.append(fila)
    else:
        filas = [{"rfc": r} for r in rfcs]
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
        "_source": list(_FUENTES) + ["archivo_id", "disco_id"],
    }
    if cursor:
        cuerpo["search_after"] = [cursor]
    return cliente.search(index=alias, body=cuerpo)["hits"]["hits"]


def _resolver_fila(
    conn: psycopg.Connection[Any], receta: Any, fila: dict[str, str],
    procedencia: dict[str, Any], resumen: ResumenBackfill,
) -> None:
    ent = construir_entidad(receta, _ASIGNACION, fila)
    if ent is None:
        return
    resumen.anclas += 1
    eid = calcular_entidad_id(receta.tipo, ent["ancla_tipo"], ent["ancla_valor"])
    resultado = _upsert(
        conn, eid, receta.tipo, ent["ancla_tipo"], ent["ancla_valor"],
        ent["campos"], receta.version, _VERSION_RES, procedencia,
    )
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
    drena hasta el final. `reiniciar` ignora el cursor guardado y empieza de cero.
    El cursor se persiste por lote, así que una corrida interrumpida continúa donde
    quedó y re-ejecutarla es idempotente (no duplica entidades)."""
    from normalizacion.core.indexador.opensearch import crear_cliente

    cliente = crear_cliente(config)
    receta = obtener_receta("persona")
    resumen = ResumenBackfill()
    lote = max(1, min(lote, 2000))

    with psycopg.connect(config.postgres_dsn) as conn:
        cursor = None if reiniciar else _leer_cursor(conn)
        while True:
            hits = _buscar_lote(cliente, config.indice_alias, cursor, lote)
            if not hits:
                break
            for hit in hits:
                doc = hit["_source"]
                resumen.docs += 1
                cursor = doc.get("archivo_id") or hit.get("sort", [None])[0]
                filas, procedencia = personas_de_doc(doc)
                if not filas:
                    resumen.sin_persona += 1
                    continue
                resumen.con_persona += 1
                for fila in filas:
                    try:
                        _resolver_fila(conn, receta, fila, procedencia, resumen)
                    except Exception as exc:  # fila/doc envenenado → dead-letter, sigue
                        resumen.errores += 1
                        log.warning("backfill_fila_fallida", error=str(exc)[:200])
            if cursor:
                _guardar_cursor(conn, cursor)
            conn.commit()
            resumen.cursor = cursor
            if on_progress:
                on_progress(resumen)
            if max_docs is not None and resumen.docs >= max_docs:
                break
    log.info("backfill_completo", **resumen.como_dict())
    return resumen
