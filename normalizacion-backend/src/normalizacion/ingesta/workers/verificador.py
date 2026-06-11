"""Verificador y puerta de integridad (Fase 2): lo SAGRADO del sistema.

- `verificar_indexados`: re-lee cada blob del almacén y compara su sha256 contra el
  esperado. Coincide → VERIFICADO → HECHO. No coincide → ERROR (bloquea la puerta).
- `mover_frio`: los COLD también son dato — se copian al almacén frío (hash al vuelo,
  una sola lectura) ANTES de que el disco pueda desecharse. La fila sigue en COLD
  (reversible: re-puntuable con un filtro vN).
- `evaluar_puerta`: un disco es "SEGURO PARA DESECHAR" solo cuando el 100% de sus
  filas está HECHO o COLD-ya-movido. Sin override manual. Tests bloquean el build
  si esto se rompe (riesgo R1: pérdida de datos = catastrófico).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg

from normalizacion.core import cola
from normalizacion.core.almacen import Almacen, AlmacenLocal, crear_almacen
from normalizacion.core.config import Config
from normalizacion.core.modelo import Estado
from normalizacion.core.observabilidad import obtener_logger
from normalizacion.ingesta.workers.orquestador import (
    AlmacenNoDisponible,
    _abrir_fuente,
    _persistir,
)

log = obtener_logger("verificador")


def crear_almacen_frio(config: Config) -> Almacen:
    """El frío usa la MISMA interfaz, en su propio espacio (bucket/carpeta barata)."""
    if config.almacen_backend == "local":
        from pathlib import Path

        return AlmacenLocal(Path(config.almacen_frio_local_raiz).expanduser())
    from normalizacion.core.almacen.backend_minio import AlmacenMinio

    return AlmacenMinio(
        endpoint=config.minio_endpoint,
        access_key=config.minio_access_key,
        secret_key=config.minio_secret_key,
        bucket=config.minio_bucket_frio,
    )


# ------------------------------------------------------------------ verificación


@dataclass(frozen=True)
class ResumenVerificacion:
    verificados: int
    fallidos: int
    transitorios: int = 0


def verificar_indexados(
    config: Config, almacen: Almacen | None = None, worker_id: str = "verificador-1"
) -> ResumenVerificacion:
    """Re-lee cada blob INDEXADO y lo compara con su hash esperado (riesgo R1)."""
    almacen = almacen if almacen is not None else crear_almacen(config)
    verificados = fallidos = transitorios = 0

    with psycopg.connect(config.postgres_dsn) as conn:
        while True:
            if cola.sistema_pausado(conn):
                log.warning("sistema_pausado", etapa="verificador")
                break
            filas = cola.claim(
                conn,
                worker_id=worker_id,
                estado=Estado.INDEXADO,
                lote=config.worker.lote_claim,
                lease_segundos=config.worker.lease_segundos,
            )
            conn.commit()
            if not filas:
                break
            for fila in filas:
                esperado = fila.hash_contenido
                if not esperado:
                    cola.marcar_error(
                        conn, fila.archivo_id, Estado.INDEXADO, "verificacion_sin_hash"
                    )
                    fallidos += 1
                    continue
                sha = hashlib.sha256()
                try:
                    with almacen.leer(esperado) as blob:
                        while bloque := blob.read(config.worker.bloque_lectura_bytes):
                            sha.update(bloque)
                except Exception as exc:  # almacén caído → TRANSITORIO con tope
                    en_reintento = cola.fallo_transitorio(
                        conn,
                        fila.archivo_id,
                        estado_actual=Estado.INDEXADO,
                        estado_retorno=None,  # sigue INDEXADO, con backoff
                        motivo=f"verificacion_io: {exc}",
                        intentos_actuales=fila.intentos,
                        intentos_max=config.worker.intentos_max,
                        backoff_s=config.worker.backoff_transitorio_base_s * (2**fila.intentos),
                    )
                    if en_reintento:
                        transitorios += 1
                    else:
                        fallidos += 1
                    continue
                if sha.hexdigest() == esperado:
                    cola.transicionar(conn, fila.archivo_id, Estado.INDEXADO, Estado.VERIFICADO)
                    cola.transicionar(conn, fila.archivo_id, Estado.VERIFICADO, Estado.HECHO)
                    verificados += 1
                else:
                    # Corrupción silenciosa detectada: la fila bloquea la puerta
                    cola.marcar_error(
                        conn,
                        fila.archivo_id,
                        Estado.INDEXADO,
                        "verificacion_fallida: hash no coincide",
                    )
                    fallidos += 1
                    log.error("blob_corrupto", archivo=fila.ruta, hash=esperado)
            conn.commit()

    log.info(
        "verificacion_completa",
        verificados=verificados,
        fallidos=fallidos,
        transitorios=transitorios,
    )
    return ResumenVerificacion(verificados, fallidos, transitorios)


# ------------------------------------------------------------------ mover a frío


@dataclass(frozen=True)
class ResumenFrio:
    movidos: int
    deduplicados: int
    errores: int
    transitorios: int = 0


def mover_frio(
    config: Config, almacen_frio: Almacen | None = None, worker_id: str = "frio-1"
) -> ResumenFrio:
    """Copia los COLD pendientes al almacén frío (hash durante el movimiento)."""
    almacen_frio = almacen_frio if almacen_frio is not None else crear_almacen_frio(config)
    movidos = dedup = errores = transitorios = 0

    with psycopg.connect(config.postgres_dsn) as conn:
        montajes = cola.montajes(conn)
        while True:
            if cola.sistema_pausado(conn):
                log.warning("sistema_pausado", etapa="frio")
                break
            filas = cola.claim(
                conn,
                worker_id=worker_id,
                estado=Estado.COLD,
                lote=config.worker.lote_claim,
                lease_segundos=config.worker.lease_segundos,
                solo_sin_hash=True,  # lo ya movido no se re-mueve
            )
            conn.commit()
            if not filas:
                break
            for fila in filas:
                raiz = montajes.get(fila.disco_id)
                try:
                    if raiz is None:
                        raise OSError(f"disco sin punto de montaje: {fila.disco_id}")
                    with _abrir_fuente(config, raiz, fila) as fuente:
                        hash_contenido, _, era_nuevo = _persistir(config, almacen_frio, fuente)
                except (OSError, AlmacenNoDisponible) as exc:  # TRANSITORIO con tope
                    en_reintento = cola.fallo_transitorio(
                        conn,
                        fila.archivo_id,
                        estado_actual=Estado.COLD,
                        estado_retorno=None,  # sigue COLD, con backoff
                        motivo=f"io_frio: {exc}",
                        intentos_actuales=fila.intentos,
                        intentos_max=config.worker.intentos_max,
                        backoff_s=config.worker.backoff_transitorio_base_s * (2**fila.intentos),
                    )
                    if en_reintento:
                        transitorios += 1
                    else:
                        errores += 1
                    continue
                except Exception as exc:  # ARCHIVO ENVENENADO (entrada de 7z/rar hostil,
                    # decode raro): dead-letter y SIGUE. Mismo blindaje que el worker.
                    cola.marcar_error(
                        conn,
                        fila.archivo_id,
                        Estado.COLD,
                        f"frio_fallido:{type(exc).__name__}: {exc}"[:300],
                    )
                    errores += 1
                    log.warning(
                        "archivo_envenenado", etapa="frio", archivo=fila.ruta, error=str(exc)[:200]
                    )
                    continue
                cola.registrar_movimiento_frio(conn, fila.archivo_id, hash_contenido)
                movidos += 1
                if not era_nuevo:
                    dedup += 1
            conn.commit()

    log.info(
        "frio_completo",
        movidos=movidos,
        deduplicados=dedup,
        errores=errores,
        transitorios=transitorios,
    )
    return ResumenFrio(movidos, dedup, errores, transitorios)


# ------------------------------------------------------------------ la puerta


@dataclass(frozen=True)
class EstadoPuerta:
    disco_id: str
    total: int
    hechos: int
    cold_movidos: int
    pendientes: int  # TODO lo que no está a salvo: en flujo, COLD sin mover, ERROR
    errores: int
    seguro_para_desechar: bool


def evaluar_puerta(config: Config, disco_id: str) -> EstadoPuerta:
    """La condición SAGRADA (riesgo R1, sin override manual):

        seguro ⟺ total > 0  Y  cada fila está HECHO o (COLD con blob ya en frío)

    Un solo archivo sin verificar, sin mover o en ERROR → el disco NO se toca."""
    with psycopg.connect(config.postgres_dsn) as conn:
        fila = conn.execute(
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE estado = 'HECHO'),
                   COUNT(*) FILTER (WHERE estado = 'COLD' AND hash_contenido IS NOT NULL),
                   COUNT(*) FILTER (WHERE estado = 'ERROR')
              FROM archivos WHERE disco_id = %s
            """,
            (disco_id,),
        ).fetchone()
        total, hechos, cold_movidos, errores = (
            (int(fila[0]), int(fila[1]), int(fila[2]), int(fila[3])) if fila else (0, 0, 0, 0)
        )
        pendientes = total - hechos - cold_movidos
        seguro = total > 0 and pendientes == 0
        conn.execute(
            "UPDATE discos SET seguro_para_desechar = %s, actualizado_en = now()"
            " WHERE disco_id = %s",
            (seguro, disco_id),
        )
        conn.commit()

    estado = EstadoPuerta(disco_id, total, hechos, cold_movidos, pendientes, errores, seguro)
    log.info("puerta_evaluada", **estado.__dict__)
    return estado
