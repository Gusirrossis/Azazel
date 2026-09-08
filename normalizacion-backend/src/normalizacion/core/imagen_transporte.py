"""Imágenes listas para viajar en una respuesta (federación con Lilith).

Un consumidor federado que encuentra una coincidencia quiere **verla**, no solo saber
que existe. Pero el original no sirve tal cual: un escaneo de 4000×3000 son varios MB,
y en base64 crece un 33 % más. Mandar eso dentro de una respuesta de búsqueda —donde
hoy una página de 20 resultados pesa 66 KB— la pondría en decenas de MB.

Este módulo hace lo único que hace falta: **decodificar lo justo, encoger al tamaño
pedido y devolver base64**, con topes duros en los dos extremos.

Tres cuidados, cada uno por un fallo concreto:

  · `draft()` antes de decodificar. Pillow puede decodificar un JPEG directamente a
    escala reducida: para una miniatura de 1600 px de un escaneo de 4000, evita
    materializar los 12 M de píxeles que luego se iban a tirar. Es la diferencia
    entre ~50 MB de RAM por imagen y unos pocos.
  · Tope de píxeles. Una imagen construida para reventar al que la abra (la
    «decompression bomb») declara dimensiones enormes con muy pocos bytes. Pillow lo
    avisa pero por defecto **no** lo impide.
  · Salida en JPEG salvo que haya transparencia. Un PNG de escaneo pesa varias veces
    lo mismo en JPEG con calidad alta, y quien recibe quiere verlo, no reeditarlo.
    Con alpha se conserva PNG: convertirlo a JPEG pinta el fondo de negro.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import IO

# Un JPEG de 24 MP ya es un escaneo A3 a 600 dpi. Por encima, o es un panorama que no
# es un documento, o alguien está intentando que reservemos memoria por él.
MAX_PIXELES = 24_000_000


class ImagenDemasiadoGrande(ValueError):
    """La imagen declara más píxeles de los que estamos dispuestos a decodificar."""


class NoEsImagen(ValueError):
    """El contenido no se pudo abrir como imagen."""


@dataclass(frozen=True)
class ImagenTransportable:
    base64: str
    tipo: str
    bytes_salida: int
    ancho: int
    alto: int
    ancho_original: int
    alto_original: int
    redimensionada: bool

    @property
    def como_dict(self) -> dict[str, object]:
        return {
            "base64": self.base64,
            "tipo": self.tipo,
            "bytes": self.bytes_salida,
            "ancho": self.ancho,
            "alto": self.alto,
            "ancho_original": self.ancho_original,
            "alto_original": self.alto_original,
            "redimensionada": self.redimensionada,
        }


def preparar(
    fuente: IO[bytes],
    *,
    max_lado: int = 1600,
    calidad: int = 82,
    max_pixeles: int = MAX_PIXELES,
) -> ImagenTransportable:
    """Decodifica lo justo, encoge a `max_lado` y devuelve base64.

    `max_lado` se aplica al lado MAYOR y solo hacia abajo: una imagen ya pequeña no se
    amplía — estirarla no añade información y multiplica los bytes que viajan.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        imagen = Image.open(fuente)
    except UnidentifiedImageError as exc:
        raise NoEsImagen(str(exc)[:200]) from exc

    ancho_original, alto_original = imagen.size
    if ancho_original * alto_original > max_pixeles:
        raise ImagenDemasiadoGrande(
            f"{ancho_original}x{alto_original} supera el tope de {max_pixeles} píxeles"
        )

    mayor = max(ancho_original, alto_original)
    # `draft` solo hace algo en JPEG y solo a escalas 1/2, 1/4, 1/8: pedirlo es gratis
    # y en el resto de formatos es un no-op. Va ANTES de tocar los píxeles.
    if mayor > max_lado:
        imagen.draft("RGB", (max_lado, max_lado))

    tiene_alpha = imagen.mode in ("RGBA", "LA", "P")
    imagen = imagen.convert("RGBA" if tiene_alpha else "RGB")

    redimensionada = False
    if mayor > max_lado:
        escala = max_lado / mayor
        nuevo = (max(1, round(ancho_original * escala)), max(1, round(alto_original * escala)))
        imagen = imagen.resize(nuevo, Image.Resampling.LANCZOS)
        redimensionada = True

    salida = io.BytesIO()
    if tiene_alpha:
        imagen.save(salida, format="PNG", optimize=True)
        tipo = "image/png"
    else:
        # `optimize` recalcula las tablas de Huffman: unos milisegundos por un 3-5 %
        # menos de bytes, que en base64 se pagan multiplicados por 1,33.
        imagen.save(salida, format="JPEG", quality=calidad, optimize=True, progressive=True)
        tipo = "image/jpeg"

    crudo = salida.getvalue()
    return ImagenTransportable(
        base64=base64.b64encode(crudo).decode("ascii"),
        tipo=tipo,
        bytes_salida=len(crudo),
        ancho=imagen.width,
        alto=imagen.height,
        ancho_original=ancho_original,
        alto_original=alto_original,
        redimensionada=redimensionada,
    )


def es_imagen(tipo_real: str | None, extension: str | None = None) -> bool:
    """¿Merece la pena intentar abrirlo como imagen?

    Se mira el tipo REAL (detectado por contenido en la precalificación), no la
    extensión: un `.jpg` que en realidad es un PDF renombrado no debe entrar aquí, y
    una imagen sin extensión sí. La extensión solo se usa como respaldo cuando el tipo
    no se detectó.
    """
    if tipo_real:
        return tipo_real.lower().startswith("image/")
    if extension:
        return extension.lower() in {
            ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".heic",
        }
    return False
