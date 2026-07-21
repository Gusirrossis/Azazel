"""OCR compartido (Tesseract vía pytesseract). Motor pluggable, import perezoso y GUARDADO:
si falta el paquete/binario → flag `ocr_no_disponible`, jamás excepción. Lo usan el extractor
de imágenes y el fallback de PDFs escaneados."""

from __future__ import annotations

from typing import Any

from normalizacion.core.config import PerillasWorker


def ocr_imagen(imagen: Any, perillas: PerillasWorker) -> tuple[str | None, list[str]]:
    """OCR de una imagen PIL ya abierta. Devuelve (texto|None, flags). Gris + downscale para
    acotar RAM/tiempo. Nunca lanza: los fallos se reportan como flags."""
    lado = max(imagen.width, imagen.height)
    if lado < perillas.ocr_min_lado:
        return None, ["ocr_saltado_pequena"]
    try:
        import pytesseract  # dependencia opcional (extra `ocr`)
    except Exception:  # sin el paquete instalado, se degrada con gracia
        return None, ["ocr_no_disponible"]
    try:
        lienzo = imagen.convert("L")
        if lado > perillas.ocr_max_lado:
            lienzo.thumbnail((perillas.ocr_max_lado, perillas.ocr_max_lado))
        texto = pytesseract.image_to_string(lienzo, lang=perillas.ocr_idiomas)
    except pytesseract.TesseractNotFoundError:
        return None, ["ocr_no_disponible"]
    except Exception as exc:  # OCR falla → flag, jamás tumba la corrida
        return None, [f"ocr_fallido:{type(exc).__name__}"]
    texto = (texto or "").strip()
    if not texto:
        return None, ["ocr_vacio"]
    return texto[: perillas.extractor_max_chars], ["ocr_ok"]
