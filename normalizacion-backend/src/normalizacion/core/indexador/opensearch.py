"""Backend OpenSearch del indexador: bulk con triple trigger, retry y dead-letter.

Patrones de producción (fscrawler):
- Flush por TRES disparadores (⚙K13): nº de acciones, bytes acumulados, timer.
- Retry con backoff exponencial (⚙K14) ante errores de transporte/429; agotados los
  reintentos, los docs van a dead-letter con motivo — jamás se pierden en silencio.
- `_id = archivo_id` → idempotente: reindexar sobrescribe, nunca duplica.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from normalizacion.core.config import Config
from normalizacion.core.modelo import DocumentoArchivo
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("indexador")


def indice_escritura(config: Config) -> str:
    """Índice físico INICIAL de este nodo. El alias agrupa los rotados (ISM).

    ⚙K16 — por nodo: en híbrido los dos nodos escriben en índices DISJUNTOS
    (`archivos-mac-01-000001`, `archivos-vps-01-000001`) para que restaurar el
    snapshot del otro AÑADA índices al alias en vez de sobrescribir el propio. En
    `local` el nombre es el histórico (`archivos-000001`): cero migración.

    Ojo: esto es sólo el nombre de CREACIÓN. Tras un rollover de ISM el índice de
    escritura real es otro, y por eso el sink escribe al ALIAS (§`SinkOpenSearch`).
    """
    d = config.despliegue
    base = config.indice_alias if d.es_local() else f"{config.indice_alias}-{d.nodo_id}"
    return f"{base}-000001"


def crear_cliente(config: Config) -> Any:
    from opensearchpy import OpenSearch

    return OpenSearch(
        hosts=[config.opensearch_url],
        use_ssl=False,
        verify_certs=False,
        ssl_show_warn=False,
        timeout=30,
    )


class SinkOpenSearch:
    """Bulk indexer con backpressure natural: el flush es síncrono — si OpenSearch
    va lento, el worker se frena solo (nada empuja sin límite).

    Escribe al **ALIAS**, no a un índice fijo. Antes apuntaba a la constante
    `f"{alias}-000001"`, así que un rollover de ISM creaba el índice nuevo y el sink
    seguía escribiendo en el viejo para siempre — la rotación por tamaño/edad no
    servía de nada. Con el alias (que lleva un `is_write_index` designado) el
    destino se mueve solo al rotar.

    Requisito: el alias debe existir. `aplicar_indice()` lo crea y el pipeline lo
    invoca antes de cada corrida (`ingesta/pipeline.py`). Ejecutar `norm worker`
    suelto contra un clúster virgen exige un `norm aplicar-indice` previo."""

    def __init__(self, config: Config, cliente: Any | None = None) -> None:
        self._perillas = config.indexador
        self._indice = config.indice_alias
        self._cliente = cliente if cliente is not None else crear_cliente(config)
        self._buffer: list[tuple[str, str]] = []  # (archivo_id, doc_json)
        self._bytes = 0
        self._ultimo_flush = time.monotonic()
        self._confirmados: list[str] = []
        self._muertos: list[tuple[str, str, bool]] = []
        self.total_indexados = 0

    # ------------------------------------------------------------- Sink
    def entregar(self, doc: DocumentoArchivo) -> None:
        linea = doc.model_dump_json()
        self._buffer.append((doc.archivo_id, linea))
        self._bytes += len(linea)
        if self._toca_flush():
            self._flush()

    def drenar(self) -> tuple[list[str], list[tuple[str, str, bool]]]:
        self._flush()
        confirmados, muertos = self._confirmados, self._muertos
        self._confirmados, self._muertos = [], []
        self.total_indexados += len(confirmados)
        return confirmados, muertos

    def cerrar(self) -> None:
        self.drenar()

    # ------------------------------------------------------------- interno
    def _toca_flush(self) -> bool:
        return (
            len(self._buffer) >= self._perillas.flush_acciones
            or self._bytes >= self._perillas.flush_bytes
            or (time.monotonic() - self._ultimo_flush) >= self._perillas.flush_segundos
        )

    def _flush(self) -> None:
        if not self._buffer:
            return
        lote = self._buffer
        self._buffer = []
        self._bytes = 0
        self._ultimo_flush = time.monotonic()

        cuerpo = "".join(
            json.dumps({"index": {"_index": self._indice, "_id": aid}}) + "\n" + linea + "\n"
            for aid, linea in lote
        )
        respuesta: dict[str, Any] | None = None
        for intento in range(self._perillas.reintentos_max + 1):
            try:
                respuesta = self._cliente.bulk(body=cuerpo)
                break
            except Exception as exc:  # transporte caído / 429 / timeout
                if intento >= self._perillas.reintentos_max:
                    # TRANSITORIO: OpenSearch caído no es culpa del doc — reintentable
                    motivo = f"transporte: {exc}"[:300]
                    self._muertos.extend((aid, motivo, True) for aid, _ in lote)
                    log.error("bulk_agotado", docs=len(lote), error=str(exc)[:200])
                    return
                espera = self._perillas.backoff_base_s * (2**intento)
                log.warning("bulk_reintento", intento=intento + 1, espera_s=espera)
                time.sleep(espera)

        assert respuesta is not None
        if not respuesta.get("errors"):
            self._confirmados.extend(aid for aid, _ in lote)
            return
        for (aid, _), item in zip(lote, respuesta.get("items", []), strict=False):
            error = item.get("index", {}).get("error")
            if error:
                # PERMANENTE: el índice rechazó ESTE doc (mapeo, etc.) → dead-letter
                self._muertos.append((aid, str(error)[:300], False))
            else:
                self._confirmados.append(aid)


# ------------------------------------------------------------------ administración


def aplicar_indice(config: Config, ruta_deploy: Path = Path("deploy")) -> None:
    """Aplica el index template + política ISM y crea el índice inicial con su alias.

    Idempotente: re-aplicar no rompe nada (la política existente se respeta)."""
    cliente = crear_cliente(config)

    template = json.loads((ruta_deploy / "mappings" / "archivos.json").read_text(encoding="utf-8"))
    # `rollover_alias` NO puede vivir en el JSON: el alias es configurable
    # (`indice_alias`) y los tests usan uno propio. Se inyecta aquí para que los
    # índices que CREE la ISM al rotar lo hereden — sin él, ISM no sabe sobre qué
    # alias rotar y la política queda muerta tras la primera rotación.
    plantilla = template.setdefault("template", {})
    plantilla.setdefault("settings", {})[
        "index.plugins.index_state_management.rollover_alias"
    ] = config.indice_alias
    cliente.indices.put_index_template(name="archivos", body=template)

    politica = json.loads(
        (ruta_deploy / "ism" / "politica_archivos.json").read_text(encoding="utf-8")
    )
    try:
        cliente.transport.perform_request("PUT", "/_plugins/_ism/policies/archivos", body=politica)
    except Exception as exc:
        codigo = getattr(exc, "status_code", None)
        detalle = str(exc)[:200]
        if codigo == 409:
            # Idempotente: la política ya existía. No es un problema.
            log.info("ism_ya_existe")
        elif codigo == 400 and "no handler found" in detalle:
            # OpenSearch SIN el plugin ISM (p. ej. el build de Homebrew en la Mac
            # de dev): la rotación por ciclo de vida no aplica, pero el índice
            # funciona igual. Esperado → info, no un warning que asuste.
            log.info("ism_plugin_ausente", detalle=detalle)
        else:
            # Cualquier otro fallo sí merece atención.
            log.warning("ism_no_aplicada", codigo=codigo, detalle=detalle)

    indice = indice_escritura(config)
    if not cliente.indices.exists(index=indice):
        cliente.indices.create(
            index=indice,
            body={
                # `is_write_index` es lo que convierte al alias en destino escribible
                # y lo que la ISM necesita para poder rotar. Sin él, escribir al
                # alias falla en cuanto tiene más de un índice — que es exactamente
                # lo que pasa en híbrido al restaurar el snapshot del otro nodo.
                "aliases": {config.indice_alias: {"is_write_index": True}},
                "settings": {
                    "index.plugins.index_state_management.rollover_alias": config.indice_alias
                },
            },
        )
    log.info("indice_aplicado", indice=indice, alias=config.indice_alias)


def buscar_por_nombre(config: Config, texto: str, limite: int = 20) -> list[dict[str, Any]]:
    """Búsqueda de demo por nombre (wildcard field — la decisión de costo del diseño)."""
    cliente = crear_cliente(config)
    respuesta = cliente.search(
        index=config.indice_alias,
        body={
            "size": limite,
            "query": {
                "wildcard": {"nombre": {"value": f"*{texto.lower()}*", "case_insensitive": True}}
            },
        },
    )
    return [hit["_source"] for hit in respuesta["hits"]["hits"]]
