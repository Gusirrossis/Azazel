"""El contenido de un tabular tiene que llegar al índice.

Este archivo existe por un fallo medido en producción: **36.657 documentos CSV
indexados y CERO con `texto_indexable`**. El plugin sacaba columnas y estadísticas de
calidad, pero descartaba las filas — y como `anclas.buscar_en_texto` mira justamente
`extraccion.texto`, ningún padrón en CSV produjo jamás una CURP.

El test que sostiene el arreglo es `test_la_curp_de_una_fila_llega_al_texto`: sin él,
un refactor puede volver a dejar el texto vacío y no se notaría hasta que alguien
busque una persona que sí estaba en el corpus y no aparezca.
"""

from __future__ import annotations

import io
import json

from normalizacion.core.config import PerillasWorker
from normalizacion.ingesta.workers.extractores import ContextoExtraccion
from normalizacion.ingesta.workers.extractores.tabular import extraer_tabular

CURP = "GOMC800101HDFXXX01"


def _ctx(datos: bytes, tipo: str, *, max_chars: int = 100_000) -> ContextoExtraccion:
    return ContextoExtraccion(
        fuente=io.BytesIO(datos),
        nombre="x",
        tipo_real=tipo,
        tamano=len(datos),
        perillas=PerillasWorker(extractor_max_chars=max_chars),
    )


def _csv(filas: int = 3, *, columnas_relleno: int = 0) -> bytes:
    extra = "".join(f",notas{i}" for i in range(columnas_relleno))
    cab = f"id,curp,nombre{extra}\n"
    cuerpo = ""
    for i in range(filas):
        relleno = "".join(f",{'x' * 60}" for _ in range(columnas_relleno))
        cuerpo += f"{i},GOMC80010{i}HDFXXX0{i},Persona {i}{relleno}\n"
    return (cab + cuerpo).encode()


class TestCsvProduceTexto:
    def test_la_curp_de_una_fila_llega_al_texto(self) -> None:
        """El fallo original, en una línea: sin texto no hay anclas, y sin anclas no
        hay personas. 36.657 CSV estaban así."""
        r = extraer_tabular(_ctx(_csv(), "text/csv"))
        assert r.texto, "un CSV con filas no puede devolver texto vacío"
        assert CURP in r.texto

    def test_tambien_llega_el_nombre(self) -> None:
        r = extraer_tabular(_ctx(_csv(), "text/csv"))
        assert "Persona 1" in r.texto

    def test_la_cabecera_va_en_el_texto(self) -> None:
        """Los nombres de columna son parte de lo que se busca ('curp', 'nombre')."""
        r = extraer_tabular(_ctx(_csv(), "text/csv"))
        assert "curp" in r.texto.splitlines()[0]

    def test_sigue_dando_columnas_y_perfil(self) -> None:
        """Añadir texto no puede quitar lo que ya funcionaba."""
        r = extraer_tabular(_ctx(_csv(), "text/csv"))
        assert r.campos["filas"] == 3
        assert "curp" in r.campos["columnas_nombres"]
        assert r.perfil_calidad is not None
        assert r.campos["tiene_columnas_identidad"] is True


class TestPresupuesto:
    def test_respeta_el_tope_de_caracteres(self) -> None:
        r = extraer_tabular(_ctx(_csv(filas=500), "text/csv", max_chars=2000))
        assert len(r.texto) <= 2000
        assert "texto_truncado" in r.flags

    def test_con_poco_espacio_gana_la_identidad(self) -> None:
        """La razón de ordenar columnas: con 10 columnas de relleno y un presupuesto
        corto, la CURP tiene que entrar igual. Si se volcaran en el orden original, el
        relleno se comería el presupuesto y el documento sería inútil."""
        r = extraer_tabular(_ctx(_csv(filas=40, columnas_relleno=10), "text/csv", max_chars=1500))
        assert "GOMC" in r.texto, "la CURP debe entrar antes que las columnas de relleno"

    def test_un_csv_vacio_no_rompe(self) -> None:
        r = extraer_tabular(_ctx(b"a,b\n", "text/csv"))
        assert r.texto == "" or "a" in r.texto


class TestJson:
    def test_el_json_tambien_vuelca_sus_valores(self) -> None:
        """Un padrón en JSON tenía el mismo problema: claves indexadas, valores no."""
        datos = json.dumps({"curp": CURP, "nombre": "Persona"}).encode()
        r = extraer_tabular(_ctx(datos, "application/json"))
        assert r.texto and CURP in r.texto

    def test_ndjson_vuelca_filas(self) -> None:
        lineas = b'{"curp":"' + CURP.encode() + b'","nombre":"P"}\n{"curp":"X","nombre":"Q"}\n'
        r = extraer_tabular(_ctx(lineas, "application/x-ndjson"))
        assert r.texto and CURP in r.texto
