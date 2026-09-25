"""Plugin tabular (CSV/NDJSON/JSON) + perfil de calidad (⚙K12).

El perfil reimplementa LIGERO el catálogo conceptual de great_expectations sobre
polars (~500 líneas era la estimación del diseño; esto es el núcleo): filas,
columnas, tipo inferido, % nulos y únicos por columna → `quality_score` buscable.
GX como dependencia quedó descartado (pesado, sin soporte polars).
"""

from __future__ import annotations

import codecs
import io
import json
from typing import Any

import polars as pl

from normalizacion.core import identidad_columnas

from . import ContextoExtraccion, ResultadoExtraccion, registrar

_MAX_COLUMNAS_DETALLE = 100

#: Tope de caracteres del texto que el perfil y los `campos` guardan de una columna (su
#: nombre, su tipo). `perfil_calidad` es `flat_object` en los 6 índices vivos: indexa cada
#: hoja como un término keyword `ruta=valor`, y Lucene rechaza el documento ENTERO si un
#: término pasa de 32.766 bytes UTF-8. Medido en el reproceso de 'Matrix.rar': el lote
#: `inovawp.sql!texto/65536-131072` (T2 lo tipó text/csv) cayó dentro de una línea larga, su
#: «cabecera» fue esa línea y el doc quedó en ERROR con «immense term in
#: field=perfil_calidad._valueAndPath». `campos_extraidos` aún es `object` en esos índices y
#: a un nombre gigante hoy lo salva el `ignore_above: 1024` de su plantilla dinámica; pero la
#: plantilla `archivos` ya lo declara `flat_object`, así que tras el próximo rollover lo
#: tumbaría igual. 256 caracteres son como mucho 1 KB en UTF-8: holgado para un nombre real
#: y a 30 veces del límite.
_MAX_CHARS_NOMBRE = 256
_MARCA_TRUNCADO = "…"


def _acotar(valor: str) -> str:
    if len(valor) <= _MAX_CHARS_NOMBRE:
        return valor
    return valor[: _MAX_CHARS_NOMBRE - len(_MARCA_TRUNCADO)] + _MARCA_TRUNCADO


def _nombres_acotados(nombres: list[str]) -> list[str]:
    """Acotados y sin repetir: dos nombres largos con el mismo principio se acotan al mismo
    texto y, como el perfil es un dict por nombre, el segundo pisaría al primero."""
    vistos: set[str] = set()
    salida: list[str] = []
    for nombre in nombres:
        base = candidato = _acotar(nombre)
        n = 1
        while candidato in vistos:
            n += 1
            candidato = f"{base}#{n}"
        vistos.add(candidato)
        salida.append(candidato)
    return salida


def _cabecera_sin_esquema(columnas: list[str]) -> bool:
    """¿La primera línea NO es una cabecera? Un nombre que no cabe en `_MAX_CHARS_NOMBRE` no
    lo escribió nadie como nombre de columna: es una línea de datos. Pasa con los lotes
    `texto/…` de un texto de líneas largas que T2 tipa text/csv, como el de 'inovawp.sql'.
    Su perfil sería basura, y va al índice como texto.

    Es un indicio estrecho a propósito: una cabecera falsa CORTA (`'x', 'y');`, la cola de un
    INSERT) no se distingue así de una real. Lo que impide que esas ventanas pierdan campos
    es `_leer_csv`, no esto. Coste asumido: un CSV real con un nombre de más de 256
    caracteres (la pregunta larga de un formulario) se queda sin perfil, pero entra ENTERO
    como texto."""
    return any(len(c) > _MAX_CHARS_NOMBRE for c in columnas)


def _leer_csv(datos: bytes) -> tuple[pl.DataFrame, bool]:
    """Devuelve (df, filas_desiguales). Lanza `PolarsError` si ni recortando se puede leer.

    `truncate_ragged_lines` recorta EN SILENCIO cada fila con más campos que la cabecera, y
    los que sobran no llegaban al texto. Es lo normal en las ventanas SQL que T2 tipa text/csv
    (255.614 lotes, ver `reglas.precalificar_contenido`): la «cabecera» es la línea con la
    que empieza la ventana, y polars solo entiende la comilla doble, así que cada coma dentro
    de un literal `'PÉREZ, JUAN'` es un campo más. Reproducido: una ventana que empezaba con
    `'x', 'y');` perdía la CURP del 5.º campo de sus 50 INSERT, con perfil y sin bandera; y
    un CSV con `|` de separador y una coma en el nombre perdía todo lo que la seguía. Medido
    en 40 lotes text/csv de Matrix tomados al azar: 12 con filas desiguales, que dejaban
    fuera del texto 2.025 de sus 19.047 palabras distintas (ningún correo ni CURP en esa
    muestra).

    Por eso se lee primero SIN recortar, y solo si polars rechaza el lote se repite como
    siempre: el `df` —y con él el perfil y los `campos`— es idéntico al de antes, y quien
    llama sabe que el texto tiene que salir de los bytes y no de las filas. Una fila con
    campos de MENOS no cuenta: polars la completa con nulos y no se pierde nada. Un CSV sin
    filas desiguales se lee una sola vez."""

    def leer(recortar: bool) -> pl.DataFrame:
        return pl.read_csv(
            io.BytesIO(datos),
            ignore_errors=True,
            truncate_ragged_lines=recortar,
            infer_schema_length=None,  # misma disciplina: esquema sobre TODAS las filas
        )

    try:
        return leer(recortar=False), False
    except pl.exceptions.PolarsError:
        # Las dos lecturas solo difieren en el recorte: si esta funciona, lo que rechazó la
        # primera fueron filas con campos de más («found more fields than defined»).
        return leer(recortar=True), True


def _texto_crudo(datos: bytes, maximo: int) -> tuple[str, bool]:
    """Los bytes como texto, topado a `maximo` chars. Devuelve (texto, truncado).

    Solo se decodifica lo que puede caber: `datos` llega hasta `calidad_max_bytes` (10 MiB)
    y el texto se queda en 100 k chars. Como en `texto.py`, cuenta 4 bytes por carácter como
    mucho; el decodificador incremental descarta el carácter que parte el corte en vez de
    dejar un «�». `datos` ya es UTF-8 válido: `_a_utf8` lo normalizó."""
    limite = maximo * 4
    decodificador = codecs.getincrementaldecoder("utf-8")(errors="replace")
    texto = decodificador.decode(datos[:limite], final=False)
    truncado = len(datos) > limite or len(texto) > maximo
    return texto[:maximo], truncado


def _como_texto(datos: bytes, maximo: int, flags: list[str], motivo: str) -> ResultadoExtraccion:
    """Lo que no es una tabla se indexa como el texto que es: los bytes tal cual, sin
    pasar por polars (que partiría, recortaría o descartaría) y sin perfil. El flag
    `tabular_como_texto:<motivo>` lo deja auditable: un text/csv sin perfil no es un fallo
    mudo.

    Un lote en blanco (`b"\\n"`, que polars rechaza con NoDataError) da `texto=None`, como en
    `texto.py`. Un "" cuenta como presente para un `exists` de OpenSearch: así se escondían
    322 docs de Matrix con `perfil_calidad.filas=0` de la cuenta de docs sin texto."""
    texto, truncado = _texto_crudo(datos, maximo)
    flags.append(f"tabular_como_texto:{motivo}")
    if truncado:
        flags.append("texto_truncado")
    return ResultadoExtraccion(
        campos={"lineas": texto.count("\n") + 1},
        texto=texto if texto.strip() else None,
        flags=flags,
    )


def _texto_de_filas(df: pl.DataFrame, presupuesto: int) -> tuple[str, bool]:
    """Vuelca las filas a texto plano. Devuelve (texto, truncado).

    Esto es lo que hace buscable un padrón. Antes el plugin devolvía columnas y
    estadísticas pero NINGÚN texto, así que `texto_indexable` quedaba vacío y
    `anclas.buscar_en_texto` no tenía dónde mirar: 36.657 CSV indexados sin una sola
    CURP detectada, aunque las tuvieran en cada fila.

    TODAS las columnas se vuelcan, ordenadas por identidad (⚙ `core/identidad_columnas`):
    un dato en la columna 41 (un correo en `observaciones`) también tiene que ser
    buscable. El orden por identidad solo decide el reparto del presupuesto de chars —si
    se acaba, que se acabe habiendo escrito la CURP y el nombre, no las observaciones—,
    no qué columnas entran. Incluirlas todas no añade RAM: el `df` ya está en memoria.
    """
    if df.height == 0 or df.width == 0:
        return "", False
    columnas = identidad_columnas.ordenar_por_identidad(list(df.columns))
    cabecera = " | ".join(columnas)
    if len(cabecera) > presupuesto:
        # Miles de columnas cortas (una línea de SQL partida por sus comas) no caben ni en
        # la cabecera: antes se devolvía entera, por encima del tope.
        return cabecera[:presupuesto], True
    partes = [cabecera]
    largo = len(cabecera)
    truncado = False
    for fila in df.select(columnas).iter_rows():
        linea = " | ".join("" if v is None else str(v) for v in fila)
        if largo + len(linea) > presupuesto:
            truncado = True
            break
        partes.append(linea)
        largo += len(linea) + 1
    return "\n".join(partes), truncado


def _perfil_calidad(df: pl.DataFrame) -> dict[str, Any]:
    filas = max(df.height, 1)
    detalle: dict[str, Any] = {}
    suma_nulos_pct = 0.0
    columnas_perfiladas = df.columns[:_MAX_COLUMNAS_DETALLE]
    for col, nombre in zip(
        columnas_perfiladas, _nombres_acotados(columnas_perfiladas), strict=True
    ):
        serie = df[col]
        nulos_pct = round(100.0 * serie.null_count() / filas, 1)
        suma_nulos_pct += nulos_pct
        detalle[nombre] = {
            # El tipo también se acota: el `Struct` de una columna NDJSON anidada enumera
            # todos sus campos, y uno de 3.000 claves da un tipo de 66 KB.
            "tipo": _acotar(str(serie.dtype)),
            "nulos_pct": nulos_pct,
            "unicos": serie.n_unique(),
        }
    columnas = max(len(detalle), 1)
    completitud = 1.0 - (suma_nulos_pct / columnas) / 100.0
    return {
        "filas": df.height,
        "columnas": df.width,
        "quality_score": round(100 * completitud),
        "columnas_detalle": detalle,
    }


def _a_utf8(datos: bytes) -> tuple[bytes, str | None]:
    """Normaliza los bytes a UTF-8 válido antes de dárselos a polars (que exige UTF-8).

    Dos causas reales de `invalid utf-8 sequence`, las dos tiraban el lote ENTERO
    (indexado sin texto, `extraccion_fallida`):
    - La muestra se corta en `calidad_max_bytes` y puede partir un carácter multibyte
      al final → se recortan esos 1-3 bytes huérfanos.
    - Dumps en cp1252/latin-1 (lo normal en bases mexicanas: medido en 'Matrix.rar').
      Se recodifican desde cp1252 —no con `utf8-lossy`, que cambiaría cada «é» por
      «�» y rompería la búsqueda por nombre—. cp1252 es superconjunto práctico de
      latin-1 en el rango imprimible; los pocos bytes sin definir se reemplazan."""
    try:
        datos.decode("utf-8")
        return datos, None
    except UnicodeDecodeError as exc:
        if exc.start >= len(datos) - 3:
            try:
                datos[: exc.start].decode("utf-8")
                return datos[: exc.start], None
            except UnicodeDecodeError:
                pass
    return datos.decode("cp1252", errors="replace").encode("utf-8"), "recodificado_cp1252"


@registrar("text/csv", "application/x-ndjson", "application/json")
def extraer_tabular(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    datos = ctx.fuente.read(ctx.perillas.calidad_max_bytes)
    flags: list[str] = []
    if ctx.tamano > len(datos):
        flags.append("perfil_truncado")  # se perfila la muestra, no el archivo entero
    datos, recodificado = _a_utf8(datos)
    if recodificado:
        flags.append(recodificado)

    if ctx.tipo_real == "application/json":
        # JSON único: sin perfil tabular; las claves raíz se vuelven buscables
        obj = json.loads(datos)
        campos: dict[str, Any] = {"json_tipo": type(obj).__name__}
        if isinstance(obj, dict):
            campos["claves_raiz"] = [_acotar(k) for k in sorted(obj.keys())[:50]]
        elif isinstance(obj, list):
            campos["elementos"] = len(obj)
        # El JSON también se vuelca: un padrón en JSON tenía el mismo problema que uno
        # en CSV — claves indexadas y valores invisibles.
        texto = json.dumps(obj, ensure_ascii=False, separators=(" ", ": "))
        if len(texto) > ctx.perillas.extractor_max_chars:
            texto = texto[: ctx.perillas.extractor_max_chars]
            flags.append("texto_truncado")
        return ResultadoExtraccion(campos=campos, texto=texto, flags=flags)

    maximo = ctx.perillas.extractor_max_chars
    filas_desiguales = False
    if ctx.tipo_real == "application/x-ndjson":
        # `infer_schema_length=None` escanea TODAS las filas para inferir el esquema.
        # Sin esto (polars mira ~100 por defecto), una columna nula en las primeras
        # filas del lote se tipa `Null` y un valor NO nulo posterior revienta con
        # `ComputeError: got non-null value for NULL-typed column`. El lote ENTERO
        # fallaba y se indexaba SIN texto (flag `extraccion_fallida`), en HECHO: una
        # pérdida silenciosa. Medido en la luna de Lilith: 52.255 lotes (~26 M filas
        # de las bases grandes) invisibles así. Con el escaneo completo, una columna
        # mezclada (int+str, típico del tipado dinámico de SQLite) se coacciona a
        # String en vez de reventar. Coste acotado: `datos` ya viene topado a
        # `calidad_max_bytes`.
        df = pl.read_ndjson(io.BytesIO(datos), infer_schema_length=None)
    else:  # text/csv
        try:
            df, filas_desiguales = _leer_csv(datos)
        except pl.exceptions.PolarsError as exc:
            # `ignore_errors` no cubre las comillas: `1,"abc` sin cerrar o `"ab"c` revientan
            # igual con «could not parse … as dtype `str`» (ComputeError). Medido en el
            # índice de 'Matrix.rar': 859 docs text/csv SIN texto por
            # `extraccion_fallida:ComputeError` (596 de 598 muestreados, lotes `texto/…`),
            # y el log del reproceso da ese mismo error. Su contenido es texto legible.
            return _como_texto(datos, maximo, flags, type(exc).__name__)
        if _cabecera_sin_esquema(df.columns):
            return _como_texto(datos, maximo, flags, "cabecera_sin_esquema")
        if df.height == 0:
            # Todo quedó en la cabecera: un lote que cae entero dentro de una línea larga, sin
            # un solo salto, o finales de línea `\r` solos. `_texto_de_filas` devolvía "" y
            # el contenido no llegaba al índice aunque OpenSearch aceptara el doc: 322 docs
            # text/csv de Matrix con `perfil_calidad.filas=0` y `texto_indexable` vacío.
            return _como_texto(datos, maximo, flags, "sin_filas")

    perfil = _perfil_calidad(df)
    campos = {
        "filas": df.height,
        "columnas": df.width,
        "columnas_nombres": _nombres_acotados(df.columns[:50]),
        "tiene_columnas_identidad": identidad_columnas.tiene_identidad(list(df.columns)),
    }
    if filas_desiguales:
        # El perfil vale (son las columnas de la cabecera, como siempre), pero las filas
        # están recortadas: el texto sale de los bytes, que traen todos los campos.
        texto, truncado = _texto_crudo(datos, maximo)
        flags.append("tabular_como_texto:filas_desiguales")
    else:
        texto, truncado = _texto_de_filas(df, maximo)
    if truncado:
        # Se marca en el doc: que no aparezca un nombre aquí no prueba que no esté en
        # el archivo. Para el 100 % de un tabular grande hace falta trocearlo en lotes.
        flags.append("texto_truncado")
    return ResultadoExtraccion(campos=campos, texto=texto, perfil_calidad=perfil, flags=flags)
