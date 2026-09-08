"""Imágenes listas para viajar a un consumidor federado.

Lo que se protege aquí es el TAMAÑO de lo que sale por el cable. Una respuesta de
búsqueda pesa hoy 66 KB; un escaneo sin reescalar la pone en decenas de MB, y en
base64 un 33 % más. Los tests de encoger y de no-ampliar no son cosméticos: son el
contrato con el otro lado de una conexión que ya cuesta segundos solo en abrirse.
"""

from __future__ import annotations

import base64
import io

import pytest

from normalizacion.core import imagen_transporte as it


def _png(ancho: int, alto: int, *, alpha: bool = False, color: str = "white") -> io.BytesIO:
    from PIL import Image

    imagen = Image.new("RGBA" if alpha else "RGB", (ancho, alto), color)
    buf = io.BytesIO()
    imagen.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _jpeg(ancho: int, alto: int) -> io.BytesIO:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (ancho, alto), "white").save(buf, format="JPEG")
    buf.seek(0)
    return buf


class TestTamano:
    def test_encoge_al_lado_mayor(self) -> None:
        r = it.preparar(_png(3000, 2000), max_lado=800)
        assert r.redimensionada is True
        assert max(r.ancho, r.alto) == 800
        assert (r.ancho_original, r.alto_original) == (3000, 2000)

    def test_conserva_la_proporcion(self) -> None:
        """Deformar un escaneo lo vuelve inservible para leerlo."""
        r = it.preparar(_png(3000, 1500), max_lado=600)
        assert (r.ancho, r.alto) == (600, 300)

    def test_no_amplia_una_pequena(self) -> None:
        """Estirar no añade información y multiplica los bytes que viajan."""
        r = it.preparar(_png(120, 90), max_lado=1600)
        assert r.redimensionada is False
        assert (r.ancho, r.alto) == (120, 90)

    def test_encoger_reduce_los_bytes_de_verdad(self) -> None:
        """El objetivo del módulo, medido: la versión de transporte pesa mucho menos."""
        from PIL import Image

        grande = Image.new("RGB", (2400, 1800))
        # Ruido determinista: una imagen de color plano comprime tanto que no mediría
        # nada. Con contenido real la diferencia es la que se quiere comprobar.
        pixeles = grande.load()
        assert pixeles is not None
        for y in range(0, 1800, 3):
            for x in range(0, 2400, 3):
                pixeles[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
        buf = io.BytesIO()
        grande.save(buf, format="PNG")
        bytes_original = len(buf.getvalue())
        buf.seek(0)

        r = it.preparar(buf, max_lado=800)
        assert r.bytes_salida < bytes_original / 4


class TestFormato:
    def test_sin_alpha_sale_jpeg(self) -> None:
        r = it.preparar(_jpeg(400, 300), max_lado=1600)
        assert r.tipo == "image/jpeg"

    def test_con_alpha_se_conserva_png(self) -> None:
        """Pasar RGBA a JPEG pinta el fondo de negro y arruina un recorte."""
        r = it.preparar(_png(400, 300, alpha=True), max_lado=1600)
        assert r.tipo == "image/png"

    def test_el_base64_es_decodificable_y_es_la_imagen(self) -> None:
        from PIL import Image

        r = it.preparar(_png(500, 400), max_lado=200)
        crudo = base64.b64decode(r.base64)
        assert len(crudo) == r.bytes_salida
        reabierta = Image.open(io.BytesIO(crudo))
        assert reabierta.size == (r.ancho, r.alto) == (200, 160)


class TestDefensas:
    def test_rechaza_una_bomba_de_pixeles(self) -> None:
        """Una imagen construida para reventar al que la abra declara dimensiones
        enormes con muy pocos bytes. Pillow avisa pero no lo impide por defecto."""
        with pytest.raises(it.ImagenDemasiadoGrande):
            it.preparar(_png(400, 400), max_lado=800, max_pixeles=1000)

    def test_lo_que_no_es_imagen_no_revienta(self) -> None:
        """Un PDF renombrado a .jpg tiene que dar un error tipado, no un traceback."""
        with pytest.raises(it.NoEsImagen):
            it.preparar(io.BytesIO(b"%PDF-1.4 esto no es una imagen"), max_lado=800)


class TestEsImagen:
    def test_manda_el_tipo_real_sobre_la_extension(self) -> None:
        """El tipo real lo detecta la precalificación por CONTENIDO. Un `.jpg` que en
        realidad es un PDF no debe entrar al camino de imagen."""
        assert it.es_imagen("application/pdf", ".jpg") is False
        assert it.es_imagen("image/jpeg", ".pdf") is True

    def test_sin_tipo_real_cae_a_la_extension(self) -> None:
        assert it.es_imagen(None, ".TIFF") is True
        assert it.es_imagen(None, ".csv") is False

    def test_sin_nada_no_se_intenta(self) -> None:
        assert it.es_imagen(None, None) is False
