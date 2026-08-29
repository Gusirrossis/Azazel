"""Reproceso dirigido: volver a extraer lo que se extrajo mal (Fase 4).

Este es el módulo que convierte "mejoramos el OCR" en "el corpus mejoró". Sin él,
un archivo ya procesado con OCR malo se queda así para siempre: `archivo_id` es
determinista sobre (ruta, tamaño, mtime), así que re-catalogar no lo reprocesa;
`reprocesar-errores` solo mira los ERROR y `rescore-frio` solo los COLD.

La pieza que lo hace posible ya existía: el almacén es **direccionado por contenido**.
Los bytes se leen por `hash_contenido`, así que el reproceso NO necesita el disco
original — que en este sistema es justamente lo que se da por desechado.

Y como el índice usa `_id = archivo_id`, reindexar sobrescribe en vez de duplicar:
correr esto dos veces es seguro.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any

import psycopg

from normalizacion.core import cache_extraccion, recursos
from normalizacion.core.config import Config
from normalizacion.core.modelo import DocumentoArchivo, RutaDecision, clave_almacen
from normalizacion.core.observabilidad import obtener_logger
from normalizacion.entidades import anclas
from normalizacion.ingesta.workers import extractores

log = obtener_logger("reextraccion")


@dataclass
class ResumenReextraccion:
    candidatos: int = 0
    reextraidos: int = 0
    mejorados: int = 0
    empeorados: int = 0
    sin_cambio: int = 0
    reindexados: int = 0
    errores: int = 0

    def como_dict(self) -> dict[str, Any]:
        return {
            "candidatos": self.candidatos, "reextraidos": self.reextraidos,
            "mejorados": self.mejorados, "empeorados": self.empeorados,
            "sin_cambio": self.sin_cambio, "reindexados": self.reindexados,
            "errores": self.errores,
        }


def _docs_del_contenido(
    conn: psycopg.Connection[Any], hash_contenido: str
) -> list[dict[str, Any]]:
    """Todas las filas que comparten este contenido.

    Son varias justamente por lo que motivó la caché: el mismo archivo copiado en
    distintas rutas. Se extrae UNA vez y se reindexan TODAS, porque cada una es un
    documento distinto en el índice con su propia ruta y procedencia.
    """
    # `origen_contenedor` va incluido a propósito: el índice usa `_id = archivo_id`,
    # así que reindexar SOBRESCRIBE el documento entero. Sin este campo, cada pasada
    # de reproceso borraba la procedencia de todo archivo que venía dentro de un ZIP
    # o un RAR — se perdía saber de qué contenedor salió, y eso no se reconstruye.
    filas = conn.execute(
        "SELECT archivo_id, disco_id, nombre, ruta, extension, tamano, mtime,"
        "       tipo_real, puntaje, senales, motivo, version_filtro, origen_contenedor"
        " FROM archivos WHERE hash_contenido = %s AND estado IN ('HECHO','VERIFICADO','INDEXADO')",
        (hash_contenido,),
    ).fetchall()
    return [
        {
            "archivo_id": f[0], "disco_id": f[1], "nombre": f[2], "ruta": f[3],
            "extension": f[4], "tamano": f[5], "mtime": f[6], "tipo_real": f[7],
            "puntaje": f[8], "senales": f[9] or {}, "motivo": f[10], "version_filtro": f[11],
            "origen_contenedor": f[12],
        }
        for f in filas
    ]


def reextraer(
    config: Config,
    *,
    confianza_menor_a: float | None = None,
    motor: str | None = None,
    tipo_real: str | None = None,
    con_bandera: str | None = None,
    version_distinta_de: str | None = None,
    limite: int = 500,
    solo_listar: bool = False,
) -> ResumenReextraccion:
    """Re-extrae los contenidos que cumplen el filtro y reindexa sus documentos.

    `solo_listar` hace una pasada en seco: dice CUÁNTOS y CUÁLES sin tocar nada. Sobre
    decenas de miles de archivos, saber el tamaño del trabajo antes de lanzarlo no es
    un lujo — es la diferencia entre una tarde y una semana.
    """
    from normalizacion.core.almacen import crear_almacen
    from normalizacion.core.indexador.opensearch import SinkOpenSearch

    resumen = ResumenReextraccion()
    candidatos = cache_extraccion.candidatos_reproceso(
        config,
        version_distinta_de=version_distinta_de or "",
        confianza_menor_a=confianza_menor_a,
        motor=motor,
        tipo_real=tipo_real,
        con_bandera=con_bandera,
        limite=limite,
    )
    resumen.candidatos = len(candidatos)
    if solo_listar or not candidatos:
        return resumen

    almacen = crear_almacen(config)
    sink = SinkOpenSearch(config)
    try:
        with psycopg.connect(config.postgres_dsn, connect_timeout=10) as conn:
            for hash_contenido, tipo, confianza_previa in candidatos:
                # Mismo throttle que la ingesta: el reproceso es trabajo pesado y no
                # puede competir con el panel por la memoria (K15).
                recursos.esperar_si_presion(config, etiqueta="reextraccion")
                try:
                    # Un SAVEPOINT por contenido, como ya hace `backfill.py`. Antes
                    # era UNA transacción para toda la corrida con un `rollback()`
                    # global dentro del bucle: un solo contenido envenenado tiraba
                    # por tierra las cientos de extracciones ya guardadas —horas de
                    # OCR— y dejaba el índice desincronizado de la caché.
                    with conn.transaction():
                        _reextraer_uno(
                            config, conn, almacen, sink, hash_contenido, tipo,
                            confianza_previa, resumen,
                        )
                except Exception as exc:
                    resumen.errores += 1
                    log.warning(
                        "reextraccion_fallida", hash=hash_contenido[:12], error=str(exc)[:200]
                    )

            # El índice se confirma ANTES del commit. Al revés, un bulk que muere
            # (OpenSearch caído) dejaba la caché commiteada y al día: el trabajo de
            # OCR se daba por hecho, el índice se quedaba con el texto viejo, y el
            # contenido ya no volvía a salir como candidato. Trabajo perdido en
            # silencio y para siempre.
            confirmados, muertos = sink.drenar()
            resumen.reindexados = len(confirmados)
            resumen.errores += len(muertos)
            if muertos:
                conn.rollback()
                log.warning(
                    "reextraccion_indice_rechazo",
                    muertos=len(muertos),
                    detalle="la caché NO se commitea: los contenidos siguen siendo candidatos",
                )
            else:
                conn.commit()
    finally:
        sink.cerrar()

    log.info("reextraccion_completa", **resumen.como_dict())
    return resumen


def _reextraer_uno(
    config: Config,
    conn: psycopg.Connection[Any],
    almacen: Any,
    sink: Any,
    hash_contenido: str,
    tipo_real: str | None,
    confianza_previa: float | None,
    resumen: ResumenReextraccion,
) -> None:
    documentos = _docs_del_contenido(conn, hash_contenido)
    if not documentos:
        return  # el contenido ya no tiene documentos vivos: nada que reindexar

    referencia = documentos[0]
    inicio = time.monotonic()
    # El blob se vuelca a un spool ANTES de extraer. `almacen.leer` de MinIO devuelve
    # una respuesta HTTP en streaming, que no es seekable, y todos los extractores la
    # necesitan seekable: pypdf hace seek para leer el trailer, PIL para el header.
    # Pasarle el stream directo fallaba en el 100% de los casos contra MinIO — o sea,
    # en producción, donde el `almacen_backend` es "minio". Es el mismo patrón que ya
    # usa el worker en `_persistir`.
    with (
        almacen.leer(hash_contenido) as fuente,
        SpooledTemporaryFile(max_size=config.worker.umbral_memoria_bytes) as spool,
    ):
        shutil.copyfileobj(fuente, spool, config.worker.bloque_lectura_bytes)
        spool.seek(0)
        extraccion = extractores.extraer(
            config.worker,
            spool,
            tipo_real=tipo_real or referencia["tipo_real"],
            nombre=referencia["nombre"],
            tamano=referencia["tamano"],
            ocr_activo=config.filtro.ocr_activo,
        )
    ms = int((time.monotonic() - inicio) * 1000)
    resumen.reextraidos += 1

    # Un resultado DEGRADADO no puede pisar uno bueno. Un timeout o un plugin que
    # revienta devuelven texto vacío; guardarlo borraba el texto del índice Y marcaba
    # la fila como al día, así que el contenido dejaba de ser candidato a reproceso.
    # Pérdida permanente y silenciosa. Se conserva lo anterior y se cuenta el fallo.
    degradada = any(
        f.startswith(("extraccion_timeout", "extraccion_fallida", "ocr_no_disponible"))
        for f in extraccion.flags
    )
    if degradada or (not extraccion.texto and confianza_previa is not None):
        resumen.errores += 1
        log.warning(
            "reextraccion_degradada_descartada",
            hash=hash_contenido[:12],
            flags=extraccion.flags[:4],
        )
        return

    # Se registra si mejoró para poder decidir con datos si valió la pena la pasada,
    # en vez de con la sensación de que "ahora se ve mejor".
    nueva = extraccion.confianza
    if nueva is not None and confianza_previa is not None:
        if nueva > confianza_previa + 1:
            resumen.mejorados += 1
        elif nueva < confianza_previa - 1:
            resumen.empeorados += 1
        else:
            resumen.sin_cambio += 1

    cache_extraccion.guardar(
        conn,
        hash_contenido,
        version=cache_extraccion.clave_version(config),
        tipo_real=tipo_real or referencia["tipo_real"],
        texto=extraccion.texto,
        campos=extraccion.campos,
        perfil_calidad=extraccion.perfil_calidad,
        flags=extraccion.flags,
        confianza=extraccion.confianza,
        ms=ms,
    )

    umbral = config.filtro.ocr_confianza_descarte
    texto = extraccion.texto
    flags = list(extraccion.flags)
    if umbral > 0 and extraccion.confianza is not None and extraccion.confianza < umbral:
        texto = None
        flags.append("ocr_descartado_confianza")

    contexto = anclas.contexto_para_doc(anclas.buscar_en_texto(texto))
    for d in documentos:
        sink.entregar(
            DocumentoArchivo(
                archivo_id=d["archivo_id"],
                disco_id=d["disco_id"],
                nombre=d["nombre"],
                ruta_original=d["ruta"],
                extension=d["extension"],
                tamano=d["tamano"],
                mtime=d["mtime"],
                tipo_real=d["tipo_real"],
                puntaje=d["puntaje"],
                ruta_decision=RutaDecision.HOT,
                senales=d["senales"],
                motivo=d["motivo"],
                version_filtro=d["version_filtro"],
                origen_contenedor=d["origen_contenedor"],
                hash_contenido=hash_contenido,
                clave_almacen=clave_almacen(hash_contenido),
                procedencias=[f"{d['disco_id']}:{d['ruta']}"],
                campos_extraidos=extraccion.campos,
                texto_indexable=texto,
                perfil_calidad=extraccion.perfil_calidad,
                limites_alcanzados=[*flags, "reextraido"],
                ocr_confianza=extraccion.confianza,
                contexto_anclas=contexto,
            )
        )
