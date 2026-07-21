"""Plugin de imágenes: dimensiones + EXIF (Pillow) y, si `ctx.ocr_activo`, **OCR** (Tesseract).

L1: cabeceras siempre; el OCR sí lee píxeles y solo corre cuando el operador activó OCR
(⚙ `filtro.ocr_activo`, propagado por el worker). El OCR se hace de forma segura (helper
`_ocr`): sin el binario/paquete → flag `ocr_no_disponible`, se devuelve la metadata igual.
El texto extraído va a `texto` → `texto_indexable` (buscable)."""

from __future__ import annotations

from typing import Any

from . import ContextoExtraccion, ResultadoExtraccion, registrar
from ._ocr import ocr_imagen

_EXIF_FECHA = 306  # DateTime


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
        texto: str | None = None
        flags: list[str] = []
        if ctx.ocr_activo:
            texto, flags = ocr_imagen(imagen, ctx.perillas)
    return ResultadoExtraccion(campos=campos, texto=texto, flags=flags)
