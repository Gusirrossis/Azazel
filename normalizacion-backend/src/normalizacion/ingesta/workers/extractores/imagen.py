"""Plugin de imágenes: dimensiones + EXIF (Pillow) y, si está activo, **OCR** (Tesseract).

L1: cabeceras siempre; el OCR sí lee píxeles. El OCR solo tiene sentido cuando el filtro
enrutó la imagen a HOT (⚙ `filtro.ocr_activo`); aquí se intenta de forma segura:
- Tesseract se importa perezoso y GUARDADO: si falta el binario/paquete → flag
  `ocr_no_disponible`, se devuelve la metadata igual (nunca rompe la corrida).
- Downscale a `ocr_max_lado` para acotar RAM/tiempo; se saltan miniaturas (< `ocr_min_lado`).
- El texto extraído va a `texto` → `texto_indexable` (buscable). Acotado a `extractor_max_chars`.
"""

from __future__ import annotations

from typing import Any

from . import ContextoExtraccion, ResultadoExtraccion, registrar

_EXIF_FECHA = 306  # DateTime


def _ocr_texto(imagen: Any, ctx: ContextoExtraccion) -> tuple[str | None, list[str]]:
    """Corre OCR sobre una copia (gris + downscale). Devuelve (texto|None, flags).
    Motor pluggable: hoy Tesseract; se puede cambiar sin tocar el resto del plugin."""
    p = ctx.perillas
    lado = max(imagen.width, imagen.height)
    if lado < p.ocr_min_lado:
        return None, ["ocr_saltado_pequena"]
    try:
        import pytesseract  # type: ignore[import-not-found]  # perezoso: dep opcional
    except Exception:  # sin el paquete instalado, se degrada con gracia
        return None, ["ocr_no_disponible"]
    try:
        # Gris + downscale: menos RAM y suele dar mejor OCR que color a full-res.
        lienzo = imagen.convert("L")
        if lado > p.ocr_max_lado:
            lienzo.thumbnail((p.ocr_max_lado, p.ocr_max_lado))
        texto = pytesseract.image_to_string(lienzo, lang=p.ocr_idiomas)
    except pytesseract.TesseractNotFoundError:
        return None, ["ocr_no_disponible"]
    except Exception as exc:  # OCR falla → flag + metadata, jamás tumba la corrida
        return None, [f"ocr_fallido:{type(exc).__name__}"]
    texto = (texto or "").strip()
    if not texto:
        return None, ["ocr_vacio"]
    return texto[: p.extractor_max_chars], ["ocr_ok"]


@registrar("image/*")
def extraer_imagen(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    from PIL import Image

    with Image.open(ctx.fuente) as imagen:
        campos: dict[str, Any] = {
            "ancho": imagen.width,
            "alto": imagen.height,
            "formato": imagen.format,
            "modo": imagen.mode,
        }
        exif = imagen.getexif()
        fecha = exif.get(_EXIF_FECHA)
        if fecha:
            campos["exif_fecha"] = str(fecha)[:40]
        texto, flags = _ocr_texto(imagen, ctx)
    return ResultadoExtraccion(campos=campos, texto=texto, flags=flags)
