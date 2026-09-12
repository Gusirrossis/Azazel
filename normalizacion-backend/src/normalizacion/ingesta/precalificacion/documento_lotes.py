"""Un PDF o DOCX grande tratado como CONTENEDOR: cada rango de PÁGINAS (PDF) o de
PÁRRAFOS (DOCX) es una entrada, y cada trozo se indexa como su propio doc.

El problema: `documentos.py` extrae el texto (nativo + OCR en PDF escaneado) y lo trunca a
`extractor_max_chars` (100k) — un contrato/boletín/PDF escaneado de 100 páginas pierde todo
lo posterior. Igual que un CSV grande antes de `tabla_plana`.

Se sirve DISTINTO según el formato, porque una página/párrafo no es texto hasta extraerlo:
 - **PDF**: se sirve un MINI-PDF con esas páginas (pypdf `PdfWriter`) → el leaf es
   `application/pdf` y lo re-extrae `documentos.py` con TODA su maquinaria (texto nativo +
   OCR + plazo) POR trozo. El plazo pasa a ser por-trozo: un PDF enorme ya no arriesga
   perderlo todo por un timeout.
 - **DOCX**: se sirve el TEXTO de esos párrafos → el leaf es `text/plain` y lo re-extrae
   `texto.py` sin tocarlo.

Un documento CHICO (≤ páginas/párrafos por lote) devuelve `[]`: no se explota y se indexa
como doc único (comportamiento de siempre). Las anclas se re-detectan por trozo.

Las constantes de corte son INMUTABLES: cambiarlas mueve las fronteras y por tanto el
`ruta_interna`/`archivo_id` (ver `tabla_plana`).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO

from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("documento_lotes")

#: Páginas por lote (PDF). 1 garantiza que el texto de un trozo cabe bajo
#: `extractor_max_chars` sin re-truncar; subirlo agrupa páginas (menos docs) a riesgo de
#: que una página densa haga que `documentos.py` re-trunque el lote.
PAGINAS_POR_LOTE = 1
#: Párrafos por lote (DOCX). ~200 párrafos rara vez pasan de 100k chars.
PARRAFOS_POR_LOTE = 200
MAX_LOTES_POR_ARCHIVO = 1_000_000


@dataclass(frozen=True)
class LoteDoc:
    formato: str  # "pdf" | "docx"
    desde: int  # índice inicial (inclusive): página (0-based) o párrafo
    hasta: int  # índice final (exclusive)

    @property
    def ruta_interna(self) -> str:
        return f"{self.formato}/{self.desde}-{self.hasta}"


def _parsear(ruta_interna: str) -> LoteDoc:
    formato, _, rango = ruta_interna.partition("/")
    desde, _, hasta = rango.partition("-")
    return LoteDoc(formato, int(desde), int(hasta))


def _abrir(fuente: str | Path | IO[bytes]) -> IO[bytes]:
    if isinstance(fuente, (str, Path)):
        return open(fuente, "rb")  # noqa: SIM115
    fuente.seek(0)
    return fuente


def _rangos(total: int, por_lote: int, max_lotes: int) -> list[tuple[int, int]]:
    if total <= por_lote:
        return []  # chico: no vale la pena explotar → doc único
    rangos = []
    a = 0
    while a < total and len(rangos) < max_lotes:
        rangos.append((a, min(a + por_lote, total)))
        a += por_lote
    return rangos


# ------------------------------------------------------------------ PDF


def planificar_pdf(
    fuente: str | Path | IO[bytes],
    *,
    paginas_por_lote: int = PAGINAS_POR_LOTE,
    max_lotes: int | None = None,
) -> list[LoteDoc]:
    """Rangos de páginas. Abre el árbol de páginas (no rasteriza ni extrae texto)."""
    from pypdf import PdfReader

    if max_lotes is None:
        max_lotes = MAX_LOTES_POR_ARCHIVO
    f = _abrir(fuente)
    cerrar = isinstance(fuente, (str, Path))
    try:
        n = len(PdfReader(f).pages)
    finally:
        if cerrar:
            f.close()
    return [LoteDoc("pdf", a, b) for a, b in _rangos(n, paginas_por_lote, max_lotes)]


def _servir_pdf(fuente: str | Path | IO[bytes], lote: LoteDoc, *, limite_bytes: int) -> IO[bytes]:
    """Un MINI-PDF con las páginas [desde, hasta). Sirve solo esas páginas: RAM ≈ recursos
    de esas páginas, no del documento entero."""
    from pypdf import PdfReader, PdfWriter

    f = _abrir(fuente)
    cerrar = isinstance(fuente, (str, Path))
    spool: IO[bytes] = SpooledTemporaryFile(max_size=limite_bytes)  # noqa: SIM115
    try:
        lector = PdfReader(f)
        escritor = PdfWriter()
        for pagina in lector.pages[lote.desde : lote.hasta]:
            escritor.add_page(pagina)
        escritor.write(spool)
    finally:
        if cerrar:
            f.close()
    spool.seek(0)
    return spool


# ------------------------------------------------------------------ DOCX


def _parrafos(f: IO[bytes]) -> list[str]:
    from docx import Document

    return [p.text for p in Document(f).paragraphs if p.text.strip()]


def planificar_docx(
    fuente: str | Path | IO[bytes],
    *,
    parrafos_por_lote: int = PARRAFOS_POR_LOTE,
    max_lotes: int | None = None,
) -> list[LoteDoc]:
    if max_lotes is None:
        max_lotes = MAX_LOTES_POR_ARCHIVO
    f = _abrir(fuente)
    cerrar = isinstance(fuente, (str, Path))
    try:
        n = len(_parrafos(f))
    finally:
        if cerrar:
            f.close()
    return [LoteDoc("docx", a, b) for a, b in _rangos(n, parrafos_por_lote, max_lotes)]


def _servir_docx(fuente: str | Path | IO[bytes], lote: LoteDoc, *, limite_bytes: int) -> IO[bytes]:
    """El TEXTO de los párrafos [desde, hasta) como text/plain → lo re-extrae `texto.py`."""
    f = _abrir(fuente)
    cerrar = isinstance(fuente, (str, Path))
    try:
        parrafos = _parrafos(f)[lote.desde : lote.hasta]
    finally:
        if cerrar:
            f.close()
    datos = ("\n".join(parrafos)).encode("utf-8")[:limite_bytes]
    spool: IO[bytes] = SpooledTemporaryFile(max_size=limite_bytes)  # noqa: SIM115
    spool.write(datos)
    spool.seek(0)
    return spool


# ------------------------------------------------------------------ API pública


def servir_lote(
    fuente: str | Path | IO[bytes], ruta_interna: str, *, umbral_memoria: int, limite_bytes: int
) -> IO[bytes]:
    lote = _parsear(ruta_interna)
    if lote.formato == "pdf":
        return _servir_pdf(fuente, lote, limite_bytes=limite_bytes)
    return _servir_docx(fuente, lote, limite_bytes=limite_bytes)


def explorar(
    perillas, fuente: str | Path | IO[bytes], formato: str, mtime_ns: int
) -> tuple[list[tuple[str, str, int, int]], str | None, bool]:
    """Entradas `(ruta_interna, nombre, tamano, mtime_ns)`, motivo si no se pudo, y si quedó
    PARCIAL. Un doc chico devuelve `[]` (no se explota → doc único, sin marca de contenedor).
    Un PDF/DOCX cifrado o corrupto → `contenedor_corrupto` (el precalificador lo preserva
    íntegro, como hoy)."""
    try:
        if formato == "pdf":
            lotes = planificar_pdf(fuente, max_lotes=perillas.t3_entradas_max)
        else:
            lotes = planificar_docx(fuente, max_lotes=perillas.t3_entradas_max)
    except Exception as exc:
        log.warning("documento_no_explorable", formato=formato, error=str(exc)[:150])
        return [], "contenedor_corrupto", False
    if not lotes:
        return [], None, False
    topado = len(lotes) >= perillas.t3_entradas_max
    # tamaño estimado por trozo: no se conoce sin servir; se usa un valor nominal para los
    # guards/priorización (una página/lote de párrafos ronda decenas de KB).
    entradas = [
        (lt.ruta_interna, f"{formato}-{lt.desde}.{'pdf' if formato == 'pdf' else 'txt'}", 50_000, mtime_ns)
        for lt in lotes
    ]
    return entradas, None, topado
