"""Política de OCR: qué imágenes lo merecen, el plazo cooperativo y el descarte.

Nada de esto necesita Tesseract instalado: se prueba la DECISIÓN, no el motor. Que es
justo lo que hay que blindar — el motor degrada solo con una bandera, pero una mala
decisión de política se traduce en una factura de OCR sin techo o en texto inventado
metiendo personas falsas en la base.
"""

from __future__ import annotations

import io
import time

import pytest

from normalizacion.core.config import PerillasFiltro, PerillasWorker
from normalizacion.ingesta.workers.extractores import ContextoExtraccion, ResultadoExtraccion

pytest.importorskip("PIL", reason="el clasificador de imágenes necesita Pillow")


def _png(ancho: int, alto: int, color: tuple[int, int, int]) -> io.BytesIO:
    """Imagen de un color plano."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (ancho, alto), color).save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _escaneo(ancho: int = 1200, alto: int = 1600) -> io.BytesIO:
    """Página blanca con renglones de tinta: lo que de verdad parece un documento.

    Un color plano NO sirve como escaneo de prueba: sin píxeles oscuros el
    clasificador lo llama `pagina_en_blanco`, y con razón — una hoja sin tinta no
    tiene nada que leer y no debe gastar OCR.
    """
    from PIL import Image, ImageDraw

    imagen = Image.new("RGB", (ancho, alto), (252, 251, 248))
    lapiz = ImageDraw.Draw(imagen)
    for fila in range(14):
        y = 180 + fila * 90
        lapiz.rectangle([160, y, ancho - 220, y + 34], fill=(28, 28, 30))
    buffer = io.BytesIO()
    imagen.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


class TestClasificadorImagen:
    """Separa el escaneo de la fotografía sin pagar OCR."""

    def test_una_imagen_pequena_no_es_documento(self) -> None:
        """Íconos, avatares y miniaturas: la mayor parte de las imágenes de un disco."""
        from normalizacion.ingesta.precalificacion import imagen

        clase = imagen.clasificar(_png(64, 64, (255, 255, 255)), ancho_min=600)
        assert clase.es_documento is False
        assert clase.motivo == "imagen_pequena"

    def test_un_banner_alargado_no_es_documento(self) -> None:
        """Ningún formato de papel llega a 3:1."""
        from normalizacion.ingesta.precalificacion import imagen

        clase = imagen.clasificar(_png(3000, 400, (255, 255, 255)), ancho_min=600)
        assert clase.es_documento is False
        assert clase.motivo == "aspecto_no_documento"

    def test_una_pagina_en_blanco_no_tiene_nada_que_leer(self) -> None:
        from normalizacion.ingesta.precalificacion import imagen

        clase = imagen.clasificar(_png(1200, 1600, (255, 255, 255)), ancho_min=600)
        assert clase.es_documento is False
        assert clase.motivo == "pagina_en_blanco"

    def test_una_imagen_de_color_saturado_es_fotografia(self) -> None:
        """El papel no es naranja. Una foto sí."""
        from normalizacion.ingesta.precalificacion import imagen

        clase = imagen.clasificar(_png(1200, 1600, (220, 90, 20)), ancho_min=600)
        assert clase.es_documento is False
        assert clase.motivo == "fotografia"

    def test_una_pagina_con_tinta_es_documento(self) -> None:
        """Papel claro, sin color y CON píxeles oscuros: la firma de un escaneo."""
        from normalizacion.ingesta.precalificacion import imagen

        clase = imagen.clasificar(_escaneo(), ancho_min=600)
        assert clase.es_documento is True
        assert clase.motivo == "escaneo_probable"

    def test_ante_un_fallo_decide_procesar(self) -> None:
        """Recall primero: no poder clasificar es razón para no ahorrar, no para
        descartar. Un documento mandado a frío por error se pierde de vista."""
        from normalizacion.ingesta.precalificacion import imagen

        clase = imagen.clasificar(io.BytesIO(b"esto no es una imagen"), ancho_min=600)
        assert clase.es_documento is True
        assert clase.motivo == "clasificacion_fallida"


class TestRouterDeImagenes:
    """El destino que el precalificador le da a una imagen según la política."""

    def _rutear(self, politica: str, fuente: io.BytesIO):  # type: ignore[no-untyped-def]
        from normalizacion.ingesta.precalificacion import reglas

        perillas = PerillasFiltro(ocr_activo=True, ocr_politica_imagen=politica)
        return reglas._rutear_imagen(perillas, "image/png", {}, fuente)

    def test_politica_ninguna_manda_todo_a_frio(self) -> None:
        r = self._rutear("ninguna", _escaneo())
        assert r.ruta.value == "COLD"

    def test_politica_todas_manda_todo_a_hot(self) -> None:
        """El comportamiento anterior, conservado a propósito para una pasada
        exhaustiva cuando se quiera pagar por ella."""
        r = self._rutear("todas", _png(1200, 1600, (220, 90, 20)))
        assert r.ruta.value == "HOT"

    def test_politica_escaneo_filtra_la_fotografia(self) -> None:
        r = self._rutear("escaneo", _png(1200, 1600, (220, 90, 20)))
        assert r.ruta.value == "COLD"
        assert r.motivo.startswith("imagen_no_ocr:")

    def test_politica_escaneo_deja_pasar_el_documento(self) -> None:
        r = self._rutear("escaneo", _escaneo())
        assert r.ruta.value == "HOT"
        assert r.motivo.startswith("imagen_ocr:")

    def test_lo_descartado_va_a_frio_no_a_error(self) -> None:
        """COLD es REVERSIBLE: si mañana se decide OCR-ear también las fotos,
        `rescore-frio` las devuelve a la cola sin haber perdido nada."""
        r = self._rutear("escaneo", _png(64, 64, (255, 255, 255)))
        assert r.ruta.value == "COLD"


class TestPlazoCooperativo:
    """El plazo que evita tirar a la basura páginas ya reconocidas."""

    def _ctx(self, plazo: float | None) -> ContextoExtraccion:
        return ContextoExtraccion(
            fuente=io.BytesIO(b""), nombre="x.pdf", tipo_real="application/pdf",
            tamano=0, perillas=PerillasWorker(), plazo=plazo,
        )

    def test_sin_plazo_nunca_vence(self) -> None:
        ctx = self._ctx(None)
        assert ctx.vencido() is False
        assert ctx.restante() == float("inf")

    def test_un_plazo_pasado_esta_vencido(self) -> None:
        assert self._ctx(time.monotonic() - 1).vencido() is True

    def test_un_plazo_futuro_no(self) -> None:
        ctx = self._ctx(time.monotonic() + 60)
        assert ctx.vencido() is False
        assert 0 < ctx.restante() <= 60

    def test_el_restante_nunca_es_negativo(self) -> None:
        assert self._ctx(time.monotonic() - 100).restante() == 0.0


class TestDescartePorConfianza:
    """Un texto inventado es peor que ningún texto: ensucia la búsqueda y mete
    anclas falsas en la resolución de entidades."""

    def _config(self, umbral: float):  # type: ignore[no-untyped-def]
        from normalizacion.core.config import Config

        return Config(filtro=PerillasFiltro(ocr_confianza_descarte=umbral))

    def _descartar(self, umbral: float, confianza: float | None):  # type: ignore[no-untyped-def]
        from normalizacion.ingesta.workers.orquestador import _aplicar_descarte

        return _aplicar_descarte(
            self._config(umbral),
            ResultadoExtraccion(texto="algo leido", confianza=confianza, flags=["ocr_ok"]),
        )

    def test_por_debajo_del_umbral_el_texto_no_va_al_indice(self) -> None:
        r = self._descartar(40.0, 25.0)
        assert r.texto is None
        assert "ocr_descartado_confianza" in r.flags
        # La confianza SÍ se conserva: es lo que permite encontrarlo después con
        # `norm reextraer --confianza-menor`.
        assert r.confianza == 25.0

    def test_por_encima_del_umbral_pasa(self) -> None:
        assert self._descartar(40.0, 85.0).texto == "algo leido"

    def test_sin_confianza_no_se_descarta(self) -> None:
        """Un CSV o un PDF con texto nativo no tienen confianza: tienen el texto
        exacto. Tratar `None` como 0 borraría la mayor parte del corpus."""
        assert self._descartar(40.0, None).texto == "algo leido"

    def test_umbral_cero_desactiva_el_descarte(self) -> None:
        assert self._descartar(0.0, 5.0).texto == "algo leido"


class TestConfianzaCero:
    """Una página donde Tesseract no se cree NINGUNA palabra devolvía confianza
    `None`, y el descarte ignora los None a propósito (un CSV no tiene confianza que
    juzgar). Resultado: el peor OCR posible era justo el que se colaba sin filtrar."""

    def test_texto_sin_ninguna_palabra_confiable_es_cero_no_none(self) -> None:
        from normalizacion.ingesta.workers.extractores._ocr import _texto_y_confianza

        datos = {
            "text": ["|||", "l1", "0O"],
            "conf": [-1, -1, -1],  # Tesseract marca así lo que no reconoce
            "line_num": [1, 1, 1],
            "block_num": [1, 1, 1],
        }
        texto, confianza = _texto_y_confianza(datos)
        assert texto, "hay texto: basura, pero texto"
        assert confianza == 0.0, "0.0 se descarta; None se colaría al índice"

    def test_sin_texto_alguno_si_es_none(self) -> None:
        from normalizacion.ingesta.workers.extractores._ocr import _texto_y_confianza

        texto, confianza = _texto_y_confianza({"text": ["", "  "], "conf": [-1, -1]})
        assert texto == ""
        assert confianza is None

    def test_la_confianza_pondera_por_longitud(self) -> None:
        """Una palabra larga bien leída dice más de la página que un 'el' suelto."""
        from normalizacion.ingesta.workers.extractores._ocr import _texto_y_confianza

        datos = {
            "text": ["el", "RAMIREZ"],
            "conf": [10.0, 90.0],
            "line_num": [1, 1],
            "block_num": [1, 1],
        }
        _texto, confianza = _texto_y_confianza(datos)
        assert confianza is not None and confianza > 70, "la palabra larga debe pesar más"
