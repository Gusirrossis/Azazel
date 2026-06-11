"""Plugins de documentos: PDF (pypdf) y DOCX (python-docx). Python puro — sin JVM."""

from __future__ import annotations

from typing import Any

from . import ContextoExtraccion, ResultadoExtraccion, registrar


@registrar("application/pdf")
def extraer_pdf(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    from pypdf import PdfReader

    lector = PdfReader(ctx.fuente)
    campos: dict[str, Any] = {"paginas": len(lector.pages)}
    meta = lector.metadata
    if meta is not None:
        if meta.title:
            campos["titulo"] = str(meta.title)[:300]
        if meta.author:
            campos["autor"] = str(meta.author)[:300]

    maximo = ctx.perillas.extractor_max_chars
    fragmentos: list[str] = []
    acumulado = 0
    flags: list[str] = []
    for pagina in lector.pages:
        fragmento = pagina.extract_text() or ""
        fragmentos.append(fragmento)
        acumulado += len(fragmento)
        if acumulado >= maximo:
            flags.append("texto_truncado")  # patrón fscrawler: indexed_chars (⚙K11)
            break
    texto = "\n".join(fragmentos)[:maximo].strip()
    return ResultadoExtraccion(campos=campos, texto=texto or None, flags=flags)


@registrar("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
def extraer_docx(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    from docx import Document

    documento = Document(ctx.fuente)
    parrafos = [p.text for p in documento.paragraphs if p.text.strip()]
    campos: dict[str, Any] = {"parrafos": len(parrafos)}
    propiedades = documento.core_properties
    if propiedades.title:
        campos["titulo"] = propiedades.title[:300]
    if propiedades.author:
        campos["autor"] = propiedades.author[:300]

    maximo = ctx.perillas.extractor_max_chars
    texto = "\n".join(parrafos)
    flags = ["texto_truncado"] if len(texto) > maximo else []
    return ResultadoExtraccion(campos=campos, texto=texto[:maximo] or None, flags=flags)
