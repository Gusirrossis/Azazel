"""Tests del modelo: identidades deterministas y máquina de estados (invariantes)."""

from __future__ import annotations

import itertools

import pytest

from normalizacion.core.modelo import (
    TRANSICIONES,
    Estado,
    calcular_archivo_id,
    clave_almacen,
    es_transicion_valida,
    sanear_texto,
)


class TestSanearTexto:
    """Nombres de volúmenes no-UTF8 (RAID con archivos Latin-1) no deben romper nada."""

    def test_surrogateescape_no_revienta(self) -> None:
        # Así llega un nombre con byte 0xed (í en Latin-1) desde os.scandir en Mac/Linux
        nombre = "Jos\udcede.sql"
        limpio = sanear_texto(nombre)
        limpio.encode("utf-8")  # NO debe lanzar (antes: 'surrogates not allowed')
        assert "\udced" not in limpio

    def test_quita_nul(self) -> None:
        assert "\x00" not in sanear_texto("a\x00b")

    def test_idempotente_y_respeta_texto_valido(self) -> None:
        assert sanear_texto("ñandú café.csv") == "ñandú café.csv"
        assert sanear_texto(sanear_texto("Jos\udcede")) == sanear_texto("Jos\udcede")

    def test_archivo_id_no_revienta_con_nombre_basura(self) -> None:
        # El bug real del usuario: 'utf-8' codec can't decode/encode … byte 0xed
        calcular_archivo_id("disco:carpeta/archivo\udced.dbf", 10, 1)  # no lanza


class TestArchivoId:
    def test_es_determinista(self) -> None:
        a = calcular_archivo_id("/discos/d1/ventas.csv", 1024, 1700000000_000000000)
        b = calcular_archivo_id("/discos/d1/ventas.csv", 1024, 1700000000_000000000)
        assert a == b

    def test_separadores_de_windows_y_linux_dan_el_mismo_id(self) -> None:
        a = calcular_archivo_id(r"discos\d1\ventas.csv", 10, 1)
        b = calcular_archivo_id("discos/d1/ventas.csv", 10, 1)
        assert a == b

    def test_cambio_de_mtime_da_id_nuevo(self) -> None:
        """Archivo cambiado → id nuevo → se reprocesa solo ese (re-scan incremental)."""
        a = calcular_archivo_id("/d/x.csv", 10, 1)
        b = calcular_archivo_id("/d/x.csv", 10, 2)
        assert a != b

    def test_cambio_de_tamano_da_id_nuevo(self) -> None:
        assert calcular_archivo_id("/d/x.csv", 10, 1) != calcular_archivo_id("/d/x.csv", 11, 1)


class TestClaveAlmacen:
    def test_estructura_content_addressed(self) -> None:
        h = "abcd" + "0" * 60
        assert clave_almacen(h) == f"ab/cd/{h}"


class TestMaquinaDeEstados:
    def test_camino_feliz_completo(self) -> None:
        camino = [
            Estado.PENDIENTE,
            Estado.PRECALIFICADO,
            Estado.EN_PROCESO,
            Estado.INDEXADO,
            Estado.VERIFICADO,
            Estado.HECHO,
        ]
        for de, a in itertools.pairwise(camino):
            assert es_transicion_valida(de, a), f"{de} → {a} debería ser válida"

    def test_hecho_es_terminal(self) -> None:
        assert TRANSICIONES[Estado.HECHO] == frozenset()

    def test_no_se_salta_la_verificacion(self) -> None:
        """INVARIANTE: nada llega a HECHO sin pasar por VERIFICADO."""
        assert not es_transicion_valida(Estado.INDEXADO, Estado.HECHO)
        assert not es_transicion_valida(Estado.EN_PROCESO, Estado.HECHO)
        assert not es_transicion_valida(Estado.PENDIENTE, Estado.HECHO)

    def test_cold_es_reversible(self) -> None:
        """INVARIANTE never-delete: el frío se puede re-puntuar (COLD → PRECALIFICADO)."""
        assert es_transicion_valida(Estado.COLD, Estado.PRECALIFICADO)

    def test_error_es_reprocesable(self) -> None:
        assert es_transicion_valida(Estado.ERROR, Estado.PRECALIFICADO)

    def test_cold_a_error_para_frio_fallido(self) -> None:
        """Un COLD que no se puede mover a frío (poison/IO agotado) va a ERROR y
        BLOQUEA la puerta — no se desecha el disco con datos sin respaldar."""
        assert es_transicion_valida(Estado.COLD, Estado.ERROR)

    @pytest.mark.parametrize("estado", list(Estado))
    def test_todo_estado_esta_en_la_tabla(self, estado: Estado) -> None:
        assert estado in TRANSICIONES
