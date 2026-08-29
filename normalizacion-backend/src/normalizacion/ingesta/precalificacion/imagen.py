"""¿Esta imagen es un DOCUMENTO escaneado o una fotografía? (Fase 3)

El problema que resuelve: hasta aquí, activar el OCR mandaba **toda** imagen a HOT sin
pasar por el scoring ni por la lista blanca. Un fondo de pantalla, la foto de una comida
y un acta de nacimiento recibían el mismo trato y el mismo coste de OCR. Sobre decenas de
miles de archivos eso es una factura sin techo por información que no existe.

Aquí se decide con lo que ya se leyó del head, ANTES de pagar nada: se abre la imagen con
Pillow (que solo decodifica lo que se le pide), se le saca una miniatura y se miran cuatro
señales. Es un clasificador de umbrales, no un modelo: es auditable, determinista y sale
prácticamente gratis. Su trabajo no es acertar siempre, es **no gastar OCR en playas**.

Ante la duda decide HOT, siguiendo el principio de recall del filtro: una fotografía
OCR-eada de más cuesta segundos; un documento mandado a frío por error se pierde de vista.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import IO, Any

#: Lado de la miniatura sobre la que se calculan las señales. 200 px bastan para
#: distinguir una página de texto de una foto, y acotan el coste a algo despreciable.
_MUESTRA = 200

#: Luminancia mínima para que algo pueda ser papel. Por debajo, ni el punto más
#: claro de la imagen se parece a una hoja: es una superficie de color.
_PAPEL_MIN = 140

#: Relación de aspecto más extrema que la de un documento razonable (un panorama,
#: un banner, una tira). Ningún formato de papel llega a 3:1.
_ASPECTO_MAX = 3.0


@dataclass(frozen=True, slots=True)
class ClaseImagen:
    """Veredicto + las señales que lo sostienen (se guardan: auditable y ajustable)."""

    es_documento: bool
    motivo: str
    senales: dict[str, Any]


def clasificar(fuente: IO[bytes], *, ancho_min: int) -> ClaseImagen:
    """Decide si vale la pena OCR-ear esta imagen. Nunca lanza.

    Si algo falla (formato raro, imagen truncada), devuelve `es_documento=True`: no
    poder clasificar no es razón para descartar, solo para no ahorrar.
    """
    try:
        return _clasificar(fuente, ancho_min=ancho_min)
    except Exception as exc:
        return ClaseImagen(True, "clasificacion_fallida", {"error": type(exc).__name__})


def _clasificar(fuente: IO[bytes], *, ancho_min: int) -> ClaseImagen:
    from PIL import Image

    fuente.seek(0)
    with Image.open(fuente) as imagen:
        ancho, alto = imagen.width, imagen.height
        senales: dict[str, Any] = {"ancho": ancho, "alto": alto, "modo": imagen.mode}

        # ① Demasiado pequeña: ícono, avatar, miniatura, bullet de una web.
        if max(ancho, alto) < ancho_min:
            return ClaseImagen(False, "imagen_pequena", senales)

        # ② Aspecto imposible para una página: banner, tira, panorama.
        lado_mayor, lado_menor = max(ancho, alto), max(1, min(ancho, alto))
        aspecto = round(lado_mayor / lado_menor, 2)
        senales["aspecto"] = aspecto
        if aspecto > _ASPECTO_MAX:
            return ClaseImagen(False, "aspecto_no_documento", senales)

        # A partir de aquí hace falta mirar píxeles, pero sobre una MINIATURA.
        #
        # `draft()` primero: le pide al decodificador JPEG que descomprima ya reducido
        # (usa el escalado DCT), así una foto de 24 MP nunca llega entera a memoria.
        # Sin esto, `convert("RGB")` a resolución completa reservaba ~350 MB por
        # imagen — y esto corre dentro del precalificador, en paralelo con los workers
        # y con el gobernador de memoria K15 vigilando.
        # formatos sin draft (PNG, TIFF): se sigue por el camino normal
        with contextlib.suppress(Exception):
            imagen.draft("RGB", (_MUESTRA, _MUESTRA))
        imagen.thumbnail((_MUESTRA, _MUESTRA))
        muestra = imagen.convert("RGB")

    saturacion = _saturacion_media(muestra)
    senales["saturacion"] = saturacion
    claros = _proporcion_clara(muestra)
    senales["proporcion_clara"] = claros

    # ③ Página en blanco (o casi): no hay tinta que leer.
    if claros > 0.995:
        return ClaseImagen(False, "pagina_en_blanco", senales)

    # ④ Fotografía: color abundante Y sin nada que se parezca a papel.
    #
    # Los dos umbrales son deliberadamente ESTRICTOS, porque el error caro es el
    # falso negativo: un acta mandada a frío desaparece de toda consulta, mientras
    # que una foto OCR-eada de más solo cuesta segundos. Casos que antes se perdían
    # y ahora pasan:
    #   · acta en papel amarillento o sepia -> `claros` bajo por el corte absoluto,
    #     pero la saturación de un papel envejecido no llega a 0.35;
    #   · credencial plastificada con foto a color -> el retrato sube la saturación,
    #     pero el resto sigue siendo papel claro, así que `claros` supera 0.25;
    #   · documento fotografiado sobre una mesa de madera -> la madera aporta color,
    #     pero la hoja ocupa el centro y deja bastante zona clara.
    if saturacion > 0.35 and claros < 0.25:
        return ClaseImagen(False, "fotografia", senales)

    return ClaseImagen(True, "escaneo_probable", senales)


def _saturacion_media(muestra: Any) -> float:
    """Saturación media en 0-1 desde el canal S de HSV.

    Se usa el histograma en vez de recorrer píxeles: son 256 cubetas contra 40 000
    píxeles, y esto corre una vez por imagen en un pipeline de decenas de miles.
    """
    histograma = muestra.convert("HSV").getchannel("S").histogram()[:256]
    total = sum(histograma)
    if total == 0:
        return 0.0
    return round(sum(i * h for i, h in enumerate(histograma)) / (total * 255.0), 3)


def _proporcion_clara(muestra: Any) -> float:
    """Fracción de píxeles del lado CLARO de la imagen: el papel domina una página.

    El corte es RELATIVO al brillo máximo de la propia imagen, no un absoluto en 200.
    Con un umbral fijo, un escaneo en papel sepia o kraft —cuyo blanco real ronda
    L≈170— daba 0.0 píxeles claros y se clasificaba como fotografía, que es
    exactamente el tipo de documento antiguo que más interesa leer.
    """
    gris = muestra.convert("L")
    histograma = gris.histogram()[:256]
    total = sum(histograma)
    if total == 0:
        return 0.0
    # Brillo máximo con presencia real (ignora píxeles sueltos de ruido).
    umbral_ruido = max(1, total // 500)
    pico = 255
    while pico > 0 and histograma[pico] < umbral_ruido:
        pico -= 1
    # Si lo más claro de la imagen sigue siendo oscuro, ahí no hay papel de ninguna
    # clase: es una superficie de color. Sin este corte, el umbral relativo daba 1.0
    # en cualquier imagen de color PLANO —su único valor es a la vez su pico— y una
    # foto saturada se colaba como documento.
    if pico < _PAPEL_MIN:
        return 0.0
    corte = max(120, int(pico * 0.82))
    return round(sum(histograma[corte:]) / total, 3)
