"""Tests del constructor de DSL (puro): allowlist, límites duros y no-inyección."""

from __future__ import annotations

from normalizacion.api.busqueda import construir_consulta
from normalizacion.api.esquemas import SolicitudBusqueda


class TestConstruccion:
    def test_sin_filtros_es_match_all(self) -> None:
        cuerpo = construir_consulta(SolicitudBusqueda(), pagina_max=100)
        assert cuerpo["query"] == {"match_all": {}}
        assert cuerpo["size"] == 20

    def test_texto_busca_nombre_Y_contenido(self) -> None:
        """Escribir un nombre de persona encuentra los PDFs que la MENCIONAN."""
        cuerpo = construir_consulta(SolicitudBusqueda(texto="Ventas"), pagina_max=100)
        should = cuerpo["query"]["bool"]["must"][0]["bool"]["should"]
        assert should[0]["wildcard"]["nombre"]["value"] == "*ventas*"
        match = should[1]["match"]["texto_indexable"]
        assert match["query"] == "Ventas"
        assert match["operator"] == "and"  # todas las palabras del nombre deben aparecer

    def test_texto_activa_resaltado_y_relevancia(self) -> None:
        cuerpo = construir_consulta(SolicitudBusqueda(texto="valeria"), pagina_max=100)
        assert "texto_indexable" in cuerpo["highlight"]["fields"]
        assert next(iter(cuerpo["sort"][0])) == "_score"  # el mejor match primero

    def test_filtros_combinados(self) -> None:
        s = SolicitudBusqueda(texto="x", tipo_real="text/csv", puntaje_min=70, tamano_min=10)
        consulta = construir_consulta(s, pagina_max=100)["query"]["bool"]
        assert len(consulta["filter"]) == 3
        assert len(consulta["must"]) == 1

    def test_sort_estable_para_search_after(self) -> None:
        """Sin texto: por puntaje, con tiebreaker (paginación sin huecos ni duplicados)."""
        cuerpo = construir_consulta(SolicitudBusqueda(), pagina_max=100)
        assert [next(iter(s)) for s in cuerpo["sort"]] == ["puntaje", "archivo_id"]

    def test_cursor_se_traduce_a_search_after(self) -> None:
        cuerpo = construir_consulta(SolicitudBusqueda(cursor=[70, "abc"]), pagina_max=100)
        assert cuerpo["search_after"] == [70, "abc"]


class TestLimitesDuros:
    def test_pagina_acotada_al_maximo_del_servidor(self) -> None:
        cuerpo = construir_consulta(SolicitudBusqueda(tamano_pagina=99999), pagina_max=100)
        assert cuerpo["size"] == 100

    def test_texto_largo_lo_rechaza_el_esquema(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SolicitudBusqueda(texto="x" * 500)


class TestNoInyeccion:
    def test_sintaxis_dsl_en_texto_queda_inerte(self) -> None:
        """Un intento de inyección viaja como VALOR literal, no como query."""
        maligno = '"}}, "script": {"source": "borrar todo"'
        cuerpo = construir_consulta(SolicitudBusqueda(texto=maligno), pagina_max=100)
        assert "script" not in cuerpo
        should = cuerpo["query"]["bool"]["must"][0]["bool"]["should"]
        assert maligno.lower() in should[0]["wildcard"]["nombre"]["value"]
        assert should[1]["match"]["texto_indexable"]["query"] == maligno

    def test_no_existe_campo_de_consulta_libre(self) -> None:
        """El esquema NO tiene forma de pasar DSL: campos extra se rechazan (forbid)."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SolicitudBusqueda.model_validate({"query": {"match_all": {}}})
