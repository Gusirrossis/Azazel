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

import codecs

from . import ContextoExtraccion, ResultadoExtraccion, registrar


def _decodificar_tolerante(datos: bytes) -> str:
    """Decodifica bytes a texto SIN perder acentos por una mala corazonada de encoding.

    Un padrón en Latin-1/CP1252 (lo normal en México) decodificado como UTF-8 con
    `errors="replace"` convierte cada byte acentuado en U+FFFD: «JOSÉ MUÑOZ PEÑA» queda
    ilegible y el nombre deja de coincidir —la CURP (ASCII) sobrevive, el nombre y el
    domicilio se pierden en silencio—. Se prueba UTF-8 primero, TOLERANDO un carácter
    multibyte cortado al final del bloque leído (`final=False`: no exige cerrarlo, así un
    corte a mitad de char no tira todo el texto a un fallback equivocado), y se cae a
    CP1252 y por último a Latin-1, que mapea los 256 bytes y nunca falla.
    """
    try:
        return codecs.getincrementaldecoder("utf-8-sig")().decode(datos, final=False)
    except UnicodeDecodeError:
        pass
    for enc in ("cp1252", "latin-1"):
        try:
            return datos.decode(enc)
        except UnicodeDecodeError:
            continue
    return datos.decode("latin-1", errors="replace")


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
    texto = _decodificar_tolerante(datos)
    flags = ["texto_truncado"] if ctx.tamano > len(datos) or len(texto) > maximo else []
    texto = texto[:maximo]
    return ResultadoExtraccion(
        campos={"lineas": texto.count("\n") + 1},
        texto=texto.strip() or None,
        flags=flags,
    )
