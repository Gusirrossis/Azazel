"""Corre la extracción REAL sobre el conjunto dorado y la mide contra la verdad.

Deliberadamente usa `extractores.extraer` —el mismo código que el worker— y no una
copia simplificada. Un arnés que mide otra cosa que la que corre en producción da
números tranquilizadores y falsos.

También esquiva la caché a propósito: aquí se está midiendo el EXTRACTOR, no el
sistema de caché, y reusar un resultado viejo mediría la configuración de ayer.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from normalizacion.calidad import metricas
from normalizacion.calidad.conjunto import Anotado, cargar
from normalizacion.core.config import Config
from normalizacion.entidades import anclas as mod_anclas
from normalizacion.ingesta.workers import extractores


def evaluar_documento(config: Config, doc: Anotado) -> metricas.Medicion:
    """Extrae un documento y lo compara con su verdad anotada."""
    inicio = time.monotonic()
    with doc.ruta.open("rb") as fuente:
        resultado = extractores.extraer(
            config.worker,
            fuente,
            tipo_real=doc.tipo_real,
            nombre=doc.ruta.name,
            tamano=doc.ruta.stat().st_size,
            ocr_activo=config.filtro.ocr_activo,
        )
    ms = int((time.monotonic() - inicio) * 1000)
    obtenido = resultado.texto or ""

    # Las anclas se buscan con el MISMO detector que usa el pipeline: si el extractor
    # leyó bien la CURP pero el detector no la reconoce, eso también es un fallo real
    # del sistema y tiene que aparecer en la métrica.
    obtenidas = [a.valor for a in mod_anclas.buscar_en_texto(obtenido)]

    return metricas.Medicion(
        documento=doc.hash_contenido,
        cer=metricas.cer(doc.texto, obtenido),
        wer=metricas.wer(doc.texto, obtenido),
        anclas=metricas.evaluar_anclas(doc.anclas, obtenidas),
        confianza=resultado.confianza,
        ms=ms,
        chars=len(obtenido),
        flags=list(resultado.flags),
    )


def evaluar_conjunto(config: Config, carpeta: Path) -> dict[str, Any]:
    """Mide el conjunto completo con la configuración ACTUAL.

    El informe se desglosa por estrato además del total: un recall global del 85%
    puede esconder que los escaneos van al 95% y las fotos de documentos al 40%, que
    es justo la información que dice dónde trabajar.
    """
    anotados, sin_anotar = cargar(carpeta)
    if not anotados:
        return {
            "error": "no hay documentos anotados todavía",
            "sin_anotar": len(sin_anotar),
        }

    mediciones = [evaluar_documento(config, d) for d in anotados]
    por_estrato: dict[str, list[metricas.Medicion]] = {}
    for doc, medicion in zip(anotados, mediciones, strict=True):
        por_estrato.setdefault(doc.estrato, []).append(medicion)

    return {
        "configuracion": _huella_config(config),
        "total": metricas.agregar(mediciones),
        "por_estrato": {k: metricas.agregar(v) for k, v in sorted(por_estrato.items())},
        "sin_anotar": len(sin_anotar),
        "detalle": [m.como_dict() for m in mediciones],
    }


def _huella_config(config: Config) -> dict[str, Any]:
    """Los ajustes que afectan al resultado, guardados junto a él.

    Sin esto, dos informes con números distintos no se pueden explicar: hace falta
    saber con qué dpi, qué idiomas y qué preprocesado se midió cada uno.
    """
    w, f = config.worker, config.filtro
    return {
        "ocr_activo": f.ocr_activo,
        "ocr_politica_imagen": f.ocr_politica_imagen,
        "ocr_confianza_descarte": f.ocr_confianza_descarte,
        "ocr_idiomas": w.ocr_idiomas,
        "ocr_pdf_escala": w.ocr_pdf_escala,
        "ocr_pdf_max_paginas": w.ocr_pdf_max_paginas,
        "ocr_deskew": w.ocr_deskew,
        "ocr_binarizar": w.ocr_binarizar,
        "ocr_max_lado": w.ocr_max_lado,
        "ocr_lado_minimo_util": w.ocr_lado_minimo_util,
        "extractor_timeout_s": w.extractor_timeout_s,
    }
