"""Regresiones de la caché de extracción y del reproceso.

La auditoría destapó que estos dos módulos —los únicos nuevos que ESCRIBEN en la BD y
que pueden destruir texto ya indexado— no tenían ni un test. Los defectos que se
fijan aquí comparten una forma: no fallan, no avisan, y el resultado es texto perdido
en silencio y para siempre.
"""

from __future__ import annotations

from normalizacion.core import cache_extraccion
from normalizacion.core.config import Config, PerillasFiltro


class TestCacheableSoloLoDefinitivo:
    """Cachear un fallo lo convierte en la respuesta DEFINITIVA de ese contenido y de
    todas sus copias — y como la fila queda con la versión al día, `reextraer` deja de
    verlo. Un tesseract que aún no estaba instalado envenenaba el corpus entero."""

    def test_un_timeout_no_se_cachea(self) -> None:
        assert cache_extraccion.es_cacheable(["extraccion_timeout"]) is False

    def test_un_plugin_que_revienta_no_se_cachea(self) -> None:
        assert cache_extraccion.es_cacheable(["extraccion_fallida:ValueError"]) is False

    def test_sin_tesseract_no_se_cachea(self) -> None:
        assert cache_extraccion.es_cacheable(["ocr_no_disponible"]) is False

    def test_un_parcial_no_se_cachea(self) -> None:
        """Las páginas que faltaron valen: el contenido sigue siendo candidato."""
        assert cache_extraccion.es_cacheable(["ocr_pdf", "ocr_pdf_parcial"]) is False

    def test_un_resultado_bueno_si_se_cachea(self) -> None:
        assert cache_extraccion.es_cacheable(["ocr_ok", "ocr_enderezada"]) is True

    def test_un_texto_nativo_si_se_cachea(self) -> None:
        assert cache_extraccion.es_cacheable([]) is True

    def test_un_ocr_vacio_legitimo_si_se_cachea(self) -> None:
        """Una página en blanco leída bien es un resultado válido, no un fallo."""
        assert cache_extraccion.es_cacheable(["ocr_vacio"]) is True


class TestClaveDeVersion:
    """`ocr_activo` tiene que estar DENTRO de la clave de invalidación. Sin él, un PDF
    escaneado extraído con el OCR apagado (cero texto) servía como caché al encenderlo:
    activar el OCR no volvía a extraer nada de lo ya procesado, que es justo donde
    tiene que actuar."""

    def _config(self, ocr: bool) -> Config:
        return Config(_env_file=None, filtro=PerillasFiltro(ocr_activo=ocr))

    def test_con_y_sin_ocr_son_versiones_distintas(self) -> None:
        con = cache_extraccion.clave_version(self._config(True))
        sin = cache_extraccion.clave_version(self._config(False))
        assert con != sin

    def test_ambas_llevan_la_version_base(self) -> None:
        for ocr in (True, False):
            assert cache_extraccion.clave_version(self._config(ocr)).startswith(
                cache_extraccion.VERSION_EXTRACTOR
            )

    def test_es_estable(self) -> None:
        """Dos llamadas iguales dan lo mismo: si no, la caché nunca acertaría."""
        c = self._config(True)
        assert cache_extraccion.clave_version(c) == cache_extraccion.clave_version(c)


class TestMotorDe:
    """De qué salió el texto. Importa para el reproceso: un PDF 'nativo' sin texto no
    mejora por mucho que se afine el OCR, mientras que uno 'ocr' con confianza baja es
    justo el candidato a rehacer."""

    def test_ocr_de_pdf(self) -> None:
        assert cache_extraccion.motor_de(["ocr_pdf", "ocr_pdf_paginas:3"], "hola") == "ocr"

    def test_ocr_de_imagen(self) -> None:
        assert cache_extraccion.motor_de(["ocr_ok"], "hola") == "ocr"

    def test_texto_nativo(self) -> None:
        assert cache_extraccion.motor_de([], "hola") == "nativo"

    def test_sin_banderas_de_ocr_es_nativo(self) -> None:
        assert cache_extraccion.motor_de(["texto_truncado"], "hola") == "nativo"


class TestSaneadoDelTexto:
    """Postgres RECHAZA el byte NUL en una columna `text`. Los extractores decodifican
    con `errors="replace"`, que no toca los NUL, así que un archivo con bytes nulos
    hacía fallar el INSERT: en el worker se tragaba como warning (la caché quedaba
    inútil en silencio) y en el reproceso tiraba el lote entero."""

    def test_sanear_quita_los_nul(self) -> None:
        from normalizacion.core.modelo import sanear_texto

        assert "\x00" not in sanear_texto("antes\x00despues")

    def test_sanear_conserva_el_resto(self) -> None:
        from normalizacion.core.modelo import sanear_texto

        assert sanear_texto("Ramírez\x00Muñoz") == "RamírezMuñoz"


class TestMotorConOcrIntentado:
    """`motor_de` solo contaba las banderas de ÉXITO, así que un OCR intentado y
    fallido se etiquetaba 'nativo' — y `norm reextraer --motor ocr` no lo veía nunca,
    que es justo el conjunto que más falta hace reprocesar cuando el OCR mejora."""

    def test_un_ocr_que_no_dio_texto_no_es_nativo(self) -> None:
        assert cache_extraccion.motor_de(["ocr_vacio"], None) == "ocr_sin_texto"

    def test_un_ocr_de_confianza_baja_cuenta_como_ocr(self) -> None:
        assert cache_extraccion.motor_de(["ocr_ok", "ocr_confianza_baja"], "x") == "ocr"

    def test_un_pdf_sin_texto_nativo_sigue_siendo_nativo(self) -> None:
        """Sin banderas de OCR, al archivo no se le ha intentado: afinar el OCR no
        lo mejora, y no debe salir como candidato."""
        assert cache_extraccion.motor_de([], None) == "nativo"
