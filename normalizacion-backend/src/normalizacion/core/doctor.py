"""Diagnóstico del nodo: ¿está bien configurado ESTE despliegue?

Existe porque los fallos de topología (⚙K16) son silenciosos: un nodo con el perfil
equivocado no revienta —arranca perfectamente y hace lo que no le toca—, una réplica
detenida sigue sirviendo datos viejos, y una API sin llaves responde a todo el mundo
sin quejarse. Todo eso se descubre semanas después, o no se descubre.

Cada comprobación devuelve un `Chequeo` con su nivel; la CLI sólo los pinta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from normalizacion.core import despliegue
from normalizacion.core.config import Config

Nivel = Literal["ok", "aviso", "error"]


@dataclass(frozen=True)
class Chequeo:
    nivel: Nivel
    titulo: str
    detalle: str = ""


def _ok(t: str, d: str = "") -> Chequeo:
    return Chequeo("ok", t, d)


def _aviso(t: str, d: str = "") -> Chequeo:
    return Chequeo("aviso", t, d)


def _error(t: str, d: str = "") -> Chequeo:
    return Chequeo("error", t, d)


# ------------------------------------------------------------------ comprobaciones


def _chequear_postgres(config: Config) -> list[Chequeo]:
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
            rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            discos = conn.execute(
                "SELECT disco_id FROM discos ORDER BY disco_id"
            ).fetchall()
    except Exception as exc:
        return [_error("Postgres inalcanzable", str(exc)[:160])]

    out = [_ok("Postgres", f"esquema en {rev[0] if rev else '?'}")]

    # ⚙K16 — discos catalogados SIN prefijo de nodo. No es un error: son válidos
    # para siempre. Pero re-catalogarlos con id nuevo los DUPLICARÍA entero, así que
    # el operador tiene que saber que existen y decidir a sabiendas.
    prefijo = despliegue.prefijo_disco(config)
    if prefijo:
        sin_prefijo = [d[0] for d in discos if not str(d[0]).startswith(prefijo)]
        if sin_prefijo:
            out.append(
                _aviso(
                    f"{len(sin_prefijo)} disco(s) sin prefijo de nodo",
                    f"{', '.join(sin_prefijo[:5])}"
                    f"{'…' if len(sin_prefijo) > 5 else ''}."
                    " Conservan su id a propósito: re-catalogarlos con uno nuevo"
                    " cambiaría todos sus archivo_id y los duplicaría en cola e índice.",
                )
            )
    return out


def _chequear_opensearch(config: Config) -> list[Chequeo]:
    from normalizacion.core.indexador.opensearch import crear_cliente, indice_escritura

    try:
        cliente = crear_cliente(config)
        if not cliente.ping():
            return [_error("OpenSearch no responde", config.opensearch_url)]
    except Exception as exc:
        return [_error("OpenSearch inalcanzable", str(exc)[:160])]

    out: list[Chequeo] = []
    try:
        alias = cliente.indices.get_alias(name=config.indice_alias)
    except Exception:
        return [
            _error(
                f"El alias '{config.indice_alias}' no existe",
                "corre `norm aplicar-indice`: sin alias, el sink no tiene dónde escribir",
            )
        ]

    escritura = [
        i for i, meta in alias.items()
        if meta.get("aliases", {}).get(config.indice_alias, {}).get("is_write_index")
    ]
    total = 0
    try:
        total = int(cliente.count(index=config.indice_alias)["count"])
    except Exception:
        pass
    out.append(_ok("OpenSearch", f"alias '{config.indice_alias}': {len(alias)} índice(s), {total} docs"))

    # Sin `is_write_index` designado, la ISM no puede rotar y escribir al alias falla
    # en cuanto tiene más de un índice — que es justo lo que pasa al restaurar el
    # snapshot del otro nodo.
    if not escritura:
        out.append(
            _error(
                "El alias no tiene índice de escritura",
                f"esperado {indice_escritura(config)}. Corre `norm aplicar-indice`.",
            )
        )
    elif len(escritura) > 1:
        out.append(_error("Más de un índice de escritura en el alias", ", ".join(escritura)))
    else:
        esperado = indice_escritura(config)
        if escritura[0] != esperado:
            out.append(
                _aviso(
                    "El índice de escritura no es el de este nodo",
                    f"es '{escritura[0]}', se esperaba '{esperado}' — ¿rotó la ISM,"
                    " o este nodo arrancó antes con otro nodo_id?",
                )
            )
    return out


def _chequear_almacen(config: Config) -> list[Chequeo]:
    if config.almacen_backend == "local":
        return [_ok("Almacén", f"carpetas locales ({config.almacen_local_raiz})")]
    try:
        from minio import Minio

        cliente = Minio(
            config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=False,
        )
        faltan = [
            b for b in (config.minio_bucket, config.minio_bucket_frio) if not cliente.bucket_exists(b)
        ]
    except Exception as exc:
        return [_error("MinIO inalcanzable", str(exc)[:160])]
    if faltan:
        return [_aviso("MinIO", f"faltan buckets: {', '.join(faltan)} (se crean al usarlos)")]
    return [_ok("MinIO", f"buckets {config.minio_bucket}, {config.minio_bucket_frio}")]


def _chequear_seguridad(config: Config) -> list[Chequeo]:
    t = despliegue.de_config(config)
    if config.api_keys:
        return [_ok("Autenticación de la API", f"{len(config.api_keys)} llave(s) estática(s)")]
    if t.sirve_publico:
        return [
            _error(
                "NORM_API_KEYS vacío en un nodo PÚBLICO",
                "con la lista vacía `llave_valida` devuelve True: la API responde a"
                " cualquiera. El índice completo queda abierto.",
            )
        ]
    return [_aviso("API sin llaves estáticas", "aceptable en un nodo no expuesto")]


def _chequear_replica(config: Config) -> list[Chequeo]:
    if config.despliegue.es_local():
        return []
    from normalizacion.core import replicacion

    t = despliegue.de_config(config)
    papel = "emisor (toma snapshot)" if t.es_archivo_maestro else "receptor (restaura)"
    lag = replicacion.lag_segundos(config)
    if lag is None:
        return [_error(f"Réplica: nunca ejecutada — {papel}", "¿está activo el timer?")]
    horas = lag / 3600
    detalle = f"último éxito hace {horas:.1f} h — {papel}"
    return [_aviso("Réplica atrasada", detalle) if horas > 2 else _ok("Réplica", detalle)]


def diagnosticar(config: Config) -> list[Chequeo]:
    """Todas las comprobaciones, en orden de lectura."""
    salida: list[Chequeo] = []
    salida += _chequear_postgres(config)
    salida += _chequear_opensearch(config)
    salida += _chequear_almacen(config)
    salida += _chequear_seguridad(config)
    salida += _chequear_replica(config)
    return salida


def cabecera(config: Config) -> dict[str, Any]:
    """Identidad y capacidades del nodo, para encabezar el informe."""
    t = despliegue.de_config(config)
    return {
        "perfil": config.despliegue.perfil,
        "nodo_id": config.despliegue.nodo_id,
        "capacidades": {
            "ingesta": t.corre_ingesta,
            "entidades": t.corre_entidades,
            "publico": t.sirve_publico,
            "archivo_maestro": t.es_archivo_maestro,
            "destino_eligible": t.destino_eligible,
        },
    }
