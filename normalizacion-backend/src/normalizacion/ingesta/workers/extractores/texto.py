"""Plugin de texto plano y XML: el texto ES el contenido buscable (con límite K11)."""

from __future__ import annotations

from . import ContextoExtraccion, ResultadoExtraccion, registrar


@registrar("text/plain", "application/xml")
def extraer_texto(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    maximo = ctx.perillas.extractor_max_chars
    datos = ctx.fuente.read(maximo * 4)  # UTF-8: hasta 4 bytes por carácter
    texto = datos.decode("utf-8-sig", errors="replace")  # -sig: el BOM no ensucia el texto
    flags = ["texto_truncado"] if ctx.tamano > len(datos) or len(texto) > maximo else []
    texto = texto[:maximo]
    return ResultadoExtraccion(
        campos={"lineas": texto.count("\n") + 1},
        texto=texto.strip() or None,
        flags=flags,
    )
