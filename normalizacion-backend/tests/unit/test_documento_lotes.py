"""PDF/DOCX grande troceado en rangos de páginas/párrafos, cada trozo su propio doc, en
vez de truncar el texto a `extractor_max_chars`. Espejo de `test_tabla_plana.py`."""

from __future__ import annotations

import io

from normalizacion.core.config import PerillasFiltro
from normalizacion.ingesta.precalificacion.documento_lotes import (
    explorar,
    planificar_docx,
    planificar_pdf,
    servir_lote,
)

GRANDE = 1 << 30
CURP = "GOMC800101HDFXXX01"


def _pdf(n_paginas: int) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(n_paginas):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _docx(parrafos: list[str]) -> bytes:
    from docx import Document

    d = Document()
    for p in parrafos:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class TestPdf:
    def test_pdf_grande_se_trocea_en_paginas(self) -> None:
        datos = _pdf(5)
        lotes = planificar_pdf(io.BytesIO(datos))  # paginas_por_lote=1 por defecto
        assert [x.ruta_interna for x in lotes] == ["pdf/0-1", "pdf/1-2", "pdf/2-3", "pdf/3-4", "pdf/4-5"]

    def test_pdf_chico_no_se_explota(self) -> None:
        assert planificar_pdf(io.BytesIO(_pdf(1))) == []  # 1 página ≤ tope → doc único

    def test_servir_devuelve_un_pdf_de_esas_paginas(self) -> None:
        from pypdf import PdfReader

        datos = _pdf(5)
        spool = servir_lote(io.BytesIO(datos), "pdf/2-3", umbral_memoria=1 << 20, limite_bytes=GRANDE)
        assert len(PdfReader(spool).pages) == 1  # el mini-PDF trae SOLO esa página

    def test_cobertura_paginas(self) -> None:
        lotes = planificar_pdf(io.BytesIO(_pdf(7)), paginas_por_lote=2)
        cubiertas = sum(x.hasta - x.desde for x in lotes)
        assert cubiertas == 7  # todas las páginas, sin solape

    def test_pdf_corrupto_es_motivo_no_excepcion(self) -> None:
        entradas, motivo, _ = explorar(PerillasFiltro(), io.BytesIO(b"no soy un pdf"), "pdf", 0)
        assert entradas == [] and motivo == "contenedor_corrupto"


class TestDocx:
    def test_docx_grande_se_trocea_en_parrafos(self) -> None:
        datos = _docx([f"parrafo {i}" for i in range(500)])
        lotes = planificar_docx(io.BytesIO(datos), parrafos_por_lote=100)
        assert len(lotes) == 5
        assert lotes[0].ruta_interna == "docx/0-100"

    def test_docx_chico_no_se_explota(self) -> None:
        datos = _docx([f"p{i}" for i in range(50)])
        assert planificar_docx(io.BytesIO(datos), parrafos_por_lote=100) == []

    def test_un_parrafo_tardio_es_buscable(self) -> None:
        """El bug: el texto se trunca y un párrafo lejano se pierde. Troceado, cae en un
        trozo que sí se sirve."""
        parrafos = [f"relleno {i}" for i in range(400)]
        parrafos[350] = f"aqui va la {CURP} escondida"
        datos = _docx(parrafos)
        lotes = planificar_docx(io.BytesIO(datos), parrafos_por_lote=100)
        recuperado = b""
        for lote in lotes:
            recuperado += servir_lote(
                io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
            ).read()
        assert CURP.encode() in recuperado


class TestExplorar:
    def test_pdf_entradas(self) -> None:
        entradas, motivo, topado = explorar(PerillasFiltro(), io.BytesIO(_pdf(4)), "pdf", 99)
        assert motivo is None and not topado
        assert [e[0] for e in entradas] == ["pdf/0-1", "pdf/1-2", "pdf/2-3", "pdf/3-4"]


class TestNoDobleOcr:
    """Un PDF explotado en páginas NO debe re-OCR-earse en el padre: cada página OCR-ea
    como hijo. Sin esto se paga el OCR dos veces y se duplica el texto en el índice."""

    def test_pdf_explotado_no_re_ocr_ea_el_padre(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from normalizacion.core.config import PerillasWorker
        from normalizacion.ingesta.workers.extractores import ContextoExtraccion, documentos

        llamadas = {"n": 0}

        def _fake_ocr(_ctx):  # type: ignore[no-untyped-def]
            llamadas["n"] += 1
            return "texto ocr fingido", ["ocr_ok"], 90.0

        monkeypatch.setattr(documentos, "_ocr_pdf", _fake_ocr)
        datos = _pdf(3)  # páginas en blanco → texto nativo vacío → dispararía OCR

        def _ctx(explotado: bool):  # type: ignore[no-untyped-def]
            return ContextoExtraccion(
                fuente=io.BytesIO(datos), nombre="x.pdf", tipo_real="application/pdf",
                tamano=len(datos), perillas=PerillasWorker(), ocr_activo=True,
                es_contenedor_explotado=explotado,
            )

        r = documentos.extraer_pdf(_ctx(True))
        assert llamadas["n"] == 0  # el padre explotado NO re-OCR-ea
        assert "pdf_explotado" in r.flags
        documentos.extraer_pdf(_ctx(False))
        assert llamadas["n"] == 1  # sin el flag, SÍ OCR-earía
