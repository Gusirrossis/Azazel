"""Reclamación de espacio: borrar el ORIGEN cuando su copia ya está a salvo.

El nodo `online` ingiere lo que cae en `/datos/<fuente>` y su disco es finito. Una
vez que una fuente está 100 % normalizada —cada archivo HECHO o COLD, con su blob en
el almacén (MinIO, content-addressed) y verificado—, el original en `/datos` es
redundante: el almacén es la copia permanente y es reindexable. Borrarlo libera sitio
para el siguiente lote.

Esto es DESTRUCTIVO, así que es fail-closed y exige TRES condiciones a la vez:

  1. Nodo archivo maestro. Solo aquí 'puerta verde' significa 'el blob está a salvo
     LOCALMENTE'. En un nodo que replica hacia fuera, verde no garantiza copia local.
  2. Puerta VERDE. `evaluar_puerta` (la condición sagrada, sin override): total > 0 y
     cada fila HECHO o COLD-movida. Un solo archivo en flujo/ERROR/COLD-sin-mover y no
     se toca nada.
  3. Firma ESTABLE. Nada se ha escrito en la fuente desde que la catalogamos (misma
     huella nº-archivos/bytes/mtime). Cierra la carrera 'llegó un lote nuevo mientras
     corría el anterior': esos archivos aún no están en la BD, la puerta no los ve, y
     borrar la carpeta entera los perdería. Si la firma cambió, se pospone: el
     siguiente ciclo los cataloga y se reclama después.

El almacén NUNCA se toca: retiene el 100 % (HOT + COLD) para poder reindexar más
adelante (p.ej. sacar de frío lo que no pasó el umbral).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger
from normalizacion.ingesta.vigilante import Firma, firmar

log = obtener_logger("reclamacion")


def _vaciar_carpeta(fuente: Path) -> int:
    """Borra el CONTENIDO de la fuente pero conserva la carpeta (sigue siendo el
    punto de caída de esa fuente). Devuelve cuántas entradas de primer nivel borró."""
    borradas = 0
    for hijo in fuente.iterdir():
        if hijo.is_dir() and not hijo.is_symlink():
            shutil.rmtree(hijo, ignore_errors=True)
        else:
            try:
                hijo.unlink()
            except OSError as exc:
                log.error("borrado_fallido", ruta=str(hijo), error=str(exc)[:120])
                continue
        borradas += 1
    return borradas


def reclamar_origen(
    config: Config,
    disco_id: str,
    fuente: Path,
    firma_procesada: Firma,
    *,
    sentinela: str | None = None,
) -> bool:
    """Intenta liberar el original de una fuente ya normalizada. Devuelve True solo si
    de verdad borró algo. Nunca lanza por decisión propia (el vigilante no debe caerse
    porque una reclamación se posponga); sí propaga fallos de E/S reales del borrado."""
    from normalizacion.core import despliegue
    from normalizacion.ingesta.workers.verificador import DiscoDesconocido, evaluar_puerta

    if not despliegue.de_config(config).es_archivo_maestro:
        # Salvaguarda redundante con el vigilante, por si se llama desde otro sitio.
        log.warning("reclamacion_rechazada_no_maestro", disco_id=disco_id)
        return False

    try:
        estado = evaluar_puerta(config, disco_id)
    except DiscoDesconocido as exc:
        log.warning("reclamacion_disco_desconocido", disco_id=disco_id, error=str(exc)[:160])
        return False
    if not estado.seguro_para_desechar:
        log.info(
            "reclamacion_pospuesta_puerta_roja",
            disco_id=disco_id,
            motivo=estado.motivo_bloqueo,
            pendientes=estado.pendientes,
            errores=estado.errores,
        )
        return False

    # Última verificación ANTES de borrar: nada nuevo cayó desde que catalogamos.
    firma_ahora = firmar(fuente, ignorar=sentinela)
    if firma_ahora != firma_procesada:
        log.info(
            "reclamacion_pospuesta_contenido_nuevo",
            disco_id=disco_id,
            firma_procesada=firma_procesada.__dict__,
            firma_ahora=firma_ahora.__dict__,
        )
        return False

    borradas = _vaciar_carpeta(fuente)
    log.warning(
        "origen_reclamado",
        disco_id=disco_id,
        entradas_borradas=borradas,
        archivos=firma_procesada.n_archivos,
        bytes_liberados=firma_procesada.bytes_totales,
    )
    return True
