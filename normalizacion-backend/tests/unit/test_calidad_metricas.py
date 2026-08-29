"""Las métricas del conjunto dorado: funciones puras, sin BD ni OCR.

Importa que estén bien porque son el instrumento de medida: si el metro está mal
calibrado, todas las decisiones que se tomen con él estarán mal y nadie lo notará.
"""

from __future__ import annotations

import pytest

from normalizacion.calidad import metricas


class TestCER:
    def test_texto_identico_no_tiene_error(self) -> None:
        assert metricas.cer("Juan Pérez Ramírez", "Juan Pérez Ramírez") == 0.0

    def test_los_acentos_no_cuentan_como_error(self) -> None:
        """El OCR destroza acentos constantemente y eso no cambia a quién resuelve:
        `RAMIREZ` y `Ramírez` son la misma persona. Contarlos hincharía el CER sin
        que corresponda a nada que importe."""
        assert metricas.cer("Ramírez Muñoz", "Ramirez Munoz") == 0.0

    def test_las_mayusculas_tampoco(self) -> None:
        assert metricas.cer("Juan Pérez", "JUAN PEREZ") == 0.0

    def test_los_saltos_de_linea_no_cuentan(self) -> None:
        """Tesseract reparte los saltos distinto en cada corrida; no es un error
        de lectura y no debe mover la métrica."""
        assert metricas.cer("linea uno\nlinea dos", "linea uno linea dos") == 0.0

    def test_un_caracter_mal_en_diez(self) -> None:
        assert metricas.cer("abcdefghij", "abcdefghix") == pytest.approx(0.1)

    def test_verdad_vacia_con_texto_inventado_es_error_total(self) -> None:
        """Una página en blanco de la que el OCR "lee" algo: es alucinación pura."""
        assert metricas.cer("", "texto que no existe") == 1.0

    def test_verdad_vacia_y_salida_vacia_es_perfecto(self) -> None:
        assert metricas.cer("", "") == 0.0


class TestWER:
    def test_texto_identico(self) -> None:
        assert metricas.wer("uno dos tres", "uno dos tres") == 0.0

    def test_una_palabra_de_tres(self) -> None:
        assert metricas.wer("uno dos tres", "uno dos cuatro") == pytest.approx(0.3333, abs=1e-3)

    def test_palabra_perdida(self) -> None:
        assert metricas.wer("uno dos tres", "uno tres") == pytest.approx(0.3333, abs=1e-3)


class TestLevenshtein:
    @pytest.mark.parametrize(
        ("a", "b", "espera"),
        [
            ("", "", 0),
            ("abc", "abc", 0),
            ("abc", "", 3),
            ("", "abc", 3),
            ("gato", "pato", 1),
            ("gato", "gatos", 1),
            ("gatos", "gato", 1),
        ],
    )
    def test_casos(self, a: str, b: str, espera: int) -> None:
        assert metricas.distancia_levenshtein(a, b) == espera

    def test_es_simetrica(self) -> None:
        ida = metricas.distancia_levenshtein("kitten", "sitting")
        vuelta = metricas.distancia_levenshtein("sitting", "kitten")
        assert ida == vuelta


class TestAnclas:
    """El recall de anclas es LA métrica: lo que no se lee, no existe para el sistema."""

    def test_todas_encontradas(self) -> None:
        r = metricas.evaluar_anclas(["ABCD800101HDFXYZ01"], ["ABCD800101HDFXYZ01"])
        assert r.recall == 1.0
        assert r.precision == 1.0

    def test_una_de_dos_perdida(self) -> None:
        r = metricas.evaluar_anclas(
            ["AAAA800101HDFXYZ01", "BBBB900202MDFXYZ02"], ["AAAA800101HDFXYZ01"]
        )
        assert r.recall == 0.5
        assert r.precision == 1.0
        assert r.faltantes == ["BBBB900202MDFXYZ02"]

    def test_ancla_inventada_baja_la_precision(self) -> None:
        """Una CURP alucinada crea una persona que no existe. Cuesta más limpiarla
        que no haberla creado, así que tiene que verse en la métrica."""
        r = metricas.evaluar_anclas(
            ["AAAA800101HDFXYZ01"], ["AAAA800101HDFXYZ01", "ZZZZ000000HZZZZZ99"]
        )
        assert r.recall == 1.0
        assert r.precision == 0.5
        assert r.inventadas == ["ZZZZ000000HZZZZZ99"]

    def test_se_normaliza_a_mayusculas(self) -> None:
        r = metricas.evaluar_anclas(["aaaa800101hdfxyz01"], ["AAAA800101HDFXYZ01"])
        assert r.correctas == 1

    def test_documento_sin_anclas_no_penaliza(self) -> None:
        """Una foto de una playa no tiene anclas que perder: su recall es perfecto."""
        r = metricas.evaluar_anclas([], [])
        assert r.recall == 1.0
        assert r.precision == 1.0


class TestAgregado:
    def _medicion(self, doc: str, esperadas: int, correctas: int) -> metricas.Medicion:
        return metricas.Medicion(
            documento=doc,
            cer=0.1,
            wer=0.2,
            anclas=metricas.ResultadoAnclas(
                esperadas=esperadas, encontradas=correctas, correctas=correctas
            ),
            confianza=80.0,
            ms=100,
            chars=500,
        )

    def test_el_recall_se_agrega_por_anclas_no_por_documento(self) -> None:
        """Un padrón con 200 CURPs no puede pesar lo mismo que un oficio con una.
        Promediar los porcentajes por documento daría 0.75; lo correcto es 100/101."""
        mediciones = [self._medicion("padron", 100, 100), self._medicion("oficio", 1, 0)]
        resumen = metricas.agregar(mediciones)
        assert resumen["recall_anclas"] == pytest.approx(100 / 101, abs=1e-4)

    def test_lista_los_peores_para_saber_por_donde_empezar(self) -> None:
        mediciones = [self._medicion("bueno", 10, 10), self._medicion("malo", 10, 1)]
        assert metricas.agregar(mediciones)["peores"][0] == "malo"

    def test_conjunto_vacio_no_revienta(self) -> None:
        assert metricas.agregar([])["documentos"] == 0


class TestComparar:
    def test_reporta_el_delta_de_cada_metrica(self) -> None:
        antes = {"recall_anclas": 0.70, "cer_medio": 0.20, "ms_medio": 100}
        despues = {"recall_anclas": 0.85, "cer_medio": 0.12, "ms_medio": 180}
        d = metricas.comparar(antes, despues)
        assert d["recall_anclas"]["delta"] == pytest.approx(0.15)
        assert d["cer_medio"]["delta"] == pytest.approx(-0.08)
        # El coste también se compara: una mejora que triplica el tiempo por página
        # es una decisión, no una victoria automática.
        assert d["ms_medio"]["delta"] == 80

    def test_ignora_las_claves_que_no_son_numeros(self) -> None:
        d = metricas.comparar({"confianza_media": None}, {"confianza_media": 80.0})
        assert "confianza_media" not in d
