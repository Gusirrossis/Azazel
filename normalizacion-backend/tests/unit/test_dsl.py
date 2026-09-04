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


class TestSeleccionDeCampos:
    """`campos` → `_source`. Lo pide la federación con Lilith: el 59 % de cada
    respuesta es `texto_indexable`, que el consumidor no usa y cruza Europa igual.

    Es una ALLOWLIST, no un paso directo: nada de lo que llega del cliente entra en
    el cuerpo de la consulta como sintaxis."""

    def _cuerpo(self, **kw):  # type: ignore[no-untyped-def]
        from normalizacion.api.busqueda import construir_consulta
        from normalizacion.api.esquemas import SolicitudBusqueda

        return construir_consulta(SolicitudBusqueda(**kw), 100)

    def test_por_omision_no_cambia_nada(self) -> None:
        """Ningún consumidor existente debe enterarse de que esto existe."""
        assert "_source" not in self._cuerpo(texto="garcia")

    def test_pedir_campos_los_traduce_a_source(self) -> None:
        cuerpo = self._cuerpo(texto="garcia", campos=["nombre", "ruta_original"])
        assert cuerpo["_source"] == ["nombre", "ruta_original"]

    def test_un_campo_inventado_se_descarta(self) -> None:
        cuerpo = self._cuerpo(campos=["nombre", "; DROP TABLE", "../../etc/passwd"])
        assert cuerpo["_source"] == ["nombre"]

    def test_solo_campos_invalidos_devuelve_el_documento_entero(self) -> None:
        """Un `_source: []` daría documentos vacíos y parecería que no hay resultados."""
        assert "_source" not in self._cuerpo(campos=["no_existe"])

    def test_contexto_anclas_no_se_puede_pedir(self) -> None:
        """Son datos personales: ±200 caracteres alrededor de cada CURP. Está
        excluido de _source en el mapping y fuera de la allowlist a propósito."""
        assert "_source" not in self._cuerpo(campos=["contexto_anclas"])

    def test_la_allowlist_sale_del_modelo_no_de_una_lista_a_mano(self) -> None:
        """Un campo retirado del modelo deja de ser pedible en el acto."""
        from normalizacion.api.busqueda import _campos_permitidos
        from normalizacion.core.modelo import DocumentoArchivo

        assert _campos_permitidos() <= set(DocumentoArchivo.model_fields)
        assert "hash_contenido" in _campos_permitidos()

    def test_el_resaltado_sigue_llegando_aunque_se_filtren_campos(self) -> None:
        """`_resaltado` no viene de _source sino de `highlight`, así que pedir solo
        `nombre` NO debe perder los fragmentos — que es justo lo que se pinta."""
        cuerpo = self._cuerpo(texto="garcia", campos=["nombre"])
        assert "highlight" in cuerpo


class TestTextoCorto:
    """Medido contra el índice real de 390.000 documentos: `a` daba 500 tras 30 s,
    `de` 20 s, `la` 10 s, `garcia` 1,8 s. El culpable es el comodín INICIAL sobre
    `nombre`, que no puede usar el índice y recorre el campo entero.

    Importa porque quien federa manda TEXTO LIBRE de usuario: una letra suelta
    devuelve un 500 que el consumidor no distingue de "Azazel está caído"."""

    def _cuerpo(self, texto: str):  # type: ignore[no-untyped-def]
        from normalizacion.api.busqueda import construir_consulta
        from normalizacion.api.esquemas import SolicitudBusqueda

        return construir_consulta(SolicitudBusqueda(texto=texto), 100)

    def _ramas(self, texto: str):  # type: ignore[no-untyped-def]
        return self._cuerpo(texto)["query"]["bool"]["must"][0]["bool"]["should"]

    def test_texto_corto_no_usa_comodin_inicial(self) -> None:
        for corto in ("a", "de", "la"):
            ramas = self._ramas(corto)
            assert not any("wildcard" in r for r in ramas), f"{corto!r} no debe usar comodín"
            assert any("prefix" in r for r in ramas), f"{corto!r} debe usar prefijo"

    def test_texto_largo_si_usa_comodin(self) -> None:
        """Buscar 'garcia' tiene que seguir encontrando 'DELGARCIA.pdf'."""
        ramas = self._ramas("garcia")
        assert any("wildcard" in r for r in ramas)
        assert not any("prefix" in r for r in ramas)

    def test_el_contenido_se_busca_siempre(self) -> None:
        """La rama sobre texto_indexable usa el índice invertido y es barata a
        cualquier longitud: nunca se quita."""
        for t in ("a", "garcia"):
            assert any("match" in r and "texto_indexable" in r["match"] for r in self._ramas(t))

    def test_hay_timeout_para_lo_que_se_escape(self) -> None:
        """Red de seguridad: OpenSearch devuelve lo que lleve en vez de agotar el hilo."""
        assert self._cuerpo("garcia").get("timeout")

    def test_el_umbral_no_parte_una_palabra_util(self) -> None:
        """4 caracteres: 'ana' o 'luz' pierden el comodín, pero son justo los términos
        que devolvían decenas de miles de resultados inservibles."""
        from normalizacion.api.busqueda import _MIN_COMODIN_INICIAL

        assert 3 <= _MIN_COMODIN_INICIAL <= 5


class TestCamposYEntidadesNoSePelean:
    """Las dos funciones se peleaban, y salió al probarlas juntas contra producción:
    `campos` quita `texto_indexable` para ahorrar el 59% del tráfico, y el
    descubrimiento de entidades lo necesita para leer las anclas de los documentos.
    Un consumidor que usa AMBAS —el caso exacto de la federación— recibía cero
    entidades. El servidor pide lo que necesita y poda antes de responder."""

    def _cuerpo(self, **kw):  # type: ignore[no-untyped-def]
        from normalizacion.api.busqueda import construir_consulta
        from normalizacion.api.esquemas import SolicitudBusqueda

        return construir_consulta(SolicitudBusqueda(**kw), 100)

    def test_con_entidades_se_piden_las_fuentes_de_anclas(self) -> None:
        fuente = self._cuerpo(
            texto="x", campos=["nombre"], incluir_entidades=True
        )["_source"]
        assert "texto_indexable" in fuente
        assert "ruta_original" in fuente
        assert "campos_extraidos" in fuente

    def test_sin_entidades_se_respeta_lo_pedido(self) -> None:
        """Quien no quiere entidades no debe pagar el texto completo."""
        fuente = self._cuerpo(texto="x", campos=["nombre"])["_source"]
        assert fuente == ["nombre"]

    def test_no_se_duplica_lo_ya_pedido(self) -> None:
        fuente = self._cuerpo(
            texto="x", campos=["nombre", "texto_indexable"], incluir_entidades=True
        )["_source"]
        assert len(fuente) == len(set(fuente))

    def test_la_poda_quita_lo_que_no_se_pidio(self) -> None:
        from normalizacion.api.busqueda import _podar_documentos

        docs = [{"nombre": "x.pdf", "texto_indexable": "…", "ruta_original": "a/x.pdf"}]
        podados = _podar_documentos(docs, ["nombre"])
        assert podados == [{"nombre": "x.pdf"}]

    def test_la_poda_conserva_el_resaltado(self) -> None:
        """`_resaltado` no sale de _source sino del highlight: es lo que se pinta."""
        from normalizacion.api.busqueda import _podar_documentos

        docs = [{"nombre": "x.pdf", "texto_indexable": "…", "_resaltado": ["⟪x⟫"]}]
        assert _podar_documentos(docs, ["nombre"])[0]["_resaltado"] == ["⟪x⟫"]

    def test_sin_campos_no_se_poda_nada(self) -> None:
        from normalizacion.api.busqueda import _podar_documentos

        docs = [{"nombre": "x.pdf", "texto_indexable": "…"}]
        assert _podar_documentos(docs, None) == docs

    def test_las_fuentes_coinciden_con_las_de_coincidencias(self) -> None:
        """Si divergen, se pediría un campo que nadie mira o faltaría uno que sí."""
        from normalizacion.api.busqueda import _FUENTES_ANCLA
        from normalizacion.entidades.coincidencias import _FUENTES_DOC

        assert set(_FUENTES_ANCLA) == set(_FUENTES_DOC)
