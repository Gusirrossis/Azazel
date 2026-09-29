"""Traducción server-side: parámetros tipados → DSL de OpenSearch (nunca al revés).

- `construir_consulta` es PURA (unit-testable): allowlist implícita, página acotada,
  sort estable (puntaje desc + archivo_id asc = tiebreaker para search_after).
- Paginación profunda: search_after + PIT (vista estable), jamás from/size
  (PROPUESTA §9: deep paging es lo que tumba clústeres).
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import HTTPException
from opensearchpy.exceptions import ConnectionError as ErrorConexionOS
from opensearchpy.exceptions import ConnectionTimeout, TransportError

from normalizacion.api.esquemas import (
    ConsultaLote,
    Estadisticas,
    ModoTexto,
    RespuestaBusqueda,
    RespuestaLote,
    ResultadoLote,
    SolicitudBusqueda,
    SolicitudLote,
)
from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger
from normalizacion.entidades import coincidencias

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


#: Campos donde viven las anclas. Se piden internamente cuando el consumidor quiere
#: entidades, aunque los haya filtrado de su respuesta. Es la misma lista que usan el
#: backfill y `coincidencias`; la unicidad de esa lista está fijada con un test.
_FUENTES_ANCLA = ("texto_indexable", "campos_extraidos", "nombre", "ruta_original")


def _podar_documentos(
    documentos: list[dict[str, Any]], pedidos: list[str] | None
) -> list[dict[str, Any]]:
    """Quita los campos que se pidieron a OpenSearch pero el consumidor no quiere.

    `_resaltado` NO se toca: no sale de `_source` sino del `highlight`, y es lo que
    de verdad se pinta.
    """
    if not pedidos:
        return documentos
    conservar = set(pedidos)
    return [
        {k: v for k, v in doc.items() if k in conservar or k.startswith("_")}
        for doc in documentos
    ]


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
    if not pedidos:
        # Ni un solo campo válido: se devuelve el documento entero en vez de uno
        # vacío. Un `_source: []` daría documentos sin nada y parecería que no hay
        # resultados.
        return None
    if solicitud.incluir_entidades:
        # Las dos funciones se peleaban: `campos` quita `texto_indexable` para
        # ahorrar el 59% del tráfico, y el descubrimiento de entidades lo necesita
        # para leer las anclas de los documentos. Un consumidor que usa las dos
        # —que es exactamente el caso de la federación— recibía CERO entidades.
        #
        # Se pide internamente lo que hace falta y se quita antes de responder
        # (`_podar_documentos`): el consumidor conserva su payload pequeño y las
        # entidades aparecen igual.
        pedidos = pedidos + [c for c in _FUENTES_ANCLA if c in permitidos and c not in pedidos]
    return pedidos


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

#: Lo que la API espera a OpenSearch. Ese "15s" solo acota la fase de CONSULTA: la de
#: recuperar los documentos (y leer el texto entero de cada uno para el resaltado) no
#: tiene tope, y con el disco de la matriz saturado —presión de IO ~78 %— una búsqueda
#: en frío tardaba 16-37 s. El cliente cortaba a los 30 s de fábrica y la API devolvía
#: un 500 mudo: medido el 26-09, 2 de 65 búsquedas federadas de Lilith, que espera 60 s.
#: 55 s queda por debajo de ese plazo, así que una búsqueda lenta pero viva llega entera.
_PLAZO_CLIENTE_S = 55

#: Techo de la fase de consulta cuando quien pregunta manda su `presupuesto_ms`. Queda
#: por debajo del plazo del cliente para que la fase de recuperar documentos (que no
#: tiene tope) aún quepa, y no se pierda entera una búsqueda que ya había encontrado.
_TECHO_CONSULTA_MS = 50_000
#: Plazo del cliente con `presupuesto_ms`: el presupuesto más un margen para esa fase,
#: y nunca por encima de 58 s (Lilith corta cada intento a los 60).
_MARGEN_RECUPERAR_S = 5
_PLAZO_CLIENTE_MAX_S = 58

#: Posiciones de holgura del modo `cerca`: alcanza para reordenar un nombre de 3-4
#: palabras («PEREZ LOPEZ JUAN») y para un campo intermedio corto de un volcado.
_HOLGURA_CERCA = 6

#: Segundos que se sugieren en `Retry-After` cuando OpenSearch rechaza por saturación.
_REINTENTO_S = 5


def _plazo_cliente_s(presupuesto_ms: int | None) -> float:
    if presupuesto_ms is None:
        return _PLAZO_CLIENTE_S
    return min(presupuesto_ms / 1000 + _MARGEN_RECUPERAR_S, _PLAZO_CLIENTE_MAX_S)


def _timeout_consulta(presupuesto_ms: int | None) -> str:
    if presupuesto_ms is None:
        return _TIMEOUT_BUSQUEDA
    return f"{min(presupuesto_ms, _TECHO_CONSULTA_MS)}ms"


def _rama_contenido(texto: str, modo: ModoTexto | None) -> dict[str, Any]:
    """Cómo casa el texto con el CONTENIDO según el modo (ver `ModoTexto`)."""
    if modo == "frase":
        return {"match_phrase": {"texto_indexable": {"query": texto}}}
    if modo == "cerca":
        # Frase con holgura y no `intervals`: el resaltado sigue a la consulta, y con una
        # frase el fragmento enseña las palabras JUNTAS. Quien federa decide con ese
        # fragmento (¿es esta persona?); con palabras sueltas no puede.
        return {"match_phrase": {"texto_indexable": {"query": texto, "slop": _HOLGURA_CERCA}}}
    # `None` y `todas`: cada palabra en algún sitio del documento (el de siempre).
    return {"match": {"texto_indexable": {"query": texto, "operator": "and"}}}


def _ramas_de_texto(texto: str, modo: ModoTexto | None = None) -> list[dict[str, Any]]:
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
        _rama_contenido(texto, modo),
    ]


def construir_consulta(solicitud: SolicitudBusqueda, pagina_max: int) -> dict[str, Any]:
    """DSL desde los campos tipados. El texto del usuario SOLO viaja como VALOR
    (wildcard sobre nombre + match sobre el texto extraído — sin sintaxis inyectable).

    `texto` busca en el NOMBRE **y** en el CONTENIDO extraído (texto_indexable):
    escribir el nombre de una persona encuentra los PDFs que la mencionan."""
    filtros: list[dict[str, Any]] = []
    debe: list[dict[str, Any]] = []
    if solicitud.texto:
        debe.append(
            {
                "bool": {
                    "should": _ramas_de_texto(solicitud.texto, solicitud.modo),
                    "minimum_should_match": 1,
                }
            }
        )
    if solicitud.tipo_real:
        filtros.append({"term": {"tipo_real": solicitud.tipo_real}})
    if solicitud.extension:
        filtros.append({"term": {"extension": solicitud.extension.lower()}})
    if solicitud.disco_id:
        filtros.append({"term": {"disco_id": solicitud.disco_id}})
    if solicitud.ruta_prefijo:
        # `prefix` sobre el sub-campo KEYWORD `ruta_original.exacta`, NO sobre el campo
        # `wildcard`. El `wildcard` sin doc_values saca a TODOS los candidatos por
        # n-gramas y verifica uno a uno: para un prefijo de base (`<ULID>.db!`) eso es
        # 16-30 s y un 500 que quien federa no distingue de "Azazel caído" (medido).
        # El keyword camina el diccionario de términos ordenado (FST): 0,018 s, mismo
        # conjunto de resultados. Sigue siendo un valor LITERAL —sin `*`/`?` que
        # interpretar—, así que nadie puede colar un patrón que barra el índice.
        # `.exacta` es el multi-field que espeja `ruta_original` (lo deriva OpenSearch).
        filtros.append({"prefix": {"ruta_original.exacta": solicitud.ruta_prefijo}})
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
        "timeout": _timeout_consulta(solicitud.presupuesto_ms),
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
            # `size` holgado: `tipo_real`/`extension` tienen cardinalidad acotada (MIMEs,
            # extensiones) pero un top-20 omitía en silencio las categorías fuera de los 20
            # más frecuentes — un consumidor que inventaríe el corpus veía menos tipos de
            # los que hay. 10 000 cubre la cardinalidad real sin coste apreciable.
            "por_tipo": {"terms": {"field": "tipo_real", "size": 10000}},
            "por_extension": {"terms": {"field": "extension", "size": 10000}},
            "por_disco": {"terms": {"field": "disco_id", "size": 10000}},
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


def _es_parcial(respuesta: dict[str, Any]) -> bool:
    """¿Resultados INCOMPLETOS? OpenSearch con timeout devuelve lo que alcanzó y marca
    `timed_out`; un shard que falla cuenta en `_shards.failed`. (`skipped` NO es pérdida:
    son shards podados en can-match por no poder casar.) Sin leer esto, `buscar` armaba
    la respuesta como si estuviera completa."""
    shards = respuesta.get("_shards", {})
    return bool(respuesta.get("timed_out")) or bool(shards.get("failed"))


def _documentos_de(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documentos = []
    for h in hits:
        doc = dict(h["_source"])
        fragmentos = h.get("highlight", {}).get("texto_indexable")
        if fragmentos:
            doc["_resaltado"] = fragmentos
        documentos.append(doc)
    return documentos


def _no_disponible(exc: Exception) -> None:
    """OpenSearch no puede atender AHORA: un 503 con `Retry-After`, no un 500.

    Dos casos: rechaza por saturación (429, su cola de búsquedas llena) o no se le puede
    ni conectar (reiniciándose, caído). Los dos se arreglan esperando, y quien federa
    reintenta un 503 pero no debe reintentar un error de verdad. Cualquier otro error de
    OpenSearch sigue su camino: esconderlo como «vuelve luego» sería mentir."""
    estado = getattr(exc, "status_code", None)
    if isinstance(exc, ErrorConexionOS) or estado == 429:
        log.warning("busqueda_opensearch_no_disponible", estado=estado, tipo=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="el índice no puede atender ahora; reintenta en unos segundos",
            headers={"Retry-After": str(_REINTENTO_S)},
        ) from exc
    raise exc


def buscar(cliente: Any, config: Config, solicitud: SolicitudBusqueda) -> RespuestaBusqueda:
    cuerpo = construir_consulta(solicitud, config.api_pagina_max)

    pit_id = solicitud.pit_id
    if pit_id is None and solicitud.abrir_pit:
        pit_id = _abrir_pit(cliente, config.indice_alias)
    plazo = _plazo_cliente_s(solicitud.presupuesto_ms)
    try:
        if pit_id:
            cuerpo["pit"] = {"id": pit_id, "keep_alive": "2m"}
            # con PIT no se pasa índice
            respuesta = cliente.search(body=cuerpo, request_timeout=plazo)
        else:
            respuesta = cliente.search(
                index=config.indice_alias, body=cuerpo, request_timeout=plazo
            )
    except ConnectionTimeout as exc:
        # Un 504 con motivo, no un 500: quien federa tiene que poder distinguir "tardó
        # demasiado, reintenta" de "Azazel está roto".
        log.warning("busqueda_plazo_agotado", plazo_s=plazo)
        raise HTTPException(
            status_code=504,
            detail=f"la búsqueda tardó más de {plazo:.0f} s; reintenta en unos segundos",
        ) from exc
    except (ErrorConexionOS, TransportError) as exc:
        _no_disponible(exc)

    parcial = _es_parcial(respuesta)
    hits = respuesta["hits"]["hits"]
    facetas: dict[str, dict[str, int]] | None = None
    if solicitud.facetas and "aggregations" in respuesta:
        facetas = {
            nombre: {b["key"]: b["doc_count"] for b in agg["buckets"]}
            for nombre, agg in respuesta["aggregations"].items()
        }
    documentos = _documentos_de(hits)
    # Las entidades se buscan con los documentos COMPLETOS (llevan los campos de
    # anclas), y solo despues se poda lo que el consumidor no pidio.
    entidades = (
        coincidencias.buscar_coincidencias(config, solicitud.texto, documentos)
        if solicitud.incluir_entidades
        else None
    )
    pedidos = [c for c in (solicitud.campos or []) if c in _campos_permitidos()]
    return RespuestaBusqueda(
        total=respuesta["hits"]["total"]["value"],
        documentos=_podar_documentos(documentos, pedidos or None),
        cursor=hits[-1]["sort"] if hits else None,
        facetas=facetas,
        pit_id=respuesta.get("pit_id", pit_id),
        origen=config.despliegue.nodo_id,
        entidades=entidades,
        parcial=parcial,
    )


# ------------------------------------------------------------------ búsqueda por lotes

#: Búsquedas simultáneas del lote. OpenSearch tiene 4 CPUs (7 hilos de búsqueda) y cada
#: búsqueda toca los 6 índices del alias: más no va más rápido, solo pone en cola a las
#: búsquedas sueltas de los demás.
_CONCURRENCIA_LOTE = 3
#: Por debajo de esto una consulta ya no se lanza: no le daría tiempo ni a la fase de
#: consulta. Vuelve con `ejecutada: false`.
_MINIMO_CONSULTA_S = 1.0
#: Orden de EJECUCIÓN (los resultados salen en el orden pedido): lo barato primero, para
#: que un presupuesto corto se gaste en lo que cabe. Medido en frío el 29-09: una CURP
#: en el contenido, 0,2 s; un teléfono, 1,7 s seguido y 5-7 s cada forma partida («55»
#: y los grupos de 4 cifras son términos frecuentísimos en los volcados SQL).
_ORDEN_COSTE: dict[str | None, int] = {
    "curp": 0, "rfc": 0, "nss": 0, "correo": 1, None: 2, "nombre": 3, "telefono": 4,
}

#: Identificadores que se buscan también en el nombre del archivo (ver `consulta_lote`).
_NOMBRE_DE_ARCHIVO = frozenset({"curp", "rfc", "correo"})

_NO_ALFANUM = re.compile(r"[^0-9A-ZÑ]")
_NO_DIGITO = re.compile(r"\D")


def normalizar_identificador(tipo: str | None, texto: str) -> str | None:
    """El identificador en su forma canónica, o None si no queda nada que buscar.

    curp/rfc/nss: mayúsculas y alfanuméricos. telefono: solo dígitos y, si trae lada
    internacional, los 10 últimos. correo: minúsculas y sin espacios. nombre / sin tipo:
    el texto tal cual (lo normaliza el analizador del índice)."""
    if tipo in ("curp", "rfc", "nss"):
        valor = _NO_ALFANUM.sub("", texto.upper())
    elif tipo == "telefono":
        digitos = _NO_DIGITO.sub("", texto)
        valor = digitos[-10:] if len(digitos) > 10 else digitos
    elif tipo == "correo":
        valor = "".join(texto.split()).lower()
    else:
        valor = texto.strip()
    return valor or None


def _variantes_telefono(diez: str) -> list[str]:
    """Cómo aparece escrito un teléfono en un texto. El analizador parte «55 1234 5678»
    en tres términos, así que cada forma es una FRASE distinta; la de 10 dígitos
    seguidos es un término solo. Con la lada (52) pegada también es un término aparte."""
    if len(diez) != 10:
        return [diez]
    return [
        diez,
        f"{diez[:2]} {diez[2:6]} {diez[6:]}",  # 55 1234 5678 (CDMX, GDL, MTY)
        f"{diez[:3]} {diez[3:6]} {diez[6:]}",  # 222 123 4567 (resto del país)
        f"52{diez}",
    ]


def consulta_lote(consulta: ConsultaLote) -> dict[str, Any] | None:
    """La parte `query` de UNA consulta del lote, o None si el texto no deja nada.

    Con tipo de identificador, la coincidencia es EXACTA sobre el término normalizado
    (frase sobre el contenido, más el nombre del archivo: las fotos de INE suelen
    llamarse como la CURP). Sin la rama de `nombre` con comodín del texto libre, que es
    lo caro de /buscar para un identificador. Sin tipo, o con `nombre`, se busca como
    en /buscar con su `modo` (con `nombre`, `frase` por omisión)."""
    valor = normalizar_identificador(consulta.tipo, consulta.texto)
    if valor is None:
        return None
    if consulta.tipo is None or consulta.tipo == "nombre":
        modo = consulta.modo or ("frase" if consulta.tipo == "nombre" else None)
        ramas = _ramas_de_texto(valor, modo)
    else:
        frases = _variantes_telefono(valor) if consulta.tipo == "telefono" else [valor]
        ramas = [{"match_phrase": {"texto_indexable": {"query": f}}} for f in frases]
        # El nombre del archivo, solo para lo que suele dar nombre a un archivo (la foto
        # de una INE se llama como la CURP). Con cifras no: el comodín se filtra por
        # trigramas, y «000» o «555» están en casi todos los nombres, así que revisa uno
        # a uno millones de candidatos (medido: un NSS agotó 20 s solo con esta rama).
        if consulta.tipo in _NOMBRE_DE_ARCHIVO and len(valor) >= _MIN_COMODIN_INICIAL:
            ramas.append(
                {"wildcard": {"nombre": {"value": f"*{valor.lower()}*", "case_insensitive": True}}}
            )
    return {"bool": {"should": ramas, "minimum_should_match": 1}}


def _cuerpo_lote(
    consulta: ConsultaLote, query: dict[str, Any], fuente: list[str] | None,
    pagina_max: int, timeout_ms: int,
) -> dict[str, Any]:
    """Mismo cuerpo que /buscar (orden, resaltado, total exacto) con la query del lote."""
    cuerpo: dict[str, Any] = {
        "size": min(consulta.tamano_pagina, pagina_max),
        "sort": _SORT_RELEVANCIA,
        "query": query,
        "track_total_hits": True,
        "timeout": f"{timeout_ms}ms",
        "highlight": {
            "fields": {"texto_indexable": {"fragment_size": 180, "number_of_fragments": 2}},
            "pre_tags": [MARCA_INICIO],
            "post_tags": [MARCA_FIN],
            "encoder": "default",
        },
    }
    if fuente is not None:
        cuerpo["_source"] = fuente
    if consulta.cursor:
        cuerpo["search_after"] = consulta.cursor
    return cuerpo


def buscar_lote(cliente: Any, config: Config, solicitud: SolicitudLote) -> RespuestaLote:
    """Muchas consultas en una petición, con un presupuesto de tiempo COMÚN.

    Cada consulta va por su lado (`_CONCURRENCIA_LOTE` a la vez) con el plazo que le
    quede al presupuesto. Así una lenta —un teléfono partido cuesta 5-7 s en frío— solo
    se marca ella como parcial: con `_msearch` la respuesta llega entera o no llega, y
    la más lenta de la tanda se llevaba por delante a las demás (medido: 12 consultas
    perdidas por un teléfono). La que ya no cabe vuelve sin lanzar (`ejecutada: false`).
    Nunca un 5xx por tiempo: se devuelve lo que cupo. Se ejecuta lo barato primero
    (`_ORDEN_COSTE`), pero los resultados salen en el orden de las consultas."""
    inicio = time.monotonic()
    limite = inicio + solicitud.presupuesto_ms / 1000
    fuente = _source_de(SolicitudBusqueda(campos=solicitud.campos))
    pedidos = [c for c in (solicitud.campos or []) if c in _campos_permitidos()] or None
    resultados: dict[str, ResultadoLote] = {}

    def _vacio(c: ConsultaLote, **extra: Any) -> ResultadoLote:
        return ResultadoLote(id=c.id, documentos=[], total=0, **extra)

    def _una(c: ConsultaLote, query: dict[str, Any]) -> ResultadoLote:
        restante = limite - time.monotonic()
        if restante < _MINIMO_CONSULTA_S:
            return _vacio(c, parcial=True, ejecutada=False)
        # El 80 % para la fase de consulta; el resto, para recuperar los documentos.
        cuerpo = _cuerpo_lote(c, query, fuente, config.api_pagina_max, int(restante * 800))
        try:
            item = cliente.search(
                index=config.indice_alias,
                body=cuerpo,
                request_timeout=min(restante, _PLAZO_CLIENTE_MAX_S),
            )
        except ConnectionTimeout:
            return _vacio(c, parcial=True, error="plazo")
        except (ErrorConexionOS, TransportError) as exc:
            tipo = str(getattr(exc, "error", "") or type(exc).__name__)
            log.warning("lote_consulta_fallida", tipo=tipo)
            return _vacio(c, parcial=True, error=tipo)
        hits = item["hits"]["hits"]
        return ResultadoLote(
            id=c.id,
            documentos=_podar_documentos(_documentos_de(hits), pedidos),
            total=item["hits"]["total"]["value"],
            parcial=_es_parcial(item),
            cursor=hits[-1]["sort"] if hits else None,
        )

    pendientes: list[tuple[ConsultaLote, dict[str, Any]]] = []
    for c in solicitud.consultas:
        query = consulta_lote(c)
        if query is None:  # el texto no dejó nada que buscar: no hay nada, y es exacto
            resultados[c.id] = _vacio(c)
        else:
            pendientes.append((c, query))
    pendientes.sort(key=lambda par: _ORDEN_COSTE.get(par[0].tipo, 2))  # estable

    # Cada tarea mira el presupuesto al empezar y lleva un plazo que no pasa de él, así
    # que el `with` (que espera a las que corren) no alarga la respuesta más allá.
    with ThreadPoolExecutor(max_workers=_CONCURRENCIA_LOTE) as ejecutor:
        futuros = [(c, ejecutor.submit(_una, c, q)) for c, q in pendientes]
    for c, futuro in futuros:
        resultados[c.id] = futuro.result()

    ordenados = [resultados[c.id] for c in solicitud.consultas]
    return RespuestaLote(
        origen=config.despliegue.nodo_id,
        resultados=ordenados,
        parcial=any(r.parcial for r in ordenados),
        ms=int((time.monotonic() - inicio) * 1000),
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
                # `size` holgado: el desglose por tipo real ya no omite en silencio los
                # tipos fuera del top-20 (cardinalidad de `tipo_real` acotada).
                "por_tipo": {"terms": {"field": "tipo_real", "size": 10000}},
                "por_disco": {"terms": {"field": "disco_id", "size": 10000}},
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
