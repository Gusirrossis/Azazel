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
from collections.abc import Iterator
from typing import Any

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


class TestComodinDeTexto:
    """`text/*` reclama los tipos de texto que nadie tenía asignados.

    El censo del índice mostraba miles de documentos que son texto puro indexados con
    cero contenido: no eran ilegibles, es que sin extractor para su mime exacto
    `extraer` devolvía `sin_extractor_l1` y el archivo quedaba mudo.
    """

    def test_un_tipo_de_texto_sin_plugin_propio_ahora_tiene_extractor(self) -> None:
        from normalizacion.ingesta.workers.extractores import extractor_para
        from normalizacion.ingesta.workers.extractores.texto import extraer_texto

        for tipo in ("text/x-c", "text/x-asm", "text/javascript", "text/x-php", "text/xml"):
            assert extractor_para(tipo) is extraer_texto, tipo

    def test_sql_y_correo_tambien(self) -> None:
        """No empiezan por `text/` pero son texto: un dump SQL trae los INSERT con los
        datos dentro."""
        from normalizacion.ingesta.workers.extractores import extractor_para
        from normalizacion.ingesta.workers.extractores.texto import extraer_texto

        assert extractor_para("application/sql") is extraer_texto
        assert extractor_para("message/rfc822") is extraer_texto

    def test_el_comodin_NO_le_quita_el_csv_al_tabular(self) -> None:
        """`extractor_para` busca el mime exacto antes que el prefijo. Si esto se
        rompiera, los CSV perderían su perfil de calidad y sus columnas."""
        from normalizacion.ingesta.workers.extractores import extractor_para

        assert extractor_para("text/csv") is extraer_tabular

    def test_un_dump_sql_produce_texto_con_sus_datos(self) -> None:
        from normalizacion.ingesta.workers.extractores.texto import extraer_texto

        dump = f"INSERT INTO padron VALUES (1,'{CURP}','Persona');\n".encode()
        r = extraer_texto(_ctx(dump, "application/sql"))
        assert r.texto and CURP in r.texto


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

    def test_ndjson_columna_nula_al_principio_no_pierde_el_valor_tardio(self) -> None:
        """El bug medido en producción: una columna nula en las primeras filas del lote
        se tipa `Null` (polars infiere el esquema con ~100 filas), y un valor NO nulo
        posterior reventaba `read_ndjson` con ComputeError. El lote ENTERO se indexaba
        SIN texto (flag `extraccion_fallida`) y entraba en HECHO — pérdida silenciosa:
        52.255 lotes (~26 M filas de las bases grandes de Lilith). Con
        `infer_schema_length=None` el valor tardío llega al texto."""
        filas = [{"curp": f"X{i:05d}", "obs": None} for i in range(120)]
        filas.append({"curp": "tardia", "obs": CURP})  # la CURP aparece tras 120 nulos
        lineas = ("\n".join(json.dumps(f) for f in filas)).encode()
        r = extraer_tabular(_ctx(lineas, "application/x-ndjson"))
        assert r.texto, "un lote no puede quedar sin texto por una columna nula al principio"
        assert CURP in r.texto, "el valor que llega tarde tiene que entrar igual"

    def test_ndjson_columna_de_tipo_mezclado_no_revienta(self) -> None:
        """SQLite es de tipado dinámico: una columna puede traer int y string. Con el
        escaneo completo, polars coacciona a String en vez de reventar el lote."""
        filas = [{"curp": CURP, "n": 5}, {"curp": "X", "n": "no-numerico"}]
        lineas = ("\n".join(json.dumps(f) for f in filas)).encode()
        r = extraer_tabular(_ctx(lineas, "application/x-ndjson"))
        assert r.texto and CURP in r.texto


class TestTodasLasColumnas:
    """No se recortan las columnas volcadas al texto: un dato en la columna 55 (un
    correo en «observaciones») también tiene que ser buscable. El tope viejo `[:40]`
    dejaba mudas las columnas 41+."""

    def test_una_columna_tardia_tambien_llega_al_texto(self) -> None:
        cols = [f"col{i:02d}" for i in range(60)]
        cab = ",".join(cols) + "\n"
        valores = [f"v{i:02d}" for i in range(60)]
        valores[55] = "hallame@ejemplo.mx"  # ninguna señal de identidad: queda al final
        fila = ",".join(valores) + "\n"
        r = extraer_tabular(_ctx((cab + fila).encode(), "text/csv"))
        assert r.texto and "hallame@ejemplo.mx" in r.texto


class TestEncoding:
    """El texto no puede perder los acentos por adivinar mal el encoding. Un padrón en
    Latin-1/CP1252 (lo normal en México) decodificado como UTF-8 con `errors="replace"`
    convertía «MUÑOZ» en «MU�OZ»: la CURP (ASCII) sobrevivía y el nombre se perdía
    en silencio."""

    def test_latin1_conserva_los_acentos(self) -> None:
        from normalizacion.ingesta.workers.extractores.texto import extraer_texto

        datos = "JOSÉ MUÑOZ PEÑA".encode("latin-1")
        r = extraer_texto(_ctx(datos, "text/plain"))
        assert r.texto is not None
        assert "MUÑOZ" in r.texto
        assert "�" not in r.texto

    def test_utf8_sigue_funcionando(self) -> None:
        from normalizacion.ingesta.workers.extractores.texto import extraer_texto

        r = extraer_texto(_ctx("JOSÉ MUÑOZ".encode(), "text/plain"))
        assert r.texto and "MUÑOZ" in r.texto

    def test_csv_cp1252_no_tira_el_lote(self) -> None:
        """polars exige UTF-8: un CSV en cp1252 reventaba con `invalid utf-8 sequence` y
        el lote ENTERO se indexaba sin texto (medido en 'Matrix.rar')."""
        datos = f"nombre,curp\nJOSÉ MUÑOZ PEÑA,{CURP}\n".encode("cp1252")
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert r.texto and "MUÑOZ" in r.texto and CURP in r.texto
        assert "recodificado_cp1252" in r.flags

    def test_csv_cortado_a_mitad_de_caracter(self) -> None:
        """La muestra se corta en `calidad_max_bytes` y puede partir una «Ñ» (2 bytes):
        el byte huérfano del final no puede tirar el lote ni forzar una recodificación."""
        datos = f"nombre,curp\nMUÑOZ,{CURP}\nPEÑA".encode()[:-2]  # parte la «Ñ»
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert r.texto and "MUÑOZ" in r.texto
        assert "recodificado_cp1252" not in r.flags


#: Tope de Lucene para un término keyword. `perfil_calidad` es `flat_object` en los índices
#: vivos y `campos_extraidos` lo es en la plantilla (lo será tras el próximo rollover): cada
#: hoja se indexa como `raiz.ruta=valor` en `<campo>._valueAndPath`, y un solo término más
#: largo tumba el documento ENTERO.
_LIMITE_TERMINO = 32_766


def _terminos(raiz: str, valor: Any, ruta: str = "") -> Iterator[str]:
    """Los términos `raiz.ruta=valor` de `_valueAndPath`, el campo que rechazó el doc de
    Matrix. Cada uno contiene la clave y el valor de su hoja: si él cabe, caben los dos."""
    if isinstance(valor, dict):
        for clave, hijo in valor.items():
            yield from _terminos(raiz, hijo, f"{ruta}.{clave}" if ruta else str(clave))
    elif isinstance(valor, list):
        for hijo in valor:
            yield from _terminos(raiz, hijo, ruta)
    else:
        yield f"{raiz}.{ruta}={valor}"


def _termino_mas_largo(r: Any) -> int:
    terminos = [
        *_terminos("perfil_calidad", r.perfil_calidad or {}),
        *_terminos("campos_extraidos", r.campos),
    ]
    return max((len(t.encode("utf-8")) for t in terminos), default=0)


class TestSinTerminosGigantes:
    """Un doc con un término de más de 32.766 bytes UTF-8 lo rechaza OpenSearch ENTERO.

    Medido en el reproceso de 'Matrix.rar': el lote `inovawp.sql!texto/65536-131072`, que
    T2 tipó text/csv, cayó dentro de una línea larga; polars tomó esa línea por cabecera,
    su «nombre de columna» fue a `perfil_calidad.columnas_detalle` y el doc quedó en ERROR
    con «immense term in field=perfil_calidad._valueAndPath». Y un lote que cae entero
    dentro de la línea no tiene filas: aunque OpenSearch lo aceptara, `_texto_de_filas`
    devolvía "" y su contenido no era buscable."""

    LINEA_70KB = ("a" * 35_000 + " " + CURP + " " + "b" * 35_000).encode()

    def test_cabecera_de_70kb_sin_comas_no_produce_terminos_gigantes(self) -> None:
        datos = self.LINEA_70KB + b"\nfila uno\nfila dos\n"
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert _termino_mas_largo(r) <= _LIMITE_TERMINO

    def test_lote_dentro_de_una_linea_larga_conserva_su_texto(self) -> None:
        """El lote entero es un trozo de UNA línea, sin salto: lo que sirve `texto_lotes`
        en mitad de una línea más larga que su ventana."""
        r = extraer_tabular(_ctx(self.LINEA_70KB, "text/csv"))
        assert _termino_mas_largo(r) <= _LIMITE_TERMINO
        assert r.texto and CURP in r.texto, "el contenido del lote tiene que ser buscable"
        assert "tabular_como_texto:cabecera_sin_esquema" in r.flags

    def test_filas_tras_una_cabecera_falsa_no_se_recortan(self) -> None:
        """Con una «cabecera» de una sola columna, `truncate_ragged_lines` recortaba cada
        fila siguiente a su primer campo: la CURP de la segunda columna se perdía. La
        cabecera falsa CORTA está en `TestFilasDesiguales`."""
        otra_curp = "PEPJ900202MDFXXX02"
        datos = self.LINEA_70KB + f"\n1,{otra_curp},Persona Dos\n".encode()
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert r.texto and otra_curp in r.texto and "Persona Dos" in r.texto

    def test_el_texto_como_texto_respeta_el_tope(self) -> None:
        r = extraer_tabular(_ctx(self.LINEA_70KB, "text/csv", max_chars=10_000))
        assert r.texto is not None and len(r.texto) == 10_000
        assert "texto_truncado" in r.flags

    def test_el_texto_multibyte_llena_el_tope_sin_caracteres_rotos(self) -> None:
        """Solo se decodifican 4 bytes por carácter del tope. Recortar de menos (a `tope`
        bytes) dejaría la mitad del texto en una línea de «ñ» (2 bytes cada una); y el
        corte, que aquí cae a mitad de carácter, no puede dejar un «�»."""
        datos = ("a" + "ñ" * 50_000).encode()
        r = extraer_tabular(_ctx(datos, "text/csv", max_chars=1_000))
        assert r.texto == "a" + "ñ" * 999
        assert "texto_truncado" in r.flags

    def test_un_lote_en_blanco_no_deja_texto_vacio(self) -> None:
        """Un "" cuenta como presente para un `exists` de OpenSearch: el doc se escondía de
        la cuenta de «sin texto». Como en `texto.py`, un lote en blanco da None."""
        for datos in (b"", b"\n", b" \r\n"):
            r = extraer_tabular(_ctx(datos, "text/csv"))
            assert r.texto is None, datos
            assert any(f.startswith("tabular_como_texto:") for f in r.flags), datos

    def test_una_cabecera_mas_larga_que_el_presupuesto_no_lo_rebasa(self) -> None:
        """Miles de columnas cortas: la cabecera sola ya pasa del tope de chars, y antes se
        devolvía entera."""
        cab = ",".join(f"c{i:04d}" for i in range(3_000))
        fila = ",".join("1" for _ in range(3_000))
        r = extraer_tabular(_ctx(f"{cab}\n{fila}\n".encode(), "text/csv", max_chars=2_000))
        assert r.texto is not None and len(r.texto) <= 2_000
        assert "texto_truncado" in r.flags

    def test_lote_de_una_linea_con_comas_conserva_su_texto(self) -> None:
        """Mismo lote dentro de una línea larga, pero partida por comas en columnas cortas:
        el término no crece, pero sin filas el texto salía vacío."""
        linea = ",".join(f"'v{i}'" for i in range(2_000)) + f",'{CURP}'"
        r = extraer_tabular(_ctx(linea.encode(), "text/csv"))
        assert r.texto and CURP in r.texto
        assert "tabular_como_texto:sin_filas" in r.flags

    def test_comilla_sin_cerrar_no_tira_el_lote(self) -> None:
        """`ignore_errors` no cubre las comillas: polars revienta con ComputeError y el lote
        se indexaba SIN texto (`extraccion_fallida:ComputeError`, 859 docs en Matrix)."""
        datos = f'id,valor\n1,"abc\n2,{CURP}\n'.encode()
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert r.texto and CURP in r.texto
        assert "tabular_como_texto:ComputeError" in r.flags

    def test_struct_ancho_de_ndjson_no_produce_un_tipo_gigante(self) -> None:
        """El `tipo` de una columna anidada enumera todos sus campos: 3.000 claves → 66 KB."""
        fila = {"curp": CURP, "meta": {f"campo_{i:05d}": i for i in range(3_000)}}
        r = extraer_tabular(_ctx((json.dumps(fila) + "\n").encode(), "application/x-ndjson"))
        assert _termino_mas_largo(r) <= _LIMITE_TERMINO
        assert r.perfil_calidad is not None
        assert r.perfil_calidad["columnas_detalle"]["meta"]["tipo"].endswith("…")
        assert r.texto and CURP in r.texto

    def test_claves_largas_de_ndjson_se_acotan_sin_pisarse(self) -> None:
        """Las claves son un esquema real: se perfilan, con el nombre acotado y marcado. Dos
        claves con el mismo principio no pueden quedar en la misma entrada del perfil."""
        larga_a, larga_b = "k" * 40_000 + "A", "k" * 40_000 + "B"
        filas = [{"curp": CURP, larga_a: 1, larga_b: 2}, {"curp": "X", larga_a: 3, larga_b: 4}]
        datos = ("\n".join(json.dumps(f) for f in filas)).encode()
        r = extraer_tabular(_ctx(datos, "application/x-ndjson"))
        assert _termino_mas_largo(r) <= _LIMITE_TERMINO
        assert r.perfil_calidad is not None
        detalle = r.perfil_calidad["columnas_detalle"]
        assert len(detalle) == 3, "cada columna conserva su entrada en el perfil"
        assert all(len(nombre) <= 300 for nombre in detalle)
        assert sum(nombre.startswith("kkk") for nombre in detalle) == 2
        assert len(r.campos["columnas_nombres"]) == 3

    def test_clave_raiz_gigante_de_json_no_produce_terminos_gigantes(self) -> None:
        datos = json.dumps({"k" * 40_000: CURP, "curp": CURP}).encode()
        r = extraer_tabular(_ctx(datos, "application/json"))
        assert _termino_mas_largo(r) <= _LIMITE_TERMINO
        assert r.texto and CURP in r.texto

    def test_csv_normal_da_el_mismo_perfil_que_antes(self) -> None:
        """Salvaguarda, no regresión: pasa también sin el arreglo. Lo que prueba es que el
        acotado no toca a un CSV normal (valores copiados de la salida previa al arreglo)."""
        r = extraer_tabular(_ctx(_csv(), "text/csv"))
        assert r.perfil_calidad == {
            "filas": 3,
            "columnas": 3,
            "quality_score": 100,
            "columnas_detalle": {
                "id": {"tipo": "Int64", "nulos_pct": 0.0, "unicos": 3},
                "curp": {"tipo": "String", "nulos_pct": 0.0, "unicos": 3},
                "nombre": {"tipo": "String", "nulos_pct": 0.0, "unicos": 3},
            },
        }
        assert r.campos == {
            "filas": 3,
            "columnas": 3,
            "columnas_nombres": ["id", "curp", "nombre"],
            "tiene_columnas_identidad": True,
        }
        assert not any(f.startswith("tabular_como_texto") for f in r.flags)


class TestFilasDesiguales:
    """Una fila con más campos que la cabecera: `truncate_ragged_lines` la recortaba EN
    SILENCIO y los campos de más no llegaban al texto, con perfil y sin bandera.

    Es lo normal en las ventanas SQL que T2 tipa text/csv (255.614 lotes): la «cabecera» es
    la línea con la que empieza la ventana, y polars solo entiende la comilla doble, así
    que cada coma dentro de un literal `'PÉREZ, JUAN'` es un campo más. Ninguna de estas
    cabeceras falsas pasa de 256 caracteres: `_cabecera_sin_esquema` no las ve."""

    CSV_CON_UN_CAMPO_DE_MAS = (
        b"id,curp,nombre\n"
        b"0,GOMC800100HDFXXX00,Persona 0\n"
        b"1,GOMC800101HDFXXX01,Persona 1,Calle Falsa 123\n"
        b"2,GOMC800102HDFXXX02,Persona 2\n"
    )

    def test_ventana_sql_con_cabecera_falsa_corta_conserva_la_curp(self) -> None:
        """La ventana empieza con la cola de un INSERT: dos «columnas», y cada INSERT de
        después trae la CURP en el 5.º campo."""
        insert = "".join(
            f"INSERT INTO padron VALUES ({i},'n{i}','a','b','{CURP}');\n" for i in range(50)
        )
        r = extraer_tabular(_ctx(("'x', 'y');\n" + insert).encode(), "text/csv"))
        assert r.texto and CURP in r.texto
        assert "tabular_como_texto:filas_desiguales" in r.flags

    def test_csv_con_barras_y_una_coma_conserva_la_curp(self) -> None:
        """Separador `|`: para polars la cabecera es UNA columna, y la coma del nombre parte
        la fila en dos; todo lo que seguía a la coma se perdía."""
        datos = f"id|nombre|curp\n1|PÉREZ, JUAN|{CURP}\n".encode()
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert r.texto and CURP in r.texto and "PÉREZ, JUAN" in r.texto

    def test_el_campo_de_mas_de_un_csv_real_llega_al_texto(self) -> None:
        r = extraer_tabular(_ctx(self.CSV_CON_UN_CAMPO_DE_MAS, "text/csv"))
        assert r.texto and "Calle Falsa 123" in r.texto
        assert all(f"GOMC80010{i}HDFXXX0{i}" in r.texto for i in range(3))
        assert "tabular_como_texto:filas_desiguales" in r.flags

    def test_filas_desiguales_conservan_el_perfil_de_siempre(self) -> None:
        """Salvaguarda, no regresión: pasa también sin el arreglo. Solo cambia de dónde sale
        el texto; el perfil y los `campos` son los de antes (copiados de su salida)."""
        r = extraer_tabular(_ctx(self.CSV_CON_UN_CAMPO_DE_MAS, "text/csv"))
        assert r.perfil_calidad == {
            "filas": 3,
            "columnas": 3,
            "quality_score": 100,
            "columnas_detalle": {
                "id": {"tipo": "Int64", "nulos_pct": 0.0, "unicos": 3},
                "curp": {"tipo": "String", "nulos_pct": 0.0, "unicos": 3},
                "nombre": {"tipo": "String", "nulos_pct": 0.0, "unicos": 3},
            },
        }
        assert r.campos == {
            "filas": 3,
            "columnas": 3,
            "columnas_nombres": ["id", "curp", "nombre"],
            "tiene_columnas_identidad": True,
        }

    def test_una_fila_con_campos_de_menos_no_cambia_nada(self) -> None:
        """Salvaguarda: polars completa con nulos la fila corta y no se pierde nada, así que
        sigue el volcado por filas de siempre, sin bandera."""
        datos = f"id,curp,nombre\n0,{CURP},Persona 0\n1,GOMC800101HDFXXX01\n".encode()
        r = extraer_tabular(_ctx(datos, "text/csv"))
        assert r.texto == f"curp | nombre | id\n{CURP} | Persona 0 | 0\nGOMC800101HDFXXX01 |  | 1"
        assert not any(f.startswith("tabular_como_texto") for f in r.flags)
