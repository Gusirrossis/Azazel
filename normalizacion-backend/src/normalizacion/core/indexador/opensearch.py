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
    """Cliente de OpenSearch, con o sin plugin de seguridad.

    `use_ssl` se deduce del esquema de la URL en vez de estar fijo: en dev el
    clúster va sin seguridad (http) y en producción CON ella (https + auth). Antes
    estaba cableado a False, así que contra un clúster seguro el cliente fallaba
    entero —búsqueda, sink y backfill— reportando sólo "no responde"."""
    from opensearchpy import OpenSearch

    usa_tls = config.opensearch_url.lower().startswith("https://")
    extra: dict[str, Any] = {}
    if config.opensearch_usuario:
        extra["http_auth"] = (config.opensearch_usuario, config.opensearch_password)
    return OpenSearch(
        hosts=[config.opensearch_url],
        use_ssl=usa_tls,
        verify_certs=config.opensearch_verificar_certs,
        ssl_show_warn=False,
        timeout=30,
        **extra,
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


def _ism_disponible(cliente: Any) -> bool:
    """¿El clúster trae el plugin ISM? El build de Homebrew de la Mac de dev NO lo
    trae. Sin ISM, `index.plugins.index_state_management.rollover_alias` es un
    setting DESCONOCIDO y OpenSearch rechaza la plantilla/creación entera (400
    settings_exception). Como sin ISM tampoco hay rotación, el ajuste se omite."""
    try:
        return any(
            "index-management" in (p.get("component") or "")
            for p in cliente.cat.plugins(format="json")
        )
    except Exception:
        return False


def aplicar_indice(config: Config, ruta_deploy: Path = Path("deploy")) -> None:
    """Aplica el index template + política ISM y crea el índice inicial con su alias.

    Idempotente: re-aplicar no rompe nada (la política existente se respeta)."""
    cliente = crear_cliente(config)
    ism = _ism_disponible(cliente)

    template = json.loads((ruta_deploy / "mappings" / "archivos.json").read_text(encoding="utf-8"))
    # `rollover_alias` NO puede vivir en el JSON: el alias es configurable
    # (`indice_alias`) y los tests usan uno propio. Se inyecta aquí para que los
    # índices que CREE la ISM al rotar lo hereden — sin él, ISM no sabe sobre qué
    # alias rotar y la política queda muerta tras la primera rotación. Sólo CON ISM:
    # sin el plugin el setting es desconocido y tumba el put_index_template entero.
    plantilla = template.setdefault("template", {})
    if ism:
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
        cuerpo: dict[str, Any] = {
            # `is_write_index` es lo que convierte al alias en destino escribible
            # y lo que la ISM necesita para poder rotar. Sin él, escribir al
            # alias falla en cuanto tiene más de un índice — que es exactamente
            # lo que pasa en híbrido al restaurar el snapshot del otro nodo.
            "aliases": {config.indice_alias: {"is_write_index": True}},
        }
        if ism:
            cuerpo["settings"] = {
                "index.plugins.index_state_management.rollover_alias": config.indice_alias
            }
        cliente.indices.create(index=indice, body=cuerpo)
    log.info("indice_aplicado", indice=indice, alias=config.indice_alias)


def reindexar_a_mapping_nuevo(
    config: Config, ruta_deploy: Path = Path("deploy"), *, borrar_viejo: bool = False
) -> dict[str, Any]:
    """Migra el alias a un índice NUEVO con la plantilla actual, copiando los documentos.

    Por qué hace falta un índice nuevo: el mapping de un índice existente es casi
    inmutable. `put_index_template` solo afecta a los que se CREEN después, y reindexar
    documentos uno a uno tampoco lo cambia — así que un analizador nuevo (el español
    con `asciifolding`) no llega jamás al corpus ya indexado. Sin este paso, la mejora
    de búsqueda simplemente no ocurre, aunque todo lo demás se despliegue bien.

    Se usa el `_reindex` del propio OpenSearch en vez de reingerir desde el almacén:
    copia servidor-a-servidor sin volver a extraer ni a pasar OCR, que sobre 19.656
    contenidos únicos es la diferencia entre minutos y días.

    REVERSIBLE: el índice viejo se conserva (sale del alias, pero sigue ahí) salvo que
    se pida `borrar_viejo`. Si algo sale mal, se devuelve el alias y no se perdió nada.
    """
    cliente = crear_cliente(config)
    alias = config.indice_alias

    # 1) La plantilla nueva primero: el índice que se cree después la hereda.
    aplicar_indice(config, ruta_deploy)

    viejos = sorted(cliente.indices.get_alias(name=alias).keys())
    if not viejos:
        raise RuntimeError(f"el alias {alias!r} no apunta a ningún índice")

    # 2) Nombre del nuevo: se conserva el prefijo del que hoy escribe y se sube el
    #    contador, para no romper el patrón que la ISM usa al rotar.
    escritura = indice_escritura(config)
    base, _, sufijo = escritura.rpartition("-")
    siguiente = f"{base}-{int(sufijo) + 1:06d}" if sufijo.isdigit() else f"{escritura}-000002"
    if cliente.indices.exists(index=siguiente):
        raise RuntimeError(f"{siguiente} ya existe: revísalo antes de reindexar")

    cliente.indices.create(index=siguiente, body={})
    log.info("reindex_indice_creado", indice=siguiente, desde=viejos)

    # 3) Copia. `wait_for_completion=False` devuelve una tarea: un corpus grande
    #    excede cualquier timeout HTTP razonable y no se puede esperar en línea.
    tarea = cliente.reindex(
        body={"source": {"index": alias}, "dest": {"index": siguiente}},
        wait_for_completion=False,
        request_timeout=120,
    )
    return {
        "indice_nuevo": siguiente,
        "indices_viejos": viejos,
        "tarea": tarea.get("task"),
        "borrar_viejo": borrar_viejo,
        "siguiente_paso": (
            f"Vigila con GET _tasks/{tarea.get('task')}; cuando termine, cambia el alias"
            f" con `norm reindexar --finalizar {siguiente}`."
        ),
    }


def finalizar_reindex(
    config: Config, indice_nuevo: str, *, borrar_viejo: bool = False
) -> dict[str, Any]:
    """Mueve el alias al índice nuevo en una operación ATÓMICA.

    Las dos acciones —quitar el alias de los viejos y ponerlo en el nuevo— van en la
    MISMA llamada a `_aliases`: si se hicieran por separado habría un instante sin
    índice de escritura, y todo lo que llegara en ese hueco se perdería.
    """
    cliente = crear_cliente(config)
    alias = config.indice_alias
    viejos = [i for i in sorted(cliente.indices.get_alias(name=alias).keys()) if i != indice_nuevo]

    cuenta_nueva = int(cliente.count(index=indice_nuevo)["count"])
    cuenta_vieja = sum(int(cliente.count(index=i)["count"]) for i in viejos) if viejos else 0
    if cuenta_nueva < cuenta_vieja:
        raise RuntimeError(
            f"{indice_nuevo} tiene {cuenta_nueva} documentos y los viejos {cuenta_vieja}:"
            " la copia no ha terminado (o falló). NO se mueve el alias."
        )

    acciones: list[dict[str, Any]] = [
        {"add": {"index": indice_nuevo, "alias": alias, "is_write_index": True}}
    ]
    acciones += [{"remove": {"index": i, "alias": alias}} for i in viejos]
    cliente.indices.update_aliases(body={"actions": acciones})
    log.info("reindex_alias_movido", alias=alias, nuevo=indice_nuevo, quitados=viejos)

    borrados: list[str] = []
    if borrar_viejo:
        for i in viejos:
            cliente.indices.delete(index=i)
            borrados.append(i)
        log.warning("reindex_viejos_borrados", indices=borrados)

    return {
        "alias": alias,
        "indice_nuevo": indice_nuevo,
        "documentos": cuenta_nueva,
        "viejos_fuera_del_alias": viejos,
        "viejos_borrados": borrados,
    }


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
