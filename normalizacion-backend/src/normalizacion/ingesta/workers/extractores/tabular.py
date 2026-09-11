"""Plugin tabular (CSV/NDJSON/JSON) + perfil de calidad (⚙K12).

El perfil reimplementa LIGERO el catálogo conceptual de great_expectations sobre
polars (~500 líneas era la estimación del diseño; esto es el núcleo): filas,
columnas, tipo inferido, % nulos y únicos por columna → `quality_score` buscable.
GX como dependencia quedó descartado (pesado, sin soporte polars).
"""

from __future__ import annotations

import io
import json
from typing import Any

import polars as pl

from normalizacion.core import identidad_columnas

from . import ContextoExtraccion, ResultadoExtraccion, registrar

_MAX_COLUMNAS_DETALLE = 100


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
    partes = [" | ".join(columnas)]
    largo = len(partes[0])
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
    for col in df.columns[:_MAX_COLUMNAS_DETALLE]:
        serie = df[col]
        nulos_pct = round(100.0 * serie.null_count() / filas, 1)
        suma_nulos_pct += nulos_pct
        detalle[col] = {
            "tipo": str(serie.dtype),
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


@registrar("text/csv", "application/x-ndjson", "application/json")
def extraer_tabular(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    datos = ctx.fuente.read(ctx.perillas.calidad_max_bytes)
    flags: list[str] = []
    if ctx.tamano > len(datos):
        flags.append("perfil_truncado")  # se perfila la muestra, no el archivo entero

    if ctx.tipo_real == "application/json":
        # JSON único: sin perfil tabular; las claves raíz se vuelven buscables
        obj = json.loads(datos)
        campos: dict[str, Any] = {"json_tipo": type(obj).__name__}
        if isinstance(obj, dict):
            campos["claves_raiz"] = sorted(obj.keys())[:50]
        elif isinstance(obj, list):
            campos["elementos"] = len(obj)
        # El JSON también se vuelca: un padrón en JSON tenía el mismo problema que uno
        # en CSV — claves indexadas y valores invisibles.
        texto = json.dumps(obj, ensure_ascii=False, separators=(" ", ": "))
        if len(texto) > ctx.perillas.extractor_max_chars:
            texto = texto[: ctx.perillas.extractor_max_chars]
            flags.append("texto_truncado")
        return ResultadoExtraccion(campos=campos, texto=texto, flags=flags)

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
        df = pl.read_csv(
            io.BytesIO(datos),
            ignore_errors=True,
            truncate_ragged_lines=True,
            infer_schema_length=None,  # misma disciplina: esquema sobre TODAS las filas
        )

    perfil = _perfil_calidad(df)
    campos = {
        "filas": df.height,
        "columnas": df.width,
        "columnas_nombres": df.columns[:50],
        "tiene_columnas_identidad": identidad_columnas.tiene_identidad(list(df.columns)),
    }
    texto, truncado = _texto_de_filas(df, ctx.perillas.extractor_max_chars)
    if truncado:
        # Se marca en el doc: que no aparezca un nombre aquí no prueba que no esté en
        # el archivo. Para el 100 % de un tabular grande hace falta trocearlo en lotes.
        flags.append("texto_truncado")
    return ResultadoExtraccion(campos=campos, texto=texto, perfil_calidad=perfil, flags=flags)
