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
    cuerpo = {
        "type": "s3",
        "settings": {
            "bucket": config.minio_bucket_snapshots,
            "endpoint": config.minio_endpoint,
            "protocol": "http",
            "path_style_access": "true",
        },
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

    existentes = set()
    try:
        existentes = set(cliente.indices.get_alias(index=f"{config.indice_alias}-*").keys())
    except Exception:
        pass

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
    log.info("restore_completo", indices=len(r.indices), ok=r.ok)
    return r


def replicar(config: Config) -> ResumenReplica:
    """La acción que le toca a ESTE nodo, según su papel en la topología."""
    if despliegue.de_config(config).es_archivo_maestro:
        return tomar_snapshot(config)
    return restaurar_ajenos(config)
