"""Plugin de texto plano y XML: el texto ES el contenido buscable (con límite K11).

Reclama además **`text/*` como comodín**. El censo del índice mostraba miles de
documentos que son texto puro —`text/x-c`, `text/x-asm`, `text/xml`, `text/javascript`,
`text/troff`, `text/x-php`, `text/x-script.python`, `text/x-tex`…— indexados con CERO
contenido, no porque fueran ilegibles sino porque **nadie los reclamaba**: sin extractor
para su mime exacto, `extraer` devolvía `sin_extractor_l1` y el archivo quedaba mudo.

El comodín no pisa a nadie: `extractor_para` busca primero el mime EXACTO y solo cae al
prefijo si no hay coincidencia, así que `text/csv` sigue yendo al plugin tabular, que
sabe perfilarlo. Y un tipo de texto que aparezca mañana entra solo.

`application/sql` y `message/rfc822` van explícitos porque no empiezan por `text/` pero
son texto a todos los efectos: un dump SQL trae los INSERT con los datos dentro, y un
correo trae cabeceras y cuerpo.
"""

from __future__ import annotations

from . import ContextoExtraccion, ResultadoExtraccion, registrar


@registrar(
    "text/plain",
    "text/*",
    "application/xml",
    "application/sql",
    "message/rfc822",
)
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
