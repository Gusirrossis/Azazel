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
from collections.abc import Callable
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
#: `replica_huella:<repo>:<índice de origen>` — qué contenido sirve ahora la copia local.
_CLAVE_HUELLA = "replica_huella"


@dataclass
class ResumenReplica:
    accion: str
    ok: bool = False
    snapshot: str | None = None
    indices: list[str] = field(default_factory=list)
    motivo: str | None = None
    #: Índices que ya servían el mismo contenido que el snapshot y NO se restauraron.
    sin_cambios: list[str] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return {
            "accion": self.accion, "ok": self.ok, "snapshot": self.snapshot,
            "indices": self.indices, "motivo": self.motivo, "sin_cambios": self.sin_cambios,
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


def _clave_de_luna(repo: str) -> str:
    """Sello de restore de UNA luna. Cada luna trae sus snapshots en su repositorio."""
    return f"{_CLAVE_ULTIMO_RESTORE}:{repo}"


def lag_por_luna(config: Config) -> dict[str, float]:
    """Segundos desde el último restore EXITOSO de cada repositorio (= cada luna).

    Vacío si la BD no responde o si aún no hay sellos por luna (los que escribía el
    código anterior eran uno solo para todas). Cada ciclo siembra la clave de las lunas
    registradas que aún no la tienen (ver `_sembrar_lunas`), así que retirar una luna
    es quitar su repositorio de la matriz (`DELETE _snapshot/<repo>`, no borra datos) Y
    borrar su fila: sólo lo segundo, y el ciclo siguiente la vuelve a sembrar."""
    prefijo = _clave_de_luna("")
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            filas = conn.execute(
                "SELECT clave, valor FROM control WHERE position(%s in clave) = 1",
                (prefijo,),
            ).fetchall()
    except Exception:
        return {}
    ahora = datetime.now(UTC)
    lags: dict[str, float] = {}
    for clave, valor in filas:
        with contextlib.suppress(Exception):
            marca = datetime.fromisoformat(json.loads(valor)["ts"])
            lags[clave[len(prefijo):]] = max(0.0, (ahora - marca).total_seconds())
    return lags


def lag_segundos(config: Config) -> float | None:
    """Segundos desde la última replicación EXITOSA de este nodo, o None si nunca.

    Es la métrica que impide que una réplica detenida pase inadvertida: sin ella,
    el nodo de servicio seguiría respondiendo búsquedas con datos viejos y nadie se
    enteraría hasta que alguien echara algo de menos.

    Si este nodo recibe lunas, manda la MÁS atrasada. Con un sello único para todas, el
    éxito de una tapaba el fallo de otra: el 24-09 storage lo reescribía cada ~20 min
    mientras Lilith fallaba 62 de 64 ciclos.

    Va antes que el papel a propósito: la matriz es `online` (su propio archivo maestro)
    y recibe a las lunas llamando a `restaurar_ajenos` directamente. Mirando sólo el
    papel leía `replica_ultimo_snapshot`, que ahí no se escribe nunca: el exportador de la
    matriz publicaba -1 («nunca ha replicado») el 25-09 con las lunas replicando.

    Sin sellos por luna (ningún ciclo desde que se desplegó esto) se lee el sello del
    papel y, en un maestro que no tiene, el único de restore de antes."""
    por_luna = lag_por_luna(config)
    if por_luna:
        return max(por_luna.values())
    claves = (
        (_CLAVE_ULTIMO_SNAPSHOT, _CLAVE_ULTIMO_RESTORE)
        if despliegue.de_config(config).es_archivo_maestro
        else (_CLAVE_ULTIMO_RESTORE,)
    )
    for clave in claves:
        sello = _leer_sello(config, clave)
        if sello and "ts" in sello:
            marca = datetime.fromisoformat(sello["ts"])
            return max(0.0, (datetime.now(UTC) - marca).total_seconds())
    return None


def _sembrar_lunas(config: Config, cliente: Any) -> None:
    """Da su sello a cada luna registrada que aún no lo tiene, sin pisar ninguno.

    El sello por luna nace vacío para todas. Sin sembrar, en cuanto sellaba la primera
    el lag pasaba a ser el máximo de las que YA sellaron, y una luna que no llegara a
    su primer ciclo bueno desaparecía del cálculo: con los dos cron pausados y
    reanudados a mano, storage reanudado y Lilith olvidada daba lag de storage y
    ninguna alerta, justo el fallo que el sello por luna viene a cerrar.

    El valor sembrado es el sello único ANTERIOR a este ciclo (el último éxito de
    alguna luna, cota optimista de la que falta) o, si no lo hay, ahora: la luna
    empieza a envejecer y avisa a las 2 h si no sella. Las lunas son los repositorios
    `azazel-snapshots[-*]` registrados en la matriz; los otros que hay (`snap-mac01`,
    `snap-vps01old`, copias viejas medidas el 25-09) no reciben a nadie y alertarían
    para siempre."""
    try:
        registrados = cliente.transport.perform_request("GET", "/_snapshot")
        lunas = sorted(
            str(r) for r in registrados if r == REPOSITORIO or str(r).startswith(REPOSITORIO + "-")
        )
    except Exception as exc:
        log.warning("replica_lunas_ilegibles", error=str(exc)[:150])
        return
    if not lunas:
        return
    anterior = _leer_sello(config, _CLAVE_ULTIMO_RESTORE) or {}
    ts = anterior.get("ts") or datetime.now(UTC).isoformat()
    valor = json.dumps({"sembrado_de": _CLAVE_ULTIMO_RESTORE, "ts": ts})
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            for repo in lunas:
                conn.execute(
                    "INSERT INTO control (clave, valor) VALUES (%s, %s)"
                    " ON CONFLICT (clave) DO NOTHING",
                    (_clave_de_luna(repo), valor),
                )
            conn.commit()
    except Exception as exc:
        log.warning("replica_siembra_fallida", error=str(exc)[:150])


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


# ------------------------------------------------------------------ huella: no recopiar lo idéntico
#
# Medido en la matriz el 24/25-09: Lilith no escribe en su índice desde el 11-09 y aun
# así cada ciclo volvía a restaurar sus 76,7 GB (3 índices) con el disco mecánico al
# 60-80 %. Los tres últimos snapshots tenían los mismos ficheros y bytes por shard y 0
# ficheros nuevos. La huella compara eso, que OpenSearch expone sin leer ni un documento.


def _clave_huella(repo: str, indice: str) -> str:
    return f"{_CLAVE_HUELLA}:{repo}:{indice}"


#: Pausa antes del único reintento de una lectura de metadatos.
_PAUSA_REINTENTO = 3.0


def _es_pasajero(exc: Exception) -> bool:
    """¿Fallo de la PETICIÓN (red, timeout, 5xx, 429) y no del dato (404, 400)?

    opensearch-py marca los fallos de conexión con `status_code` "N/A" o "TIMEOUT" y los
    del servidor con el código HTTP. Un parpadeo de MinIO llega como 500 del repositorio."""
    estado = getattr(exc, "status_code", None)
    if estado in ("N/A", "TIMEOUT", 408, 429):
        return True
    if isinstance(estado, int):
        return estado >= 500
    return isinstance(exc, (ConnectionError, TimeoutError))


def _leer_metadatos(peticion: Callable[[], Any], que: str) -> Any:
    """Una lectura de metadatos con UN reintento si falló la petición, no el dato.

    Cada lectura que falla decide «ante la duda, restaura»: con este disco, una sola
    petición perdida costaba 35,9 GB y decenas de minutos de E/S al tope (Lilith
    000001). Un 404 no se reintenta: el snapshot se purgó y no va a volver."""
    try:
        return peticion()
    except Exception as exc:
        if not _es_pasajero(exc):
            raise
        log.info("replica_reintento", que=que, error=str(exc)[:150])
    time.sleep(_PAUSA_REINTENTO)
    return peticion()


def _huella_snapshot(
    cliente: Any, repo: str, snapshot: str, indice: str, estados: dict[str, Any]
) -> dict[str, Any] | None:
    """Huella de UN índice dentro de un snapshot, o None si no se puede saber.

    `shards`: por shard, `[ficheros, bytes]` totales. Lucene nunca reescribe un fichero:
    cualquier escritura deja segmentos y un `segments_N` nuevos, así que ficheros y bytes
    cambian salvo coincidencia exacta al byte. Contra esa coincidencia está `nuevos`: los
    ficheros que ESTE snapshot tuvo que subir porque el repositorio no los tenía
    (`incremental`). Sale de `_snapshot/<repo>/<snap>/_status`, que lee sólo los
    metadatos de cada shard en el repositorio: 0,2 s (2 s en frío) medido en la matriz.

    `estados` guarda la respuesta por snapshot: uno trae varios índices y así se pide una
    vez por ciclo, no una por índice."""
    if snapshot not in estados:
        try:
            estados[snapshot] = _leer_metadatos(
                lambda: cliente.transport.perform_request(
                    "GET", f"/_snapshot/{repo}/{snapshot}/_status"
                ),
                "status",
            )
        except Exception as exc:
            log.warning("replica_huella_ilegible", snapshot=snapshot, error=str(exc)[:150])
            estados[snapshot] = None
    try:
        snap = next(
            s for s in estados[snapshot]["snapshots"] if s.get("snapshot") == snapshot
        )
        shards: dict[str, list[int]] = {}
        nuevos = 0
        for sid, shard in snap["indices"][indice]["shards"].items():
            if shard.get("stage") != "DONE":
                return None
            total = shard["stats"]["total"]
            shards[str(sid)] = [int(total["file_count"]), int(total["size_in_bytes"])]
            # Sin el dato no se presume que no subió nada: se cuenta como cambio.
            nuevos += int((shard["stats"].get("incremental") or {}).get("file_count", 1))
    except Exception:
        return None
    return {"shards": shards, "nuevos": nuevos} if shards else None


def _uuid_indice(cliente: Any, indice: str) -> str | None:
    try:
        ajustes = _leer_metadatos(
            lambda: cliente.indices.get_settings(index=indice, name="index.uuid"), "uuid"
        )
        return str(ajustes[indice]["settings"]["index"]["uuid"]) or None
    except Exception:
        return None


def _snapshot_de_origen(cliente: Any, vivo: str, repo: str, indice: str) -> str | None:
    """De qué snapshot se restauró `vivo`, según el propio clúster (`_recovery`).

    Es lo que permite saltarse el primer ciclo tras desplegar, cuando aún no hay huella
    guardada: sin esto ese ciclo recopiaría los 76,7 GB una vez más. Se pierde si
    OpenSearch reinicia (la recuperación pasa a EXISTING_STORE), y entonces None."""
    try:
        recuperacion = _leer_metadatos(
            lambda: cliente.transport.perform_request("GET", f"/{vivo}/_recovery"), "recovery"
        )
        shards = recuperacion[vivo]["shards"]
        fuentes = set()
        for shard in shards:
            if not shard.get("primary"):
                continue
            origen = shard.get("source") or {}
            if (
                shard.get("type") != "SNAPSHOT"
                or shard.get("stage") != "DONE"
                or origen.get("repository") != repo
                or origen.get("index") != indice
            ):
                return None
            fuentes.add(str(origen.get("snapshot") or ""))
    except Exception:
        return None
    if len(fuentes) != 1:
        return None  # sin primarios, o shards de snapshots distintos: no se sabe
    return fuentes.pop() or None


def _huella_viva(
    config: Config, cliente: Any, repo: str, indice: str, vivo: str, estados: dict[str, Any]
) -> dict[str, Any] | None:
    """Qué contenido sirve AHORA `vivo`, o None si no hay forma fiable de saberlo.

    La huella guardada sólo vale si sigue siendo el MISMO índice físico (mismo uuid): un
    restore a mano o un ciclo del código anterior pueden haber puesto otro con el mismo
    nombre, y fiarse del nombre saltaría un cambio real."""
    guardada = _leer_sello(config, _clave_huella(repo, indice))
    if (
        guardada
        and guardada.get("destino") == vivo
        and guardada.get("uuid")
        and guardada.get("uuid") == _uuid_indice(cliente, vivo)
    ):
        return guardada
    origen = _snapshot_de_origen(cliente, vivo, repo, indice)
    if origen is None:
        return None
    huella = _huella_snapshot(cliente, repo, origen, indice, estados)
    if huella is None:
        return None
    return {"snapshot": origen, "shards": huella["shards"], "destino": vivo, "nueva": True}


def _mismo_contenido(
    viva: dict[str, Any], snapshot: str, candidata: dict[str, Any] | None
) -> bool:
    """¿El snapshot candidato trae exactamente lo que ya sirve la copia local?

    Hacen falta las DOS señales: totales iguales y ningún fichero nuevo en el candidato.
    Si el candidato subió algo es que cambió respecto a su anterior, y unos totales que
    coincidan entonces serían casualidad, no identidad: se restaura."""
    if viva.get("snapshot") == snapshot:
        return True
    if candidata is None:
        return False
    return bool(candidata["nuevos"] == 0 and viva.get("shards") == candidata["shards"])


def _guardar_huella(
    config: Config,
    cliente: Any,
    repo: str,
    indice: str,
    destino: str,
    snapshot: str,
    shards: dict[str, list[int]],
) -> None:
    """Anota qué contenido sirve `destino`, atado a su uuid. Sin uuid no se anota: una
    huella que no se puede atar a un índice físico no sirve para saltarse nada."""
    uuid = _uuid_indice(cliente, destino)
    if uuid is None:
        log.warning("replica_huella_sin_uuid", indice=destino)
        return
    _marcar(
        config,
        _clave_huella(repo, indice),
        {"snapshot": snapshot, "destino": destino, "uuid": uuid, "shards": shards},
    )


def _sirve_entera(cliente: Any, indice: str) -> bool:
    """¿La copia local tiene todos sus primarios? Sólo entonces se puede saltar.

    Antes del salto, cada ciclo restauraba en la otra ranura y cambiaba el alias, así
    que una copia ROJA (shard perdido tras un reinicio sucio o con el disco lleno; en la
    matriz van con 0 réplicas) quedaba sustituida en el ciclo siguiente. Saltarla sin
    mirar la dejaba roja mientras la luna no cambiara, y una búsqueda sobre el alias
    devolvía menos resultados sin marcar fallo. Amarillo vale: no tiene sentido
    recopiar decenas de GB por una réplica sin asignar.

    `request_timeout` no es adorno: sin él, opensearch-py toma `timeout` como el del
    CLIENTE y una cadena como "1s" revienta en urllib3 ("Timeout value connect was 1s,
    but it must be an int", medido en la matriz el 25-09). Ese error se leería como
    copia dañada y todo salto acabaría en restore."""
    try:
        salud = _leer_metadatos(
            lambda: cliente.cluster.health(index=indice, timeout="5s", request_timeout=30),
            "health",
        )
    except Exception as exc:
        log.warning("replica_salud_ilegible", indice=indice, error=str(exc)[:150])
        return False
    return str(salud.get("status")) in ("green", "yellow")


def _retirar_huerfano(cliente: Any, huerfano: str) -> None:
    """Borra la ranura libre si quedó ocupada por un ciclo interrumpido.

    Antes lo hacía de hecho cada ciclo al preparar la ranura de destino. Con el salto
    no, y un restore a medias fuera del alias seguía gastando E/S hasta terminar y
    ocupando disco hasta el siguiente cambio real: el 25-09 el ciclo viejo dejó así
    `…-000003-r` restaurando 10,4 GB idénticos. Borrarlo cancela ese restore. Con el
    `flock` de cada luna ningún otro ciclo del mismo repositorio puede estar usándolo."""
    try:
        if not cliente.indices.exists(index=huerfano):
            return
        cliente.indices.delete(index=huerfano)
    except Exception as exc:
        log.warning("replica_huerfano_no_retirado", indice=huerfano, error=str(exc)[:150])
        return
    log.info("replica_huerfano_retirado", indice=huerfano)


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
    6. Al refrescar, un snapshot con el MISMO contenido que lo que ya sirve no se
       restaura (ver `_mismo_contenido`). Lilith recopiaba 76,7 GB idénticos cada ciclo
       y cada ciclo tardaba 33-60 min: los ciclos se pisaban y cada uno borraba la
       restauración a medias del anterior (2 ciclos buenos de 64 en 24 h). Ante la
       duda se restaura: saltarse un cambio real es peor que recopiar. Y el salto no
       quita lo que el refresco hacía de paso: una copia roja se repone igual
       (`_sirve_entera`) y el huérfano de la ranura libre se borra (`_retirar_huerfano`).
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

    estados: dict[str, Any] = {}  # `_status` por snapshot, para `_huella_snapshot`
    for indice, nombre in sorted(ultimo_por_indice.items()):
        # Cuál de las dos ranuras está sirviendo AHORA (si alguna).
        alterna = _ranura_alterna(indice)
        sirviendo = next(
            (i for i in (indice, alterna) if _en_alias(cliente, i, config.indice_alias)), None
        )
        if sirviendo is not None and not refrescar:
            continue  # ya hay una copia servible y no se pidió refrescar (precaución 4)

        # Precaución 6. La huella del candidato se calcula también cuando no hay copia
        # sirviendo: es la que se guarda tras restaurar para poder saltarse el siguiente.
        candidata = _huella_snapshot(cliente, repo, nombre, indice, estados)
        if sirviendo is not None:
            viva = _huella_viva(config, cliente, repo, indice, sirviendo, estados)
            libre = _ranura_alterna(sirviendo)
            if viva is None or not _mismo_contenido(viva, nombre, candidata):
                pass  # cambió, o no se sabe: se restaura
            elif not _sirve_entera(cliente, sirviendo):
                # Mismo contenido, pero la copia local está rota: se repone igual.
                log.warning("replica_copia_danada", indice=sirviendo, snapshot=nombre)
            elif _en_alias(cliente, libre, config.indice_alias):
                # Las dos ranuras sirviendo duplican cada resultado. El restore de abajo
                # lo arregla (borra la libre, restaura en ella y retira la otra).
                log.warning("replica_dos_ranuras_en_alias", indice=indice)
            else:
                if viva.get("nueva"):
                    # Deducida de `_recovery`: se anota para no depender de ella, que
                    # desaparece en cuanto OpenSearch reinicia.
                    _guardar_huella(
                        config, cliente, repo, indice, sirviendo, viva["snapshot"], viva["shards"]
                    )
                _retirar_huerfano(cliente, libre)
                log.info(
                    "replica_sin_cambios",
                    snapshot=nombre,
                    indice=indice,
                    sirviendo=sirviendo,
                    restaurado_de=viva.get("snapshot"),
                )
                r.sin_cambios.append(sirviendo)
                continue

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
        # Fuera del `try`: el restore ya ocurrió y no se da por fallido por no poder
        # anotarlo; sólo se pierde el salto del ciclo siguiente.
        if candidata is not None:
            _guardar_huella(config, cliente, repo, indice, destino, nombre, candidata["shards"])

    r.ok = r.motivo is None
    # Antes de sellar: se siembra con el sello único tal como lo dejó el ciclo anterior.
    _sembrar_lunas(config, cliente)
    if r.ok:
        # Un ciclo que no restauró nada porque nada cambió también es un éxito: la copia
        # está al día. Si no sellara, el lag de una luna quieta crecería sin parar y la
        # alerta saltaría justo cuando todo va bien.
        sello = {"indices": r.indices, "sin_cambios": r.sin_cambios, "repositorio": repo}
        _marcar(config, _CLAVE_ULTIMO_RESTORE, sello)
        _marcar(config, _clave_de_luna(repo), sello)
    if r.indices and despliegue.de_config(config).corre_entidades:
        # Sólo si se restauró algo: sin docs nuevos no hay nada que resolver, y borrar
        # el cursor obligaría a la próxima pasada del backfill a barrer el índice entero.
        _invalidar_cursor_backfill(config, r)
        # Enganche: los docs recién restaurados son EXACTAMENTE lo que el resolvedor
        # de entidades aún no vio. El propio `lanzar_en_fondo` es no-op si ya hay uno
        # en curso o si el gobernador K15 ve poca RAM.
        #
        # OJO, medido el 25-09: el cron de las lunas llama aquí desde un
        # `docker exec … python -c` que termina segundos después, y el hilo daemon del
        # backfill muere con él. `backfill_entidades_estado` quedó en `ejecutando: true,
        # docs: 0` un segundo después del sello y no se movió más. Desde ese camino el
        # barrido no llega a correr; lo único que sobrevive es el cursor invalidado.
        from normalizacion.entidades import backfill

        resultado = backfill.lanzar_en_fondo(config, reiniciar=True)
        log.info("backfill_por_replica", **resultado)
    log.info("restore_completo", indices=len(r.indices), sin_cambios=len(r.sin_cambios), ok=r.ok)
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
