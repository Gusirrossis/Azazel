"""Un XLSX es contenido buscable, no solo nombres de hoja.

Este archivo existe por el peor agujero del barrido de cobertura: el extractor de
hojas de cálculo devolvía las dimensiones de cada hoja y NINGUNA celda, así que un
padrón en Excel —CURPs, nombres, teléfonos en las celdas— se indexaba con
`texto_indexable` vacío: 0 % de cobertura, indistinguible de un archivo ilegible.

El test que sostiene el arreglo es `test_la_curp_de_una_celda_llega_al_texto`: sin él,
un refactor puede volver a dejar el XLSX mudo y no se notaría hasta que alguien busque
a una persona que sí estaba en una hoja y no aparezca.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from normalizacion.core.config import PerillasWorker
from normalizacion.ingesta.workers.extractores import ContextoExtraccion
from normalizacion.ingesta.workers.extractores.hoja import extraer_xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CURP = "GOMC800101HDFXXX01"


def _ctx(datos: bytes, *, max_chars: int = 100_000) -> ContextoExtraccion:
    return ContextoExtraccion(
        fuente=io.BytesIO(datos),
        nombre="x.xlsx",
        tipo_real=XLSX,
        tamano=len(datos),
        perillas=PerillasWorker(extractor_max_chars=max_chars),
    )


def _libro(hojas: dict[str, list[list[object]]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)  # quita la hoja vacía por defecto
    for titulo, filas in hojas.items():
        ws = wb.create_sheet(titulo)
        for fila in filas:
            ws.append(fila)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestXlsxProduceTexto:
    def test_la_curp_de_una_celda_llega_al_texto(self) -> None:
        """El fallo original, en una línea: el XLSX se indexaba sin una sola celda."""
        datos = _libro({"Padron": [["id", "curp", "nombre"], [1, CURP, "Persona Uno"]]})
        r = extraer_xlsx(_ctx(datos))
        assert r.texto, "un XLSX con filas no puede devolver texto vacío"
        assert CURP in r.texto
        assert "Persona Uno" in r.texto

    def test_la_cabecera_va_en_el_texto(self) -> None:
        datos = _libro({"Padron": [["id", "curp", "nombre"], [1, CURP, "P"]]})
        r = extraer_xlsx(_ctx(datos))
        assert "curp" in r.texto

    def test_sigue_dando_metadatos_de_hojas(self) -> None:
        """Añadir texto no puede quitar el metadato que ya existía."""
        datos = _libro({"Padron": [["curp"], [CURP]], "Notas": [["x"], ["y"]]})
        r = extraer_xlsx(_ctx(datos))
        assert r.campos["hojas_total"] == 2
        assert "Padron" in r.campos["hojas"]


class TestTodasLasHojas:
    def test_una_curp_en_la_segunda_hoja_tambien_se_indexa(self) -> None:
        """No se lee solo la primera hoja: el dato de la hoja 2 también es buscable."""
        datos = _libro(
            {
                "Resumen": [["titulo"], ["cuadro"]],
                "Detalle": [["id", "curp"], [1, CURP]],
            }
        )
        r = extraer_xlsx(_ctx(datos))
        assert r.texto and CURP in r.texto


class TestPresupuesto:
    def test_las_columnas_de_identidad_van_primero(self) -> None:
        """El orden importa dentro de cada línea: la CURP se escribe antes que las notas,
        de modo que si el presupuesto obliga a truncar, se pierde el relleno y no la
        identidad."""
        cab = ["notas", "curp", "relleno"]
        fila = ["nota-larga-de-relleno", CURP, "otro-relleno"]
        datos = _libro({"Padron": [cab, fila]})
        r = extraer_xlsx(_ctx(datos))
        linea_datos = r.texto.splitlines()[1]
        assert linea_datos.index(CURP) < linea_datos.index("nota-larga-de-relleno")

    def test_respeta_el_tope_y_marca_truncado(self) -> None:
        filas = [["curp", "nombre"]] + [[f"C{i:05d}", "n" * 50] for i in range(500)]
        datos = _libro({"Padron": filas})
        r = extraer_xlsx(_ctx(datos, max_chars=1000))
        assert len(r.texto) <= 1000
        assert "texto_truncado" in r.flags


class TestBordes:
    def test_una_hoja_vacia_no_rompe(self) -> None:
        datos = _libro({"Vacia": [], "Con": [["curp"], [CURP]]})
        r = extraer_xlsx(_ctx(datos))
        assert r.texto and CURP in r.texto
