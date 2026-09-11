"""Plugin de hojas de cálculo: XLSX (openpyxl, read_only — no carga el libro entero).

Antes SOLO sacaba nombres de hoja y dimensiones: un padrón en Excel se indexaba con
CERO contenido buscable —el peor agujero del corpus, un XLSX con CURPs/nombres/teléfonos
en las celdas y `texto_indexable` vacío—. Ahora vuelca las CELDAS a texto (TODAS las
hojas, TODAS las columnas) priorizando las de identidad, igual que el tabular y con el
mismo presupuesto de caracteres. `read_only` + `iter_rows` transmiten fila a fila: el
libro nunca se carga entero en RAM.
"""

from __future__ import annotations

from typing import Any

from normalizacion.core import identidad_columnas

from . import ContextoExtraccion, ResultadoExtraccion, registrar


def _valor(v: Any) -> str:
    return "" if v is None else str(v)


@registrar("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
def extraer_xlsx(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    from openpyxl import load_workbook  # type: ignore[import-untyped]

    presupuesto = ctx.perillas.extractor_max_chars
    libro = load_workbook(ctx.fuente, read_only=True, data_only=True)
    try:
        hojas: dict[str, Any] = {}
        partes: list[str] = []
        largo = 0
        truncado = False
        for ws in libro.worksheets:  # TODAS las hojas (antes se topaba a 50)
            # Metadato barato desde la dimensión declarada; no exige recorrer las filas.
            hojas[ws.title] = {"filas": int(ws.max_row or 0), "columnas": int(ws.max_column or 0)}
            if truncado:
                continue  # sin presupuesto: seguimos catalogando hojas, sin volcar celdas
            filas = ws.iter_rows(values_only=True)
            try:
                cabecera = next(filas)
            except StopIteration:
                continue  # hoja vacía
            nombres = [str(c) if c is not None else f"col{i}" for i, c in enumerate(cabecera)]
            # Orden por POSICIÓN (una hoja puede traer cabeceras repetidas, a diferencia
            # de un df de polars): identidad primero, el resto en su orden original.
            orden = sorted(
                range(len(nombres)), key=lambda i: (-identidad_columnas.puntuar(nombres[i]), i)
            )
            enc = f"# {ws.title}: " + " | ".join(nombres[i] for i in orden)
            if largo + len(enc) > presupuesto:
                truncado = True
                continue
            partes.append(enc)
            largo += len(enc) + 1
            for fila in filas:
                linea = " | ".join(_valor(fila[i]) if i < len(fila) else "" for i in orden)
                if largo + len(linea) > presupuesto:
                    truncado = True
                    break
                partes.append(linea)
                largo += len(linea) + 1
        campos = {
            "hojas": sorted(hojas.keys()),
            "hojas_total": len(libro.worksheets),
            "hojas_detalle": hojas,
        }
        texto = "\n".join(partes).strip() or None
        flags = ["texto_truncado"] if truncado else []
        return ResultadoExtraccion(campos=campos, texto=texto, flags=flags)
    finally:
        libro.close()
