"""Plugin de imágenes: dimensiones + EXIF (Pillow). L1: solo cabeceras, no pixeles.

Nota: las imágenes normalmente van a COLD (tipo no objetivo, ⚙K3); este plugin
existe para cuando el alcance cambie (quitar image/* de K3 → ya hay extractor).
"""

from __future__ import annotations

from typing import Any

from . import ContextoExtraccion, ResultadoExtraccion, registrar

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
    return ResultadoExtraccion(campos=campos)
