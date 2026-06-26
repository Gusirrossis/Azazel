"""Worker HOT (Fase 2): la LECTURA ÚNICA que pone el dato a salvo.

Por cada fila HOT reclamada, en un solo barrido en streaming (nunca el archivo
entero en RAM): calcula sha256 al vuelo → dedup contra el almacén → persiste el
blob si es nuevo → construye el doc JSON → lo entrega al sink. La regla de oro
del diseño: *cada archivo se lee una vez, se procesa una vez, y se puede reanudar
desde donde se quedó sin reprocesar nada*.

Robustez: un archivo ilegible va a dead-letter y el lote SIGUE. Un worker muerto
deja su fila EN_PROCESO con lease — `recuperar_huerfanos` la devuelve a la cola.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO

import psycopg

from normalizacion.core import cola, recursos
from normalizacion.core.almacen import Almacen, crear_almacen
from normalizacion.core.config import Config
from normalizacion.core.indexador import Sink, SinkNulo
from normalizacion.core.modelo import DocumentoArchivo, Estado, RutaDecision, clave_almacen
from normalizacion.core.observabilidad import obtener_logger
from normalizacion.ingesta.precalificacion import contenedores
from normalizacion.ingesta.workers import extractores

log = obtener_logger("worker")


class AlmacenNoDisponible(Exception):
    """El backend del almacén falló (MinIO caído…) — TRANSITORIO, no es culpa del archivo."""


@dataclass(frozen=True)
class ResumenWorker:
    procesados: int
    blobs_nuevos: int
    deduplicados: int
    bytes_copiados: int
    errores: int
    transitorios: int  # devueltos con backoff (se reintentarán solos)
    huerfanos_rescatados: int


def _abrir_fuente(config: Config, raiz: str, fila: cola.FilaReclamada) -> IO[bytes]:
    if fila.origen_contenedor:
        return contenedores.abrir_entrada(
            Path(raiz),
            list(fila.origen_contenedor["cadena"]),
            umbral_memoria=config.worker.umbral_memoria_bytes,
            limite_bytes=config.filtro.t3_descomprimido_max_bytes,
        )
    ruta_abs = Path(raiz) / fila.ruta
    if not ruta_abs.is_file():
        raise OSError(f"no existe bajo el punto de montaje: {fila.ruta}")
    return ruta_abs.open("rb")


def _persistir(
    config: Config, almacen: Almacen, fuente: IO[bytes], spool: IO[bytes] | None = None
) -> tuple[str, int, bool]:
    """Lectura única: stream → sha256 al vuelo + spool → dedup → blob si es nuevo.

    Si el caller pasa su `spool`, queda ABIERTO con el contenido completo — la
    extracción L1 lo reutiliza sin re-leer el disco origen (regla de oro).
    Devuelve (hash_contenido, bytes_leidos, era_nuevo)."""
    propio = spool is None
    destino: IO[bytes] = (
        SpooledTemporaryFile(max_size=config.worker.umbral_memoria_bytes)  # noqa: SIM115
        if spool is None
        else spool
    )
    try:
        sha = hashlib.sha256()
        copiado = 0
        while bloque := fuente.read(config.worker.bloque_lectura_bytes):
            sha.update(bloque)
            destino.write(bloque)
            copiado += len(bloque)
        hash_contenido = sha.hexdigest()
        try:
            if almacen.existe(hash_contenido):
                return hash_contenido, copiado, False
            destino.seek(0)
            almacen.guardar(hash_contenido, destino, copiado)
        except Exception as exc:  # el ALMACÉN falló, no el archivo → transitorio
            raise AlmacenNoDisponible(str(exc)[:200]) from exc
        return hash_contenido, copiado, True
    finally:
        if propio:
            destino.close()


def _construir_doc(
    fila: cola.FilaReclamada,
    hash_contenido: str,
    extraccion: extractores.ResultadoExtraccion | None = None,
) -> DocumentoArchivo:
    """El doc JSON del contrato: L0 de la cola + L1 del extractor (si lo hubo)."""
    extraccion = extraccion if extraccion is not None else extractores.ResultadoExtraccion()
    return DocumentoArchivo(
        archivo_id=fila.archivo_id,
        disco_id=fila.disco_id,
        nombre=fila.nombre,
        ruta_original=fila.ruta,
        extension=fila.extension,
        tamano=fila.tamano,
        mtime=fila.mtime,
        tipo_real=fila.tipo_real,
        puntaje=fila.puntaje,
        ruta_decision=RutaDecision.HOT,
        senales=fila.senales or {},
        motivo=fila.motivo,
        version_filtro=fila.version_filtro,
        hash_contenido=hash_contenido,
        clave_almacen=clave_almacen(hash_contenido),
        procedencias=[f"{fila.disco_id}:{fila.ruta}"],
        campos_extraidos=extraccion.campos,
        texto_indexable=extraccion.texto,
        perfil_calidad=extraccion.perfil_calidad,
        limites_alcanzados=extraccion.flags,
    )


def procesar_hot(
    config: Config,
    worker_id: str = "worker-1",
    sink: Sink | None = None,
    almacen: Almacen | None = None,
    seguir_esperando: Callable[[], bool] | None = None,
) -> ResumenWorker:
    """Drena las filas PRECALIFICADO (HOT) de la cola: persistir + doc + sink.

    `seguir_esperando`: modo CONTINUO — si la cola se vacía pero el productor
    (el precalificador, corriendo en paralelo) sigue vivo, espera en vez de
    terminar. Al morir el productor se hace UN barrido final (evita la carrera
    de filas insertadas entre el último claim y la muerte del productor)."""
    sink = sink if sink is not None else SinkNulo()
    almacen = almacen if almacen is not None else crear_almacen(config)
    procesados = nuevos = dedup = copiados = errores = transitorios = 0
    barrido_final = False
    w = config.worker

    with psycopg.connect(config.postgres_dsn) as conn:
        huerfanos = cola.recuperar_huerfanos(conn)
        conn.commit()
        if huerfanos:
            log.warning("huerfanos_rescatados", cuantos=huerfanos)
        montajes = cola.montajes(conn)

        while True:
            if cola.sistema_pausado(conn):  # norm pausar: drena el lote y se detiene
                log.warning("sistema_pausado", etapa="worker")
                break
            # Throttle adaptativo (K15): si la RAM está bajo presión —otro sistema
            # arrancó, las entidades pesan— este worker NO reclama más archivos hasta
            # que se recupere. Reduce la concurrencia efectiva sin matar procesos.
            recursos.esperar_si_presion(
                config, etiqueta=worker_id, seguir=lambda: not cola.sistema_pausado(conn)
            )
            filas = cola.claim(
                conn,
                worker_id=worker_id,
                estado=Estado.PRECALIFICADO,
                lote=config.worker.lote_claim,
                lease_segundos=config.worker.lease_segundos,
            )
            conn.commit()
            if not filas:
                if seguir_esperando is not None and seguir_esperando():
                    time.sleep(0.5)  # el productor sigue: pronto habrá más filas
                    continue
                if seguir_esperando is not None and not barrido_final:
                    barrido_final = True  # el productor murió: UNA pasada más
                    continue
                break

            hash_por_id: dict[str, str] = {}
            fila_por_id: dict[str, cola.FilaReclamada] = {}
            ultimo_heartbeat = time.monotonic()
            for fila in filas:
                # Heartbeat: un archivo enorme no debe dejar vencer el lease del lote
                if time.monotonic() - ultimo_heartbeat > w.lease_segundos / 3:
                    cola.renovar_lease(conn, worker_id, w.lease_segundos)
                    conn.commit()
                    ultimo_heartbeat = time.monotonic()

                if not cola.transicionar(
                    conn,
                    fila.archivo_id,
                    Estado.PRECALIFICADO,
                    Estado.EN_PROCESO,
                    conservar_lease=True,
                ):
                    continue  # otro proceso la ganó
                raiz = montajes.get(fila.disco_id)
                try:
                    if raiz is None:
                        raise OSError(f"disco sin punto de montaje: {fila.disco_id}")
                    with (
                        _abrir_fuente(config, raiz, fila) as fuente,
                        SpooledTemporaryFile(max_size=w.umbral_memoria_bytes) as spool,
                    ):
                        hash_contenido, leidos, era_nuevo = _persistir(
                            config, almacen, fuente, spool=spool
                        )
                        # L1 sobre el MISMO spool: cero re-lecturas del disco origen
                        extraccion = extractores.extraer(
                            w,
                            spool,
                            tipo_real=fila.tipo_real,
                            nombre=fila.nombre,
                            tamano=fila.tamano,
                        )
                    # entregar DENTRO del try: si serializar el doc falla, no tumba la corrida
                    sink.entregar(_construir_doc(fila, hash_contenido, extraccion))
                    hash_por_id[fila.archivo_id] = hash_contenido
                    fila_por_id[fila.archivo_id] = fila
                    copiados += leidos
                    if era_nuevo:
                        nuevos += 1
                    else:
                        dedup += 1
                except contenedores.ContenedorInseguro as exc:  # PERMANENTE
                    cola.marcar_error(
                        conn, fila.archivo_id, Estado.EN_PROCESO, f"contenedor: {exc}"
                    )
                    errores += 1
                    continue
                except (AlmacenNoDisponible, OSError) as exc:  # TRANSITORIO con tope
                    tipo = "almacen" if isinstance(exc, AlmacenNoDisponible) else "io_fuente"
                    en_reintento = cola.fallo_transitorio(
                        conn,
                        fila.archivo_id,
                        estado_actual=Estado.EN_PROCESO,
                        estado_retorno=Estado.PRECALIFICADO,
                        motivo=f"{tipo}: {exc}",
                        intentos_actuales=fila.intentos,
                        intentos_max=w.intentos_max,
                        backoff_s=w.backoff_transitorio_base_s * (2**fila.intentos),
                    )
                    if en_reintento:
                        transitorios += 1
                        log.warning("fallo_transitorio", archivo=fila.ruta, tipo=tipo)
                    else:
                        errores += 1
                    continue
                except Exception as exc:  # ARCHIVO ENVENENADO: dead-letter y la corrida
                    # SIGUE. Jamás un solo archivo tumba el worker automático.
                    cola.marcar_error(
                        conn,
                        fila.archivo_id,
                        Estado.EN_PROCESO,
                        f"worker_fallido:{type(exc).__name__}: {exc}"[:300],
                    )
                    errores += 1
                    log.warning(
                        "archivo_envenenado",
                        etapa="worker",
                        archivo=fila.ruta,
                        error=str(exc)[:200],
                    )
                    continue

            # Confirmación ANTES de transicionar: la cola nunca le miente al índice.
            confirmados, muertos = sink.drenar()
            for archivo_id in confirmados:
                cola.registrar_persistencia(conn, archivo_id, hash_por_id[archivo_id])
                procesados += 1
            for archivo_id, motivo, es_transitorio in muertos:
                if es_transitorio:  # OpenSearch caído: la fila vuelve con backoff
                    intentos = fila_por_id[archivo_id].intentos
                    en_reintento = cola.fallo_transitorio(
                        conn,
                        archivo_id,
                        estado_actual=Estado.EN_PROCESO,
                        estado_retorno=Estado.PRECALIFICADO,
                        motivo=f"indice: {motivo}",
                        intentos_actuales=intentos,
                        intentos_max=w.intentos_max,
                        backoff_s=w.backoff_transitorio_base_s * (2**intentos),
                    )
                    if en_reintento:
                        transitorios += 1
                    else:
                        errores += 1
                else:  # el índice rechazó ESTE doc → dead-letter
                    cola.marcar_error(
                        conn, archivo_id, Estado.EN_PROCESO, f"indexado_rechazado: {motivo}"
                    )
                    errores += 1
            conn.commit()  # lote durable
            log.info(
                "avance_worker",
                procesados=procesados,
                deduplicados=dedup,
                errores=errores,
                transitorios=transitorios,
            )

    sink.cerrar()
    log.info(
        "worker_completo",
        procesados=procesados,
        blobs_nuevos=nuevos,
        deduplicados=dedup,
        bytes=copiados,
        errores=errores,
        transitorios=transitorios,
    )
    return ResumenWorker(procesados, nuevos, dedup, copiados, errores, transitorios, huerfanos)
