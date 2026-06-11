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

from . import ContextoExtraccion, ResultadoExtraccion, registrar

_MAX_COLUMNAS_DETALLE = 100


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
        return ResultadoExtraccion(campos=campos, flags=flags)

    if ctx.tipo_real == "application/x-ndjson":
        df = pl.read_ndjson(io.BytesIO(datos))
    else:  # text/csv
        df = pl.read_csv(io.BytesIO(datos), ignore_errors=True, truncate_ragged_lines=True)

    perfil = _perfil_calidad(df)
    campos = {
        "filas": df.height,
        "columnas": df.width,
        "columnas_nombres": df.columns[:50],
    }
    return ResultadoExtraccion(campos=campos, perfil_calidad=perfil, flags=flags)
