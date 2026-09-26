"""`norm reextraer` no puede volver a convertir una ventana `texto/…` en tabla.

Re-extrae con el tipo guardado en la caché, sin pasar por el worker. Tras v7 quedaban
66.247 extracciones tabulares de antes (ventanas de .sql de 'Matrix.rar' leídas como CSV):
re-extraerlas con su tipo metía otra vez los valores de la primera fila como nombres de
columna en el índice, pisando el doc que v7 ya había extraído como texto. Todos los bytes
de estos tests son inventados.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from normalizacion import reextraccion
from normalizacion.core.config import Config

_VENTANA = (
    b"INSERT INTO t VALUES (1,'ana','perez','x1'),(2,'luis','diaz','x2');\n"
    b"INSERT INTO t VALUES (3,'eva','ruiz','x3'),(4,'raul','sanz','x4');\n"
) * 40
_CSV = b"id,nombre,monto\n" + b"1,ana,10.5\n2,luis,22.0\n3,eva,9.99\n" * 30


def _doc(archivo_id: str, *, ventana: bool, tipo: str = "text/csv") -> dict[str, Any]:
    base = {"contenedor_archivo_id": "p", "ruta_interna": "i", "profundidad": 1, "hoja": True}
    origen = (
        {**base, "cadena": ["x.sql", "texto/0-65536"]}
        if ventana
        else {**base, "cadena": ["x.zip", "tabla.csv"]}
    )
    return {
        "archivo_id": archivo_id, "disco_id": "d", "nombre": "n", "ruta": "r",
        "extension": ".csv", "tamano": 1, "mtime": datetime(2026, 1, 1, tzinfo=UTC),
        "tipo_real": tipo,
        "puntaje": 70, "senales": {}, "motivo": None, "version_filtro": "v",
        "origen_contenedor": origen,
    }


class _Almacen:
    def __init__(self, datos: bytes) -> None:
        self.datos = datos

    @contextmanager
    def leer(self, _hash: str) -> Any:
        yield io.BytesIO(self.datos)


class _Sink:
    def __init__(self) -> None:
        self.docs: list[Any] = []

    def entregar(self, doc: Any) -> None:
        self.docs.append(doc)


def _reextraer(
    monkeypatch: pytest.MonkeyPatch, datos: bytes, documentos: list[dict[str, Any]]
) -> tuple[_Sink, dict[str, Any]]:
    guardado: dict[str, Any] = {}
    monkeypatch.setattr(reextraccion, "_docs_del_contenido", lambda _c, _h: documentos)
    monkeypatch.setattr(
        reextraccion.cache_extraccion, "guardar", lambda _c, _h, **kw: guardado.update(kw)
    )
    sink = _Sink()
    reextraccion._reextraer_uno(
        Config(_env_file=None),
        None,  # type: ignore[arg-type]
        _Almacen(datos),
        sink,
        "h" * 64,
        "text/csv",  # el tipo que guardó la caché de antes de v7
        None,
        reextraccion.ResumenReextraccion(),
    )
    return sink, guardado


class TestVentanaSeReextraeComoTexto:
    def test_ventana_con_tipo_tabular_guardado_sale_como_texto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink, guardado = _reextraer(monkeypatch, _VENTANA, [_doc("a", ventana=True)])
        (doc,) = sink.docs
        assert doc.perfil_calidad is None
        assert "columnas_nombres" not in doc.campos_extraidos
        assert doc.texto_indexable and "INSERT INTO t VALUES" in doc.texto_indexable
        # La caché queda con el tipo con el que se extrajo: la próxima pasada ya no es tabla.
        assert guardado["tipo_real"] == "text/plain"

    def test_basta_una_ventana_entre_las_copias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sink, guardado = _reextraer(
            monkeypatch, _VENTANA, [_doc("a", ventana=False), _doc("b", ventana=True)]
        )
        assert len(sink.docs) == 2
        assert all(d.perfil_calidad is None for d in sink.docs)
        assert guardado["tipo_real"] == "text/plain"

    def test_el_tipo_del_doc_no_se_reescribe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Solo cambia el extractor; el tipo de la fila lo corrige el filtro (v7)."""
        sink, _ = _reextraer(monkeypatch, _VENTANA, [_doc("a", ventana=True)])
        assert sink.docs[0].tipo_real == "text/csv"


class TestUnCsvDeVerdadSigueSiendoTabla:
    def test_csv_que_no_es_ventana_conserva_el_extractor_tabular(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink, guardado = _reextraer(monkeypatch, _CSV, [_doc("a", ventana=False)])
        (doc,) = sink.docs
        assert doc.perfil_calidad is not None
        assert guardado["tipo_real"] == "text/csv"


class TestTipoParaReextraer:
    def test_ventana_tabular(self) -> None:
        assert reextraccion._tipo_para_reextraer("text/csv", [_doc("a", ventana=True)]) == (
            "text/plain"
        )

    @pytest.mark.parametrize("tipo", ["application/x-ndjson", "application/json"])
    def test_ventana_json(self, tipo: str) -> None:
        docs = [_doc("a", ventana=True, tipo=tipo)]
        assert reextraccion._tipo_para_reextraer(tipo, docs) == "text/plain"

    def test_ventana_no_tabular_conserva_su_tipo(self) -> None:
        docs = [_doc("a", ventana=True, tipo="application/sql")]
        assert reextraccion._tipo_para_reextraer("application/sql", docs) == "application/sql"

    def test_sin_origen(self) -> None:
        doc = _doc("a", ventana=False)
        doc["origen_contenedor"] = None
        assert reextraccion._tipo_para_reextraer("text/csv", [doc]) == "text/csv"
