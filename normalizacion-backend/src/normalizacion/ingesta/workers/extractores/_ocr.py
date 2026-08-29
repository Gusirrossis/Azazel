"""OCR compartido (Tesseract vía pytesseract). Motor pluggable, import perezoso y GUARDADO:
si falta el paquete/binario → flag `ocr_no_disponible`, jamás excepción. Lo usan el extractor
de imágenes y el fallback de PDFs escaneados.

Dos cosas que este módulo hace y antes no:

**Mide.** `image_to_data` devuelve confianza por palabra; `image_to_string` solo texto.
Sin una medida, `|||l1 0O ¬` y un acta perfectamente leída entran al índice con la
misma cara — y no hay forma de filtrar la basura, de priorizar el reproceso, ni de
saber si un cambio en el preprocesado mejoró algo o lo empeoró.

**Prepara la imagen.** Tesseract lee mucho peor una página torcida, en color y a baja
resolución. Deskew + binarización + un tamaño mínimo razonable cuestan milisegundos y
cambian el resultado en escaneos de fotocopia, que es de lo que está hecho el corpus.
"""

from __future__ import annotations

from typing import Any

from normalizacion.core.config import PerillasWorker

#: Palabras con confianza por debajo de esto no cuentan para la media. Tesseract
#: marca con -1 los bloques sin texto; incluirlos hundiría la media de cualquier
#: página con márgenes en blanco, que son todas.
_CONFIANZA_IGNORAR = 0.0


def _preparar(imagen: Any, perillas: PerillasWorker) -> tuple[Any, list[str]]:
    """Gris → deskew → escala mínima → binarización. Devuelve (imagen, notas).

    Cada paso es best-effort: si algo falla se sigue con lo que haya. Un
    preprocesado que reviente no puede costar el OCR entero de la página.
    """
    notas: list[str] = []
    lienzo = imagen.convert("L")

    # Downscale primero si viene enorme: todo lo que sigue cuesta por píxel.
    lado = max(lienzo.width, lienzo.height)
    if lado > perillas.ocr_max_lado:
        lienzo.thumbnail((perillas.ocr_max_lado, perillas.ocr_max_lado))
        notas.append("ocr_reducida")

    if perillas.ocr_deskew:
        lienzo, giro = _enderezar(lienzo)
        if giro:
            notas.append("ocr_enderezada")

    # Upscale si quedó por debajo del mínimo útil. Tesseract necesita unos 20 px de
    # alto por carácter; por debajo confunde sistemáticamente 0/O, 1/l y 5/S.
    lado = max(lienzo.width, lienzo.height)
    if lado < perillas.ocr_lado_minimo_util:
        factor = perillas.ocr_lado_minimo_util / max(lado, 1)
        if factor <= perillas.ocr_upscale_max:
            from PIL import Image

            nuevo = (max(1, int(lienzo.width * factor)), max(1, int(lienzo.height * factor)))
            lienzo = lienzo.resize(nuevo, Image.LANCZOS)
            notas.append("ocr_ampliada")

    if perillas.ocr_binarizar:
        lienzo = _binarizar(lienzo)
        notas.append("ocr_binarizada")

    return lienzo, notas


def _enderezar(lienzo: Any) -> tuple[Any, bool]:
    """Corrige la inclinación usando la orientación que reporta el propio Tesseract.

    Se usa `osd` en vez de una transformada de Hough propia para no arrastrar OpenCV
    ni numpy solo por esto: el OSD ya viene con Tesseract y da el ángulo directo.
    """
    try:
        import pytesseract

        datos = pytesseract.image_to_osd(lienzo, output_type=pytesseract.Output.DICT)
        angulo = float(datos.get("rotate", 0) or 0)
    except Exception:
        return lienzo, False  # sin OSD (o falló): se sigue con la imagen tal cual
    if abs(angulo) < 0.5:
        return lienzo, False
    try:
        # `expand` para no recortar las esquinas al girar; el relleno en blanco no
        # molesta a Tesseract, que ignora el margen.
        return lienzo.rotate(-angulo, expand=True, fillcolor=255), True
    except Exception:
        return lienzo, False


def _binarizar(lienzo: Any) -> Any:
    """Umbral global de Otsu sobre el histograma. Suficiente para escaneos de
    iluminación uniforme, que es el caso normal de un documento; una binarización
    adaptativa (Sauvola) exigiría numpy y solo gana en fotos con sombra."""
    try:
        histograma = lienzo.histogram()[:256]
        umbral = _otsu(histograma)
        return lienzo.point(lambda p: 255 if p > umbral else 0, mode="L")
    except Exception:
        return lienzo


def _otsu(histograma: list[int]) -> int:
    """Umbral que maximiza la varianza entre clases. Implementación directa: son 256
    cubetas, no vale la pena traer una dependencia."""
    total = sum(histograma)
    if total == 0:
        return 128
    suma_total = sum(i * h for i, h in enumerate(histograma))
    suma_fondo = 0.0
    peso_fondo = 0
    mejor_varianza = -1.0
    mejor_umbral = 128
    for i, h in enumerate(histograma):
        peso_fondo += h
        if peso_fondo == 0:
            continue
        peso_frente = total - peso_fondo
        if peso_frente == 0:
            break
        suma_fondo += i * h
        media_fondo = suma_fondo / peso_fondo
        media_frente = (suma_total - suma_fondo) / peso_frente
        varianza = peso_fondo * peso_frente * (media_fondo - media_frente) ** 2
        if varianza > mejor_varianza:
            mejor_varianza = varianza
            mejor_umbral = i
    return mejor_umbral


def ocr_imagen(
    imagen: Any, perillas: PerillasWorker
) -> tuple[str | None, list[str], float | None]:
    """OCR de una imagen PIL ya abierta.

    Devuelve `(texto|None, flags, confianza|None)`. La confianza es la media
    ponderada por longitud de palabra en 0-100: una palabra larga bien leída dice
    más de la calidad de la página que un "el" suelto.

    Nunca lanza: los fallos se reportan como flags.
    """
    lado = max(imagen.width, imagen.height)
    if lado < perillas.ocr_min_lado:
        return None, ["ocr_saltado_pequena"], None
    try:
        import pytesseract  # dependencia opcional (extra `ocr`)
    except Exception:  # sin el paquete instalado, se degrada con gracia
        return None, ["ocr_no_disponible"], None

    try:
        lienzo, notas = _preparar(imagen, perillas)
    except Exception as exc:
        return None, [f"ocr_preparado_fallido:{type(exc).__name__}"], None

    try:
        datos = pytesseract.image_to_data(
            lienzo, lang=perillas.ocr_idiomas, output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractNotFoundError:
        return None, ["ocr_no_disponible"], None
    except Exception as exc:  # OCR falla → flag, jamás tumba la corrida
        return None, [f"ocr_fallido:{type(exc).__name__}", *notas], None

    texto, confianza = _texto_y_confianza(datos)
    if not texto:
        return None, ["ocr_vacio", *notas], None

    flags = ["ocr_ok", *notas]
    if confianza is not None and confianza < perillas.ocr_confianza_min:
        # No se descarta aquí: el que decide qué hacer con un texto dudoso es el
        # llamador (el plugin), que sabe si tiene alternativas. Aquí solo se marca.
        flags.append("ocr_confianza_baja")
    return texto[: perillas.extractor_max_chars], flags, confianza


def _texto_y_confianza(datos: dict[str, Any]) -> tuple[str, float | None]:
    """Reconstruye el texto y su confianza media desde la salida de `image_to_data`.

    Se respetan los saltos de línea que Tesseract ya identificó (`line_num` /
    `block_num`): un documento con columnas o tablas queda ilegible si todas sus
    palabras se pegan con espacios, y el backfill de entidades busca patrones que
    un salto perdido puede partir en dos.
    """
    palabras: list[str] = datos.get("text") or []
    confianzas: list[Any] = datos.get("conf") or []
    lineas: list[Any] = datos.get("line_num") or []
    bloques: list[Any] = datos.get("block_num") or []

    partes: list[str] = []
    clave_previa: tuple[Any, Any] | None = None
    suma_pesos = 0.0
    suma_conf = 0.0

    for i, palabra in enumerate(palabras):
        limpia = (palabra or "").strip()
        if not limpia:
            continue
        try:
            conf = float(confianzas[i])
        except (IndexError, TypeError, ValueError):
            conf = -1.0

        clave = (bloques[i] if i < len(bloques) else 0, lineas[i] if i < len(lineas) else 0)
        if clave_previa is not None and clave != clave_previa:
            partes.append("\n")
        elif partes:
            partes.append(" ")
        partes.append(limpia)
        clave_previa = clave

        if conf > _CONFIANZA_IGNORAR:
            peso = float(len(limpia))
            suma_pesos += peso
            suma_conf += conf * peso

    texto = "".join(partes).strip()
    if suma_pesos > 0:
        return texto, round(suma_conf / suma_pesos, 1)
    # Hay texto pero NINGUNA palabra superó el filtro de confianza: Tesseract leyó
    # algo y no se cree nada de ello. Eso es confianza 0.0, no "no aplica".
    # Devolver None dejaba pasar la basura entera: el descarte por confianza ignora
    # los None a propósito (un CSV no tiene confianza que juzgar), así que este caso
    # —el peor OCR posible— era justo el que se colaba al índice sin filtrar.
    return texto, 0.0 if texto else None
