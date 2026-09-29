"""Lo que pide quien federa (Lilith) para seguir un rastro: modos de nombre, presupuesto
de tiempo, 503 cuando OpenSearch no puede y búsqueda por lotes de identificadores.

Todo es ADITIVO: sin los campos nuevos, /buscar se comporta exactamente como antes. Los
textos de estos tests son inventados.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi import HTTPException
from opensearchpy.exceptions import ConnectionError as ErrorConexionOS
from opensearchpy.exceptions import ConnectionTimeout, TransportError
from pydantic import ValidationError

from normalizacion.api import busqueda
from normalizacion.api.esquemas import ConsultaLote, Salud, SolicitudBusqueda, SolicitudLote
from normalizacion.core.config import Config


def _contenido(cuerpo: dict[str, Any]) -> dict[str, Any]:
    """La rama del CONTENIDO (la segunda; la primera es la del nombre de archivo)."""
    return cuerpo["query"]["bool"]["must"][0]["bool"]["should"][1]


def _cuerpo(**kw: Any) -> dict[str, Any]:
    return busqueda.construir_consulta(SolicitudBusqueda(**kw), 500)


class TestModo:
    """Medido el 29-09 con «juan perez lopez»: 951.693 documentos con las palabras
    sueltas (de personas distintas en un volcado SQL), 1.266 en frase, 22.188 cerca."""

    def test_sin_modo_es_el_de_siempre(self) -> None:
        assert _contenido(_cuerpo(texto="juan perez")) == {
            "match": {"texto_indexable": {"query": "juan perez", "operator": "and"}}
        }

    def test_todas_es_el_de_siempre_por_su_nombre(self) -> None:
        assert _contenido(_cuerpo(texto="juan perez", modo="todas")) == _contenido(
            _cuerpo(texto="juan perez")
        )

    def test_frase(self) -> None:
        assert _contenido(_cuerpo(texto="juan perez", modo="frase")) == {
            "match_phrase": {"texto_indexable": {"query": "juan perez"}}
        }

    def test_cerca_es_una_frase_con_holgura(self) -> None:
        rama = _contenido(_cuerpo(texto="juan perez lopez", modo="cerca"))
        assert rama["match_phrase"]["texto_indexable"]["slop"] >= 4  # reordenar 3 palabras

    def test_la_rama_del_nombre_de_archivo_sigue_primera(self) -> None:
        should = _cuerpo(texto="juan perez", modo="frase")["query"]["bool"]["must"][0]["bool"][
            "should"
        ]
        assert "wildcard" in should[0]

    def test_un_modo_desconocido_es_un_422(self) -> None:
        with pytest.raises(ValidationError):
            SolicitudBusqueda(texto="x", modo="regex")  # type: ignore[arg-type]


class TestPresupuesto:
    def test_por_omision_la_fase_de_consulta_sigue_en_15_s(self) -> None:
        assert _cuerpo(texto="x")["timeout"] == "15s"

    def test_el_presupuesto_manda_en_la_fase_de_consulta(self) -> None:
        assert _cuerpo(texto="x", presupuesto_ms=30000)["timeout"] == "30000ms"

    def test_con_techo_para_que_quepa_recuperar_los_documentos(self) -> None:
        assert _cuerpo(texto="x", presupuesto_ms=55000)["timeout"] == "50000ms"

    def test_el_plazo_del_cliente_acompana_y_no_pasa_de_58_s(self) -> None:
        assert busqueda._plazo_cliente_s(None) == 55
        assert busqueda._plazo_cliente_s(20000) == 25
        assert busqueda._plazo_cliente_s(55000) == 58

    @pytest.mark.parametrize("ms", [999, 55001])
    def test_fuera_de_rango_es_un_422(self, ms: int) -> None:
        with pytest.raises(ValidationError):
            SolicitudBusqueda(texto="x", presupuesto_ms=ms)

    def test_sigue_rechazando_campos_desconocidos(self) -> None:
        with pytest.raises(ValidationError):
            SolicitudBusqueda(texto="x", query={"match_all": {}})  # type: ignore[call-arg]


class _ClienteQueFalla:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def search(self, **_kw: Any) -> dict:
        raise self.exc


class TestNoDisponible:
    """Un «ahora no» de OpenSearch es un 503 con Retry-After, que quien federa reintenta;
    un error de verdad no se disfraza de «vuelve luego»."""

    def _buscar(self, exc: Exception) -> None:
        busqueda.buscar(_ClienteQueFalla(exc), Config(_env_file=None), SolicitudBusqueda(texto="x"))

    def test_saturado_429_es_503_con_retry_after(self) -> None:
        with pytest.raises(HTTPException) as e:
            self._buscar(TransportError(429, "es_rejected_execution_exception", {}))
        assert e.value.status_code == 503
        assert e.value.headers and e.value.headers["Retry-After"].isdigit()

    def test_sin_conexion_es_503(self) -> None:
        with pytest.raises(HTTPException) as e:
            self._buscar(ErrorConexionOS("N/A", "Connection refused", Exception()))
        assert e.value.status_code == 503

    def test_el_plazo_sigue_siendo_504(self) -> None:
        with pytest.raises(HTTPException) as e:
            self._buscar(ConnectionTimeout("TIMEOUT", "lento", Exception()))
        assert e.value.status_code == 504

    def test_un_400_no_se_esconde(self) -> None:
        with pytest.raises(TransportError):
            self._buscar(TransportError(400, "search_phase_execution_exception", {}))


class TestNormalizar:
    @pytest.mark.parametrize(
        ("tipo", "texto", "esperado"),
        [
            ("curp", " pegj-850315 hdfrrn09 ", "PEGJ850315HDFRRN09"),
            ("rfc", "pegj-850315-ab1", "PEGJ850315AB1"),
            ("nss", "123 4567 8901", "12345678901"),
            ("telefono", "+52 (55) 1234-5678", "5512345678"),
            ("telefono", "55.1234.5678", "5512345678"),
            ("correo", " Juan.Perez@Correo.COM ", "juan.perez@correo.com"),
            ("nombre", "  Juan Pérez ", "Juan Pérez"),
            ("curp", " -- ", None),
        ],
    )
    def test_forma_canonica(self, tipo: str, texto: str, esperado: str | None) -> None:
        assert busqueda.normalizar_identificador(tipo, texto) == esperado


def _frases(query: dict[str, Any]) -> list[str]:
    return [
        r["match_phrase"]["texto_indexable"]["query"]
        for r in query["bool"]["should"]
        if "match_phrase" in r
    ]


class TestConsultaLote:
    def test_un_telefono_se_busca_en_sus_formas_escritas(self) -> None:
        q = busqueda.consulta_lote(ConsultaLote(id="t", texto="+52 55 1234 5678", tipo="telefono"))
        assert q is not None
        assert set(_frases(q)) >= {"5512345678", "55 1234 5678", "551 234 5678"}

    def test_una_curp_es_exacta_y_mira_tambien_el_nombre_del_archivo(self) -> None:
        q = busqueda.consulta_lote(ConsultaLote(id="c", texto="pegj850315hdfrrn09", tipo="curp"))
        assert q is not None
        assert _frases(q) == ["PEGJ850315HDFRRN09"]
        comodines = [r for r in q["bool"]["should"] if "wildcard" in r]
        assert comodines[0]["wildcard"]["nombre"]["value"] == "*pegj850315hdfrrn09*"

    @pytest.mark.parametrize(
        ("tipo", "texto"), [("telefono", "5512345678"), ("nss", "12345678901")]
    )
    def test_las_cifras_no_se_buscan_en_el_nombre_del_archivo(self, tipo: str, texto: str) -> None:
        """«000» o «555» están en casi todos los nombres: el comodín revisaría millones."""
        q = busqueda.consulta_lote(ConsultaLote(id="x", texto=texto, tipo=tipo))  # type: ignore[arg-type]
        assert q is not None
        assert not [r for r in q["bool"]["should"] if "wildcard" in r]

    def test_un_nombre_va_en_frase_por_omision(self) -> None:
        q = busqueda.consulta_lote(ConsultaLote(id="n", texto="juan perez", tipo="nombre"))
        assert q is not None
        assert "match_phrase" in q["bool"]["should"][1]

    def test_sin_tipo_es_como_buscar(self) -> None:
        q = busqueda.consulta_lote(ConsultaLote(id="x", texto="juan perez"))
        ramas = busqueda._ramas_de_texto("juan perez")
        assert q == {"bool": {"should": ramas, "minimum_should_match": 1}}

    def test_ids_repetidos_son_un_422(self) -> None:
        with pytest.raises(ValidationError):
            SolicitudLote(
                consultas=[ConsultaLote(id="a", texto="x"), ConsultaLote(id="a", texto="y")]
            )

    def test_mas_de_150_consultas_es_un_422(self) -> None:
        with pytest.raises(ValidationError):
            SolicitudLote(consultas=[ConsultaLote(id=str(i), texto="x") for i in range(151)])


def _respuesta(n: int, *, parcial: bool = False) -> dict[str, Any]:
    hits = [
        {"_source": {"archivo_id": f"a{i}", "nombre": f"n{i}"}, "sort": [1.0, f"a{i}"],
         "highlight": {"texto_indexable": ["…"]}}
        for i in range(n)
    ]
    return {"hits": {"hits": hits, "total": {"value": n}}, "timed_out": parcial,
            "_shards": {"failed": 0}}


def _identificador(cuerpo: dict[str, Any]) -> str:
    return cuerpo["query"]["bool"]["should"][0]["match_phrase"]["texto_indexable"]["query"]


class _Cliente:
    """OpenSearch falso: responde por identificador. Anota el orden de las llamadas y los
    cuerpos; `reloj`, si se da, avanza lo que dure cada búsqueda."""

    def __init__(
        self, salidas: dict[str, Any], *, dura_s: float = 0.0, reloj: list[float] | None = None
    ) -> None:
        self.salidas = salidas
        self.orden: list[str] = []
        self.cuerpos: list[dict[str, Any]] = []
        self._cerrojo = threading.Lock()
        self.dura_s = dura_s
        self.reloj = reloj

    def search(self, *, body: dict[str, Any], **_kw: Any) -> dict:
        ident = _identificador(body)
        with self._cerrojo:
            self.orden.append(ident)
            self.cuerpos.append(body)
            if self.reloj is not None:
                self.reloj[0] += self.dura_s
        salida = self.salidas.get(ident, _respuesta(0))
        if isinstance(salida, Exception):
            raise salida
        return salida


def _curp(i: int) -> str:
    return f"PEGJ85031{i}HDFRRN0{i}"


def _lote(n: int, **kw: Any) -> SolicitudLote:
    return SolicitudLote(
        consultas=[ConsultaLote(id=f"c{i}", texto=_curp(i), tipo="curp") for i in range(n)], **kw
    )


class TestBuscarLote:
    def test_devuelve_en_el_orden_de_las_consultas_con_su_id_y_cursor(self) -> None:
        salidas = {_curp(0): _respuesta(2), _curp(1): _respuesta(0), _curp(2): _respuesta(1)}
        cliente = _Cliente(salidas)
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), _lote(3))
        assert [x.id for x in r.resultados] == ["c0", "c1", "c2"]
        assert [x.total for x in r.resultados] == [2, 0, 1]
        assert r.resultados[0].cursor == [1.0, "a1"]
        assert r.resultados[1].cursor is None
        assert r.parcial is False
        assert r.resultados[0].documentos[0]["_resaltado"] == ["…"]

    def test_una_consulta_que_vence_no_se_lleva_a_las_demas(self) -> None:
        """Lo que pasó con `_msearch` (29-09): un teléfono lento perdió la tanda entera."""
        cliente = _Cliente({_curp(1): ConnectionTimeout("TIMEOUT", "lento", Exception())})
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), _lote(3))
        assert [x.parcial for x in r.resultados] == [False, True, False]
        assert r.resultados[1].error == "plazo"

    def test_el_error_de_una_consulta_no_tumba_las_demas(self) -> None:
        cliente = _Cliente({_curp(1): TransportError(400, "too_many_clauses", {})})
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), _lote(2))
        assert not r.resultados[0].parcial
        assert r.resultados[1].parcial and r.resultados[1].error == "too_many_clauses"
        assert r.parcial is True

    def test_lo_barato_se_ejecuta_primero_y_sale_en_su_orden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(busqueda, "_CONCURRENCIA_LOTE", 1)
        lote = SolicitudLote(consultas=[
            ConsultaLote(id="t", texto="5512345678", tipo="telefono"),
            ConsultaLote(id="c", texto=_curp(0), tipo="curp"),
        ])
        cliente = _Cliente({})
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), lote)
        assert cliente.orden == [_curp(0), "5512345678"]
        assert [x.id for x in r.resultados] == ["t", "c"]

    def test_sin_presupuesto_no_se_lanza_y_se_dice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(busqueda, "_CONCURRENCIA_LOTE", 1)
        reloj = [0.0]
        monkeypatch.setattr(busqueda.time, "monotonic", lambda: reloj[0])
        cliente = _Cliente({}, dura_s=15.0, reloj=reloj)  # cada búsqueda "tarda" 15 s
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), _lote(3, presupuesto_ms=20000))
        assert len(cliente.orden) == 2  # 0 s y 15 s caben; a los 30 s ya no
        assert [x.ejecutada for x in r.resultados] == [True, True, False]
        assert r.resultados[2].parcial is True

    def test_el_plazo_de_cada_consulta_no_pasa_del_presupuesto(self) -> None:
        cliente = _Cliente({})
        busqueda.buscar_lote(cliente, Config(_env_file=None), _lote(1, presupuesto_ms=20000))
        assert int(cliente.cuerpos[0]["timeout"].removesuffix("ms")) <= 16000

    def test_un_texto_que_no_deja_nada_no_se_lanza_y_no_es_parcial(self) -> None:
        cliente = _Cliente({})
        lote = SolicitudLote(consultas=[ConsultaLote(id="v", texto="--", tipo="curp")])
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), lote)
        assert r.resultados[0].total == 0 and not r.resultados[0].parcial
        assert cliente.orden == []

    def test_los_campos_se_podan_como_en_buscar(self) -> None:
        cliente = _Cliente({_curp(0): _respuesta(1)})
        r = busqueda.buscar_lote(cliente, Config(_env_file=None), _lote(1, campos=["nombre"]))
        assert set(r.resultados[0].documentos[0]) == {"nombre", "_resaltado"}
        assert cliente.cuerpos[0]["_source"] == ["nombre"]


class TestSalud:
    def test_por_omision_no_esta_ocupado(self) -> None:
        s = Salud(ok=True, indice=True)
        assert s.ocupado is False and s.ocupado_desde is None
