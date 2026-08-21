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


def restaurar_ajenos(config: Config, cliente: Any | None = None) -> ResumenReplica:
    """Restaura los índices de los OTROS nodos y los añade al alias como lectura.

    Tres precauciones, cada una por un fallo concreto:

    1. `include_aliases=False` — el índice del snapshot trae su alias con
       `is_write_index: true`. Restaurarlo daría dos índices de escritura para el
       mismo alias y OpenSearch rechazaría toda escritura de este nodo.
    2. Sólo se restauran índices que NO son de este nodo: restaurar el propio lo
       sobrescribiría con una copia vieja. Es el fallo más caro y más silencioso.
    3. Los índices ya presentes se saltan (un restore sobre un índice abierto
       falla); para refrescarlos hace falta cerrarlos o borrarlos primero, y eso
       es una decisión del operador, no un efecto colateral."""
    from normalizacion.core.indexador.opensearch import crear_cliente

    r = ResumenReplica(accion="restore")
    cliente = cliente or crear_cliente(config)
    propio = _indices_propios(config).rstrip("*")
    try:
        asegurar_repositorio(config, cliente)
        snapshots = cliente.transport.perform_request(
            "GET", f"/_snapshot/{REPOSITORIO}/_all"
        ).get("snapshots", [])
    except Exception as exc:
        r.motivo = f"{type(exc).__name__}: {exc}"[:250]
        log.warning("restore_sin_repositorio", error=r.motivo)
        return r

    # Qué índices hay ya. En un clúster virgen el alias aún no existe y la consulta
    # da 404: eso NO es un fallo, sólo significa que no hay nada que saltarse.
    existentes: set[str] = set()
    with contextlib.suppress(Exception):
        existentes = set(cliente.indices.get_alias(index=f"{config.indice_alias}-*").keys())

    for snap in sorted(snapshots, key=lambda s: str(s.get("snapshot", ""))):
        ajenos = [
            i
            for i in snap.get("indices", [])
            if not i.startswith(propio) and i not in existentes
        ]
        if not ajenos:
            continue
        try:
            cliente.transport.perform_request(
                "POST",
                f"/_snapshot/{REPOSITORIO}/{snap['snapshot']}/_restore",
                params={"wait_for_completion": "true"},
                body={"indices": ",".join(ajenos), "include_aliases": False},
            )
            for indice in ajenos:
                # NO-escritura: el índice de escritura de este alias es el propio.
                cliente.indices.put_alias(
                    index=indice, name=config.indice_alias, body={"is_write_index": False}
                )
                existentes.add(indice)
                r.indices.append(indice)
        except Exception as exc:
            r.motivo = f"{type(exc).__name__}: {exc}"[:250]
            log.warning("restore_parcial", snapshot=snap.get("snapshot"), error=r.motivo)
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


def replicar(config: Config) -> ResumenReplica:
    """La acción que le toca a ESTE nodo, según su papel en la topología."""
    if despliegue.de_config(config).es_archivo_maestro:
        return tomar_snapshot(config)
    return restaurar_ajenos(config)
