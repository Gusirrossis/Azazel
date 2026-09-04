"""Casar el texto de una búsqueda con ENTIDADES resueltas.

El objetivo: quien busca «Alfredo Adame» o una CURP no quiere saber en qué PDFs
aparece — quiere saber si esa persona está. Hasta ahora `/buscar` solo devolvía
documentos, y la entidad resuelta vivía en otra tabla que nadie cruzaba.

Tres caminos, en orden de coste creciente. Se para en el primero que da resultados:

  1. El texto ES una CURP o un RFC válido  → búsqueda exacta por ancla, microsegundos.
  2. El texto parece un nombre             → trigramas sobre `nombre_completo`.
  3. Ninguna de las dos                    → se sacan las anclas de los documentos
                                             que ya casaron y se resuelven ESAS.

El tercero es el que convierte «encontré 8.518 PDFs» en «encontré a esta persona, y
aparece en 12 de ellos». Es barato porque no busca nada: reusa las anclas que el
worker ya extrajo y guardó en `contexto_anclas` al indexar.
"""

from __future__ import annotations

from typing import Any

import psycopg

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

from . import anclas, derivados
from . import normalizadores as N

log = obtener_logger("entidades.coincidencias")

#: Tope de entidades que se devuelven junto a una página de resultados. Quien federa
#: pinta una lista corta al lado de los documentos, no un padrón: devolver cientos
#: engordaría la respuesta por lo mismo que se acaba de quitar el `texto_indexable`.
MAX_ENTIDADES = 10

#: Longitud mínima para buscar por nombre. Por debajo, los trigramas devuelven media
#: tabla y el resultado no le sirve a nadie. Mismo criterio que el umbral del comodín
#: en la búsqueda de documentos.
_MIN_NOMBRE = 4

_CAMPOS = "entidad_id, tipo, campos, procedencias"


def _fila(f: tuple[Any, ...], via: str) -> dict[str, Any]:
    campos = dict(f[2] or {})
    return {
        "entidad_id": f[0],
        "tipo": f[1],
        # `procedencias` puede tener cientos de rutas; se manda el CONTEO, no la
        # lista. Quien quiera el detalle pide la entidad por su id.
        "documentos": len(f[3] or []),
        "coincide_por": via,
        **derivados.ficha_breve(campos),
    }


def _por_ancla(conn: psycopg.Connection[Any], valores: list[str], via: str) -> list[dict[str, Any]]:
    if not valores:
        return []
    filas = conn.execute(
        f"SELECT {_CAMPOS} FROM entidades"
        " WHERE activo AND ancla_valor = ANY(%s) LIMIT %s",
        (valores, MAX_ENTIDADES),
    ).fetchall()
    return [_fila(f, via) for f in filas]


def buscar_coincidencias(
    config: Config, texto: str | None, documentos: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Entidades que casan con esta búsqueda. Nunca lanza: sin entidades, lista vacía.

    Que un fallo aquí no rompa la búsqueda es deliberado — los documentos son el
    resultado principal y la entidad es un extra. Degradar sin ella es aceptable;
    devolver un 500 por no encontrarla, no.
    """
    if not texto:
        return []
    try:
        return _buscar(config, texto.strip(), documentos)
    except Exception as exc:
        log.warning("coincidencias_fallidas", error=str(exc)[:200])
        return []


def _buscar(
    config: Config, texto: str, documentos: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        # 1) ¿El texto ES un ancla? Se valida con dígito verificador, no por forma:
        # una cadena con pinta de CURP que no lo es no debe disparar una consulta.
        arriba = texto.upper()
        exactas: list[str] = []
        for validar in (N.validar_curp, N.validar_rfc):
            n = validar(arriba)
            if n.valido and n.valor:
                exactas.append(n.valor)
        if exactas:
            encontradas = _por_ancla(conn, exactas, "ancla")
            if encontradas:
                return encontradas

        # 2) ¿Parece un nombre? Trigramas (índice ix_entidades_nombre_trgm).
        if len(texto) >= _MIN_NOMBRE:
            patron = "%" + texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            filas = conn.execute(
                f"SELECT {_CAMPOS} FROM entidades"
                " WHERE activo AND campos->>'nombre_completo' ILIKE %s LIMIT %s",
                (patron, MAX_ENTIDADES),
            ).fetchall()
            if filas:
                return [_fila(f, "nombre") for f in filas]

        # 3) Las anclas que ya venían en los documentos que casaron. No se busca nada
        # nuevo: el worker las extrajo al indexar y viven en `contexto_anclas`.
        de_docs: list[str] = []
        vistos: set[str] = set()
        for doc in documentos[:20]:
            for a in doc.get("contexto_anclas") or []:
                v = a.get("valor")
                if v and v not in vistos:
                    vistos.add(v)
                    de_docs.append(v)
            # Cuando `contexto_anclas` no viaja (excluido de _source o campos
            # filtrados), se relee del texto que sí vino.
            if not doc.get("contexto_anclas"):
                for a in anclas.buscar_en_texto(doc.get("texto_indexable")):
                    if a.valor not in vistos:
                        vistos.add(a.valor)
                        de_docs.append(a.valor)
        return _por_ancla(conn, de_docs[:MAX_ENTIDADES], "documento")
