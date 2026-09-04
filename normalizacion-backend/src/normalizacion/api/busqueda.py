"""Traducción server-side: parámetros tipados → DSL de OpenSearch (nunca al revés).

- `construir_consulta` es PURA (unit-testable): allowlist implícita, página acotada,
  sort estable (puntaje desc + archivo_id asc = tiebreaker para search_after).
- Paginación profunda: search_after + PIT (vista estable), jamás from/size
  (PROPUESTA §9: deep paging es lo que tumba clústeres).
"""

from __future__ import annotations

from typing import Any

from normalizacion.api.esquemas import (
    Estadisticas,
    RespuestaBusqueda,
    SolicitudBusqueda,
)
from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("api.busqueda")

_SORT_ESTABLE = [{"puntaje": {"order": "desc", "missing": 0}}, {"archivo_id": {"order": "asc"}}]
_SORT_RELEVANCIA = [{"_score": {"order": "desc"}}, {"archivo_id": {"order": "asc"}}]

# Marcadores de resaltado NO-HTML: el front los convierte a <mark> de forma segura
# (nunca se inyecta HTML del contenido de archivos al navegador)
MARCA_INICIO = "⟪"  # ⟪
MARCA_FIN = "⟫"  # ⟫


#: Campos que un cliente puede pedir en `SolicitudBusqueda.campos`.
#:
#: Se deriva del modelo del documento, no se escribe a mano: así un campo nuevo se
#: puede pedir sin tocar esto, y —más importante— un campo que se RETIRE del modelo
#: deja de ser pedible en el acto.
#:
#: `contexto_anclas` queda FUERA a propósito. Está excluido de `_source` en el
#: mapping por ser datos personales (±200 caracteres alrededor de cada CURP), y
#: dejarlo en la allowlist sugeriría que se puede pedir. No se puede: OpenSearch no
#: lo tiene guardado en `_source`, así que pedirlo devolvería vacío y confundiría.
def _campos_permitidos() -> frozenset[str]:
    from normalizacion.core.modelo import DocumentoArchivo

    return frozenset(DocumentoArchivo.model_fields) - {"contexto_anclas"}


def _source_de(solicitud: SolicitudBusqueda) -> list[str] | None:
    """Traduce `campos` a `_source`. None = todos (el comportamiento de siempre).

    Es una ALLOWLIST y no un paso directo: el cuerpo de la consulta a OpenSearch se
    construye en el servidor y nada de lo que llega del cliente entra en él como
    sintaxis — la misma disciplina que ya tiene el resto de `construir_consulta`.
    Lo desconocido se descarta en silencio en vez de dar error: un cliente que pide
    un campo que ya no existe debe seguir funcionando, no romperse.
    """
    if not solicitud.campos:
        return None
    permitidos = _campos_permitidos()
    pedidos = [c for c in solicitud.campos if c in permitidos]
    # Ni un solo campo válido: se devuelve el documento entero en vez de uno vacío.
    # Un `_source: []` daría documentos sin nada y parecería que no hay resultados.
    return pedidos or None


#: Longitud a partir de la cual se permite el comodín INICIAL sobre `nombre`.
#:
#: Un `*a*` obliga a recorrer el campo de los 390.000 documentos. Medido contra el
#: índice real: `a` → 500 tras 30 s (el hilo de OpenSearch se agota), `de` → 20 s,
#: `la` → 10 s, `garcia` → 1,8 s. Es un gradiente, no un caso raro: cuanto más corto
#: y frecuente el término, peor.
#:
#: Importa porque quien federa (Lilith) manda TEXTO LIBRE de usuario: una letra
#: suelta o una errata tumban un hilo y devuelven un 500 que el consumidor no puede
#: distinguir de "Azazel está caído". Por debajo de este umbral se busca por PREFIJO,
#: que sí usa el índice y responde en milisegundos.
_MIN_COMODIN_INICIAL = 4

#: Tope de tiempo que se le da a OpenSearch. Con él devuelve lo que llevara
#: encontrado marcándolo como parcial, en vez de agotar el hilo y dar un 500.
#: Es una red de seguridad para el término que se escape del umbral de arriba.
_TIMEOUT_BUSQUEDA = "15s"


def _ramas_de_texto(texto: str) -> list[dict[str, Any]]:
    """Las formas de casar el texto del usuario. Solo viaja como VALOR, nunca como
    sintaxis: el DSL lo construye el servidor entero (allowlist implícita)."""
    limpio = texto.lower().strip()
    # El orden importa: la rama del NOMBRE va primera, como siempre. Hay tests que la
    # localizan por posición, y cambiarlo sin necesidad rompe a quien la consuma.
    if len(limpio) >= _MIN_COMODIN_INICIAL:
        por_nombre: dict[str, Any] = {
            "wildcard": {"nombre": {"value": f"*{limpio}*", "case_insensitive": True}}
        }
    else:
        # Prefijo en vez de comodín inicial: `ana*` sí puede saltar por el índice,
        # `*ana*` no. Se pierde encontrar "Mariana" tecleando "ana", que es un precio
        # razonable por no colgar el clúster con cada pulsación corta.
        por_nombre = {"prefix": {"nombre": {"value": limpio, "case_insensitive": True}}}
    return [
        por_nombre,
        # El contenido extraído: usa el índice invertido y es barata a cualquier
        # longitud, así que esta rama nunca se quita.
        {"match": {"texto_indexable": {"query": texto, "operator": "and"}}},
    ]


def construir_consulta(solicitud: SolicitudBusqueda, pagina_max: int) -> dict[str, Any]:
    """DSL desde los campos tipados. El texto del usuario SOLO viaja como VALOR
    (wildcard sobre nombre + match sobre el texto extraído — sin sintaxis inyectable).

    `texto` busca en el NOMBRE **y** en el CONTENIDO extraído (texto_indexable):
    escribir el nombre de una persona encuentra los PDFs que la mencionan."""
    filtros: list[dict[str, Any]] = []
    debe: list[dict[str, Any]] = []
    if solicitud.texto:
        debe.append({"bool": {"should": _ramas_de_texto(solicitud.texto), "minimum_should_match": 1}})
    if solicitud.tipo_real:
        filtros.append({"term": {"tipo_real": solicitud.tipo_real}})
    if solicitud.extension:
        filtros.append({"term": {"extension": solicitud.extension.lower()}})
    if solicitud.disco_id:
        filtros.append({"term": {"disco_id": solicitud.disco_id}})
    if solicitud.puntaje_min is not None:
        filtros.append({"range": {"puntaje": {"gte": solicitud.puntaje_min}}})
    if solicitud.tamano_min is not None or solicitud.tamano_max is not None:
        rango: dict[str, int] = {}
        if solicitud.tamano_min is not None:
            rango["gte"] = solicitud.tamano_min
        if solicitud.tamano_max is not None:
            rango["lte"] = solicitud.tamano_max
        filtros.append({"range": {"tamano": rango}})

    if debe or filtros:
        consulta: dict[str, Any] = {"bool": {}}
        if debe:
            consulta["bool"]["must"] = debe
        if filtros:
            consulta["bool"]["filter"] = filtros
    else:
        consulta = {"match_all": {}}

    cuerpo: dict[str, Any] = {
        "size": min(solicitud.tamano_pagina, pagina_max),  # límite DURO del servidor
        # Con texto: ordenar por RELEVANCIA (el mejor match primero); sin él, por puntaje
        "sort": _SORT_RELEVANCIA if debe else _SORT_ESTABLE,
        "query": consulta,
        "track_total_hits": True,
        # Devuelve lo que lleve encontrado en vez de agotar el hilo: un 500 tras 30 s
        # es indistinguible de "el servicio esta caido" para quien federa.
        "timeout": _TIMEOUT_BUSQUEDA,
    }
    fuente = _source_de(solicitud)
    if fuente is not None:
        cuerpo["_source"] = fuente
    if debe:  # fragmentos del contenido donde aparece lo buscado
        cuerpo["highlight"] = {
            "fields": {"texto_indexable": {"fragment_size": 180, "number_of_fragments": 2}},
            "pre_tags": [MARCA_INICIO],
            "post_tags": [MARCA_FIN],
            "encoder": "default",
        }
    if solicitud.cursor:
        cuerpo["search_after"] = solicitud.cursor
    if solicitud.facetas:
        cuerpo["aggs"] = {
            "por_tipo": {"terms": {"field": "tipo_real", "size": 20}},
            "por_extension": {"terms": {"field": "extension", "size": 20}},
            "por_disco": {"terms": {"field": "disco_id", "size": 20}},
        }
    return cuerpo


def _abrir_pit(cliente: Any, alias: str) -> str | None:
    try:
        respuesta = cliente.transport.perform_request(
            "POST", f"/{alias}/_search/point_in_time", params={"keep_alive": "2m"}
        )
        pit = respuesta.get("pit_id")
        return str(pit) if pit else None
    except Exception as exc:  # PIT no disponible: se pagina sin vista estable
        log.warning("pit_no_disponible", error=str(exc)[:150])
        return None


def buscar(cliente: Any, config: Config, solicitud: SolicitudBusqueda) -> RespuestaBusqueda:
    cuerpo = construir_consulta(solicitud, config.api_pagina_max)

    pit_id = solicitud.pit_id
    if pit_id is None and solicitud.abrir_pit:
        pit_id = _abrir_pit(cliente, config.indice_alias)
    if pit_id:
        cuerpo["pit"] = {"id": pit_id, "keep_alive": "2m"}
        respuesta = cliente.search(body=cuerpo)  # con PIT no se pasa índice
    else:
        respuesta = cliente.search(index=config.indice_alias, body=cuerpo)

    hits = respuesta["hits"]["hits"]
    facetas: dict[str, dict[str, int]] | None = None
    if solicitud.facetas and "aggregations" in respuesta:
        facetas = {
            nombre: {b["key"]: b["doc_count"] for b in agg["buckets"]}
            for nombre, agg in respuesta["aggregations"].items()
        }
    documentos = []
    for h in hits:
        doc = dict(h["_source"])
        fragmentos = h.get("highlight", {}).get("texto_indexable")
        if fragmentos:
            doc["_resaltado"] = fragmentos
        documentos.append(doc)
    return RespuestaBusqueda(
        total=respuesta["hits"]["total"]["value"],
        documentos=documentos,
        cursor=hits[-1]["sort"] if hits else None,
        facetas=facetas,
        pit_id=respuesta.get("pit_id", pit_id),
        origen=config.despliegue.nodo_id,
    )


def autocompletar(cliente: Any, config: Config, prefijo: str, limite: int) -> list[str]:
    limite = min(limite, config.api_autocompletar_max)
    respuesta = cliente.search(
        index=config.indice_alias,
        body={
            "size": limite * 3,  # margen para deduplicar nombres repetidos
            "_source": ["nombre"],
            "query": {
                "wildcard": {"nombre": {"value": f"{prefijo.lower()}*", "case_insensitive": True}}
            },
        },
    )
    vistos: list[str] = []
    for hit in respuesta["hits"]["hits"]:
        nombre = hit["_source"]["nombre"]
        if nombre not in vistos:
            vistos.append(nombre)
        if len(vistos) >= limite:
            break
    return vistos


def doc_por_id(cliente: Any, config: Config, archivo_id: str) -> dict[str, Any] | None:
    respuesta = cliente.search(
        index=config.indice_alias,
        body={"size": 1, "query": {"term": {"archivo_id": archivo_id}}},
    )
    hits = respuesta["hits"]["hits"]
    fuente: dict[str, Any] | None = hits[0]["_source"] if hits else None
    return fuente


def estadisticas(cliente: Any, config: Config) -> Estadisticas:
    respuesta = cliente.search(
        index=config.indice_alias,
        body={
            "size": 0,
            "track_total_hits": True,
            "aggs": {
                "bytes": {"sum": {"field": "tamano"}},
                "por_tipo": {"terms": {"field": "tipo_real", "size": 20}},
                "por_disco": {"terms": {"field": "disco_id", "size": 20}},
            },
        },
    )
    aggs = respuesta["aggregations"]
    return Estadisticas(
        total_documentos=respuesta["hits"]["total"]["value"],
        bytes_totales=int(aggs["bytes"]["value"]),
        por_tipo={b["key"]: b["doc_count"] for b in aggs["por_tipo"]["buckets"]},
        por_disco={b["key"]: b["doc_count"] for b in aggs["por_disco"]["buckets"]},
    )
