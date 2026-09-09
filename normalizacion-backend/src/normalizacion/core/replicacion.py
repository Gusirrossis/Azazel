"""⚙K16 — replicación entre nodos: el CONTRATO, no el mecanismo.

Azazel **no** implementa un motor de sincronización. Escribirlo en Python sería
reimplementar algo resuelto y crear una segunda fuente de verdad. Lo que hace es:

  · declarar QUÉ debe replicarse y en qué dirección,
  · orquestar el snapshot/restore del índice vía la API de OpenSearch,
  · y OBSERVAR el retraso, para que una réplica detenida no pase inadvertida.

MinIO replica los buckets por su cuenta (replicación de bucket nativa), y como el
snapshot de OpenSearch aterriza EN un bucket, un solo canal transporta las dos
cosas: no hay un segundo mecanismo que mantener.

Direcciones (asimétricas a propósito):

    índice   mac-01 ──▶ vps-01   el VPS sirve búsquedas sobre TODO el corpus
    blobs    vps-01 ──▶ mac-01   la copia permanente converge donde hay espacio
    frío     no se replica       lo más pesado y lo menos consultado

**El detalle que hace que esto no destruya datos:** el índice guardado en un
snapshot lleva su alias con `is_write_index: true`. Restaurarlo tal cual en el otro
nodo daría DOS índices de escritura para el mismo alias, y OpenSearch rechaza
escribir. Por eso se restaura con `include_aliases=False` y el alias se añade
después explícitamente como NO-escritura.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from normalizacion.core import despliegue
from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("replicacion")

REPOSITORIO = "azazel-snapshots"
_CLAVE_ULTIMO_SNAPSHOT = "replica_ultimo_snapshot"
_CLAVE_ULTIMO_RESTORE = "replica_ultimo_restore"


@dataclass
class ResumenReplica:
    accion: str
    ok: bool = False
    snapshot: str | None = None
    indices: list[str] = field(default_factory=list)
    motivo: str | None = None

    def como_dict(self) -> dict[str, Any]:
        return {
            "accion": self.accion, "ok": self.ok, "snapshot": self.snapshot,
            "indices": self.indices, "motivo": self.motivo,
        }


# ------------------------------------------------------------------ estado en `control`


def _marcar(config: Config, clave: str, payload: dict[str, Any]) -> None:
    """Sella el momento del último éxito. Best-effort: si la BD parpadea, la
    replicación ya ocurrió — no se deshace por no poder anotarla."""
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            conn.execute(
                "INSERT INTO control (clave, valor) VALUES (%s, %s)"
                " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor,"
                " actualizado_en = now()",
                (clave, json.dumps({**payload, "ts": datetime.now(UTC).isoformat()})),
            )
            conn.commit()
    except Exception as exc:
        log.warning("replica_sello_fallido", clave=clave, error=str(exc)[:150])


def _leer_sello(config: Config, clave: str) -> dict[str, Any] | None:
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            fila = conn.execute(
                "SELECT valor FROM control WHERE clave = %s", (clave,)
            ).fetchone()
    except Exception:
        return None
    if not fila:
        return None
    valor: dict[str, Any] = json.loads(fila[0])
    return valor


def lag_segundos(config: Config) -> float | None:
    """Segundos desde la última replicación EXITOSA de este nodo, o None si nunca.

    Es la métrica que impide que una réplica detenida pase inadvertida: sin ella,
    el nodo de servicio seguiría respondiendo búsquedas con datos viejos y nadie se
    enteraría hasta que alguien echara algo de menos."""
    clave = (
        _CLAVE_ULTIMO_SNAPSHOT
        if despliegue.de_config(config).es_archivo_maestro
        else _CLAVE_ULTIMO_RESTORE
    )
    sello = _leer_sello(config, clave)
    if not sello or "ts" not in sello:
        return None
    marca = datetime.fromisoformat(sello["ts"])
    return max(0.0, (datetime.now(UTC) - marca).total_seconds())


# ------------------------------------------------------------------ repositorio


def asegurar_repositorio(config: Config, cliente: Any | None = None) -> None:
    """Registra el repositorio de snapshots sobre el bucket de MinIO. Idempotente.

    El bucket es el MISMO canal que replica los blobs, así que el snapshot viaja
    solo: no hace falta un segundo transporte."""
    from normalizacion.core.indexador.opensearch import crear_cliente

    cliente = cliente or crear_cliente(config)
    # SOLO `bucket` y `client`. El endpoint de MinIO, el protocolo y el
    # `path_style_access` son settings de CLIENTE (`s3.client.default.*`), no de
    # repositorio: van en la configuración del nodo de OpenSearch (el compose los
    # pasa como -E) y las credenciales en su keystore. Ponerlos aquí no configura
    # nada y hace fallar la verificación con un
    # "path is not accessible on cluster-manager node" que no dice por qué.
    cuerpo = {
        "type": "s3",
        "settings": {"bucket": config.minio_bucket_snapshots, "client": "default"},
    }
    cliente.transport.perform_request(
        "PUT", f"/_snapshot/{REPOSITORIO}", body=cuerpo
    )
    log.info("repositorio_listo", repositorio=REPOSITORIO, bucket=config.minio_bucket_snapshots)


# ------------------------------------------------------------------ snapshot (emisor)


def _indices_propios(config: Config) -> str:
    """Patrón de los índices que ESTE nodo escribe (no los restaurados del otro)."""
    d = config.despliegue
    return f"{config.indice_alias}-*" if d.es_local() else f"{config.indice_alias}-{d.nodo_id}-*"


def tomar_snapshot(config: Config, cliente: Any | None = None) -> ResumenReplica:
    """Snapshot de los índices de ESTE nodo hacia el repositorio compartido."""
    from normalizacion.core.indexador.opensearch import crear_cliente

    r = ResumenReplica(accion="snapshot")
    cliente = cliente or crear_cliente(config)
    nombre = f"{config.despliegue.nodo_id}-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    try:
        asegurar_repositorio(config, cliente)
        cliente.transport.perform_request(
            "PUT",
            f"/_snapshot/{REPOSITORIO}/{nombre}",
            params={"wait_for_completion": "true"},
            body={
                "indices": _indices_propios(config),
                "ignore_unavailable": True,
                # Los alias del snapshot llevan `is_write_index: true`; al restaurar
                # se excluyen a propósito (ver `restaurar_ajenos`).
                "include_global_state": False,
            },
        )
    except Exception as exc:
        r.motivo = f"{type(exc).__name__}: {exc}"[:250]
        log.warning("snapshot_fallido", error=r.motivo)
        return r
    r.ok, r.snapshot = True, nombre
    _marcar(config, _CLAVE_ULTIMO_SNAPSHOT, {"snapshot": nombre})
    log.info("snapshot_tomado", snapshot=nombre, patron=_indices_propios(config))
    return r


# ------------------------------------------------------------------ restore (receptor)


#: Espera máxima a que el índice restaurado quede VERDE. Alta a propósito: con 24,4 GB
#: el shard tarda ~12 min y crece con el corpus. El viejo sigue sirviendo mientras tanto,
#: así que esperar no le cuesta nada a nadie.
_ESPERA_VERDE = "60m"


#: Sufijo de la ranura de repuesto. Cada índice de origen tiene SU par.
_SUF_RANURA = "-r"


def _ranura_alterna(indice: str) -> str:
    """La otra ranura del par blue/green DE ESE índice: `X` ↔ `X-r`.

    El par se deriva del nombre completo, no del número final. Alternar
    `…-000001` ↔ `…-000002` parecía lo natural hasta que el emisor ROTÓ sus índices
    (ISM): en cuanto la luna tuvo `…-000001` y `…-000002` de verdad, restaurar el
    primero escribía sobre el segundo y viceversa. Se perdió una copia entera así.

    Dos nombres fijos por índice y no uno con marca de tiempo: el juego de índices que
    puede existir queda acotado y no se acumulan residuos de ciclos viejos.
    """
    if indice.endswith(_SUF_RANURA):
        return indice[: -len(_SUF_RANURA)]
    return indice + _SUF_RANURA


def _en_alias(cliente: Any, indice: str, alias: str) -> bool:
    try:
        return alias in (cliente.indices.get_alias(index=indice)[indice].get("aliases") or {})
    except Exception:
        return False


def restaurar_ajenos(
    config: Config,
    cliente: Any | None = None,
    *,
    refrescar: bool = False,
    repositorio: str | None = None,
) -> ResumenReplica:
    """Restaura los índices de los OTROS nodos y los añade al alias como lectura.

    Cinco precauciones, cada una por un fallo concreto:

    1. `include_aliases=False` — el índice del snapshot trae su alias con
       `is_write_index: true`. Restaurarlo daría dos índices de escritura para el
       mismo alias y OpenSearch rechazaría toda escritura de este nodo.
    2. Sólo se restauran índices que NO son de este nodo: restaurar el propio lo
       sobrescribiría con una copia vieja. Es el fallo más caro y más silencioso.
    3. De cada índice se restaura el snapshot **más reciente** que lo contiene.
       Recorrer los snapshots en orden y dar el índice por restaurado en el primero
       que lo trae dejaba SIEMPRE el más ANTIGUO: contra un emisor que fotografía su
       índice cada ciclo —el caso normal de una luna— lo nuevo no llegaba nunca, sin
       error ni aviso. Síntoma observado: el emisor con 24 documentos y el receptor
       recibiendo 2.
    4. Un índice ya presente se salta salvo `refrescar=True`, porque un restore
       sobre un índice abierto falla y retirarlo es destructivo. Con replicación
       continua hace falta refrescar (la versión nueva sustituye a la vieja); en un
       restore puntual, no. Por eso se pide explícitamente y no ocurre por sorpresa.
    5. El refresco es BLUE/GREEN, no borrar-y-restaurar-encima. Con 24,4 GB el shard
       tarda ~12 min en recuperarse y el ciclo corre cada 20: la copia estaba a medias
       más de la mitad del tiempo, y una búsqueda sobre el alias devolvía MENOS
       resultados sin marcar fallo (`_shards: total 4, successful 3, failed 0`). Se
       restaura en la ranura libre, se espera a verde y se cambia el alias de golpe.
    """
    from normalizacion.core.indexador.opensearch import crear_cliente

    r = ResumenReplica(accion="restore")
    cliente = cliente or crear_cliente(config)
    propio = _indices_propios(config).rstrip("*")
    # Cada luna trae sus snapshots en SU repositorio (`azazel-snapshots-lilith`,
    # `azazel-snapshots`…): quien llama puede decir cuál, y entonces también es quien
    # lo ha registrado, así que aquí no se toca.
    repo = repositorio or REPOSITORIO
    try:
        if repositorio is None:
            asegurar_repositorio(config, cliente)
        snapshots = cliente.transport.perform_request("GET", f"/_snapshot/{repo}/_all").get(
            "snapshots", []
        )
    except Exception as exc:
        r.motivo = f"{type(exc).__name__}: {exc}"[:250]
        log.warning("restore_sin_repositorio", error=r.motivo)
        return r

    # Qué índices hay ya. En un clúster virgen el alias aún no existe y la consulta
    # da 404: eso NO es un fallo, sólo significa que no hay nada que saltarse.
    existentes: set[str] = set()
    with contextlib.suppress(Exception):
        existentes = set(cliente.indices.get_alias(index=f"{config.indice_alias}-*").keys())

    # Índice ajeno → el snapshot MÁS RECIENTE que lo contiene. El nombre lleva
    # `<nodo>-<AAAAMMDD-HHMMSS>`, así que ordena bien como cadena (precaución 3).
    ultimo_por_indice: dict[str, str] = {}
    for snap in snapshots:
        if snap.get("state") not in (None, "SUCCESS"):
            continue  # un snapshot a medias o fallido no es una fuente válida
        nombre = str(snap.get("snapshot", ""))
        for indice in snap.get("indices", []):
            if indice.startswith(propio):
                continue
            if nombre > ultimo_por_indice.get(indice, ""):
                ultimo_por_indice[indice] = nombre

    for indice, nombre in sorted(ultimo_por_indice.items()):
        # Cuál de las dos ranuras está sirviendo AHORA (si alguna).
        alterna = _ranura_alterna(indice)
        sirviendo = next(
            (i for i in (indice, alterna) if _en_alias(cliente, i, config.indice_alias)), None
        )
        if sirviendo is not None and not refrescar:
            continue  # ya hay una copia servible y no se pidió refrescar (precaución 4)

        # BLUE/GREEN (precaución 5). Antes se borraba el índice VIVO y se restauraba
        # encima. Durante todo el restore —12 min con 24,4 GB, medido en la matriz— el
        # alias apuntaba a un índice a medias, y una búsqueda devolvía MENOS resultados
        # afirmando que todo fue bien: `_shards: total 4, successful 3, failed 0`. Quien
        # federa recibía menos datos sin un solo error. Ahora se restaura en la otra
        # ranura, se espera a VERDE y se cambia el alias en UNA operación atómica: la
        # copia anterior sirve hasta el último instante y la ventana desaparece.
        if sirviendo:
            destino = _ranura_alterna(sirviendo)
        else:
            # Sin copia sirviendo, el nombre propio suele estar libre. Pero puede haber
            # un HUÉRFANO de un ciclo roto: restaurar sobre él falla con
            # "an open index with same name already exists" y el ciclo entero muere.
            destino = (
                indice if not cliente.indices.exists(index=indice) else _ranura_alterna(indice)
            )
        try:
            # Sin `suppress`: si la ranura no se puede liberar, el restore va a fallar
            # de todas formas y con un error que no dice por qué. Antes esto se tragaba
            # el fallo y la excepción aparecía tres líneas después, desorientando.
            if cliente.indices.exists(index=destino):
                cliente.indices.delete(index=destino)
            # `wait_for_completion=false`: la petición vuelve en cuanto el restore se
            # ACEPTA. Con `true` la conexión se queda abierta mientras dura —minutos
            # con decenas de GB— y el cliente la corta a los 30 s con un
            # ConnectionTimeout que parece un fallo y no lo es. Medido: el ciclo de la
            # luna de Lilith moría así con el índice a medio restaurar. Quien espera de
            # verdad es `cluster.health` de abajo, que sí lleva su `request_timeout`.
            cliente.transport.perform_request(
                "POST",
                f"/_snapshot/{repo}/{nombre}/_restore",
                params={"wait_for_completion": "false"},
                body={
                    "indices": indice,
                    "include_aliases": False,
                    "rename_pattern": ".+",
                    "rename_replacement": destino,
                },
            )
            # `wait_for_completion` vuelve cuando el shard está ASIGNADO, no cuando
            # terminó de recuperarse. Sin esta espera el swap metería en el alias
            # exactamente el índice a medias que se quiere evitar.
            #
            # `request_timeout` va aparte del `timeout`: el primero es el del CLIENTE
            # (30 s por defecto) y el segundo el del servidor. Sin el primero, esperar
            # 60 min de servidor se corta a los 30 s con un ConnectionTimeout que
            # parece un fallo y no lo es — el mismo error que hacía cantar 16 de 43
            # ciclos como fallidos cuando los snapshots salían todos bien.
            # El restore ya fue aceptado, pero el índice puede tardar un instante en
            # aparecer. Se espera a que EXISTA comprobándolo, no durmiendo a ciegas.
            for _ in range(12):
                if cliente.indices.exists(index=destino):
                    break
                time.sleep(5)
            cliente.cluster.health(
                index=destino,
                wait_for_status="green",
                timeout=_ESPERA_VERDE,
                request_timeout=3900,
            )
            # NO-escritura: el índice de escritura de este alias es el propio.
            acciones: list[dict[str, Any]] = [
                {
                    "add": {
                        "index": destino,
                        "alias": config.indice_alias,
                        "is_write_index": False,
                    }
                }
            ]
            if sirviendo and sirviendo != destino:
                acciones.append({"remove": {"index": sirviendo, "alias": config.indice_alias}})
            cliente.indices.update_aliases(body={"actions": acciones})
            if sirviendo and sirviendo != destino:
                with contextlib.suppress(Exception):
                    cliente.indices.delete(index=sirviendo)
                existentes.discard(sirviendo)
            existentes.add(destino)
            r.indices.append(destino)
        except Exception as exc:
            r.motivo = f"{type(exc).__name__}: {exc}"[:250]
            log.warning("restore_parcial", snapshot=nombre, indice=destino, error=r.motivo)
            continue

    r.ok = r.motivo is None
    if r.ok:
        _marcar(config, _CLAVE_ULTIMO_RESTORE, {"indices": r.indices})
    if r.indices and despliegue.de_config(config).corre_entidades:
        _invalidar_cursor_backfill(config, r)
        # Enganche: los docs recién restaurados son EXACTAMENTE lo que el resolvedor
        # de entidades aún no vio. Con el cursor ya invalidado (rescan completo
        # idempotente por `entidad_id`), disparar el backfill aquí hace que el mismo
        # timer de réplica que ya corre gobierne las entidades — sin demonios ni
        # timers nuevos. El propio `lanzar_en_fondo` es no-op si ya hay uno en curso
        # o si el gobernador K15 ve poca RAM, así que es seguro llamarlo por ciclo.
        from normalizacion.entidades import backfill

        resultado = backfill.lanzar_en_fondo(config, reiniciar=True)
        log.info("backfill_por_replica", **resultado)
    log.info("restore_completo", indices=len(r.indices), ok=r.ok)
    return r


def _invalidar_cursor_backfill(config: Config, r: ResumenReplica) -> None:
    """Un restore invalida el cursor del backfill de entidades. Hay que borrarlo.

    El backfill barre el índice ordenado por `archivo_id` con `search_after`, y
    guarda su avance en `control`. Pero `archivo_id` es un sha256: se distribuye
    UNIFORME. Los documentos que llegan restaurados caen repartidos por todo el
    espacio de ids, así que **en promedio la mitad de cada lote replicado aterriza
    por detrás del cursor** — y `search_after` sólo avanza, nunca vuelve. Esas
    personas no se resolverían jamás.

    Borrar el cursor hace que la siguiente pasada barra desde cero. Es más caro
    (un escaneo completo del índice) pero es idempotente —el upsert por
    `entidad_id` no duplica— y es la única forma correcta con el mapping actual.
    La solución de fondo, un campo `indexado_en` monótono para barrer por tiempo en
    vez de por hash, cambia el índice y obliga a reindexar: no está en este plan.
    """
    from normalizacion.entidades.backfill import _CURSOR_CLAVE

    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            borradas = conn.execute(
                "DELETE FROM control WHERE clave = %s", (_CURSOR_CLAVE,)
            ).rowcount
            conn.commit()
    except Exception as exc:  # el restore ya ocurrió: no se deshace por esto
        log.warning("cursor_backfill_no_invalidado", error=str(exc)[:150])
        r.motivo = (r.motivo or "") + " · cursor del backfill NO invalidado"
        return
    if borradas:
        log.info("cursor_backfill_invalidado", indices_restaurados=len(r.indices))


def replicar(config: Config, *, refrescar: bool = False) -> ResumenReplica:
    """La acción que le toca a ESTE nodo, según su papel en la topología.

    `refrescar` sólo pinta en el receptor: sustituye la copia local de un índice
    ajeno por la del snapshot más reciente. Es lo que necesita una replicación
    periódica; en un restore puntual sobra."""
    if despliegue.de_config(config).es_archivo_maestro:
        return tomar_snapshot(config)
    return restaurar_ajenos(config, refrescar=refrescar)
