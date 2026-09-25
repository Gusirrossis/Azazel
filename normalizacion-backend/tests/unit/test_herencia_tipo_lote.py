"""Los lotes de ventana de texto (`texto/…`) heredan el tipo de su padre; UTF-16 legible.

Caso real (Matrix.rar, 2026-09): cada ventana de 64 KB de un .sql ya aceptado se volvía a
detectar desde cero y, si su tipo no estaba en la lista blanca, iba a frío aunque el padre
fuera `application/sql`. Así quedaron fuera 6.311 ventanas de SQL con HTML en sus columnas
(como `text/html`), 6.562 de un .sql UTF-16 (como binario: sus ventanas empiezan sin BOM) y
un lote con 'ustar' en el offset 257 se exploró como tar. Los BLOBs binarios de verdad
(3.644 ventanas de fotos JPEG dentro de INSERT) SÍ deben seguir en frío. Y lo que llegaba
vacío (106.140 ventanas y 41 entradas del RAR) iba a frío como `application/x-empty`, cuando
es un fallo nuestro que tiene que verse.
"""

from __future__ import annotations

import io
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from normalizacion.core import cola
from normalizacion.core.config import Config, PerillasFiltro, PerillasWorker
from normalizacion.core.modelo import Estado, RutaDecision
from normalizacion.ingesta.precalificacion import contenedores, precalificador, reglas
from normalizacion.ingesta.precalificacion.reglas import (
    precalificar_archivo,
    precalificar_contenido,
)
from normalizacion.ingesta.workers.extractores import extraer

PERILLAS = PerillasFiltro()
SQL = "application/sql"
VENTANA = 64 * 1024  # texto_lotes.BYTES_POR_LOTE


class _LibmagicFalsa:
    """libmagic con un veredicto fijo: la rama ④ de T1 no puede depender de la versión
    instalada (en Windows puede no estar y en la imagen es la 5.44)."""

    def __init__(self, tipo: str) -> None:
        self.tipo = tipo

    def from_buffer(self, _buf: bytes) -> str:
        return self.tipo


def _con_controles(texto: bytes, ratio: float) -> bytes:
    """`texto` con bytes de control C0 (ESC) repartidos hasta la proporción `ratio`."""
    buf = bytearray(texto)
    paso = round(1 / ratio)
    for i in range(paso // 2, len(buf), paso):
        buf[i] = 0x1B
    return bytes(buf)


LINEAS_SQL = [f"INSERT INTO personas VALUES ({i}, 'José Muñoz Peña', 'CDMX');" for i in range(4000)]


def _html(n: int = 60) -> bytes:
    """Una página guardada en una columna: la ventana cae dentro del valor del INSERT, así
    que no trae ninguna sentencia SQL, solo HTML."""
    return b"<html><head><title>Aviso</title></head><body>\n" + (
        b"<p>Contenido de la nota numero 123 con texto legible</p>\n" * n
    )


def _blob(semilla: int = 3, largo: int = 60_000) -> bytes:
    """Un INSERT con una foto en crudo, escapada como mysqldump (sin NUL, LF, CR, comillas
    ni \\Z): así eran las 3.644 ventanas binarias de Matrix."""
    crudo = bytearray(random.Random(semilla).randbytes(largo))
    for i, b in enumerate(crudo):
        if b in b"\x00\n\r\x1a'\"\\":
            crudo[i] = 0x41
    return b"INSERT INTO fotos VALUES (1,'" + bytes(crudo) + b"');\n"


def _utf16_modo_texto(lineas: list[str]) -> bytes:
    """UTF-16LE con los fines de línea de modo texto de Windows: `\\r\\0\\r\\n\\0`, un byte
    suelto por línea (859.021 de 859.021 en el .sql de Matrix)."""
    return b"".join(ln.encode("utf-16-le") + b"\r\x00\r\n\x00" for ln in lineas)


def _lote(head: bytes, tipo_padre: str | None = SQL, **perillas: Any) -> Any:
    return precalificar_contenido(
        PerillasFiltro(**perillas) if perillas else PERILLAS,
        head=head,
        abrible=io.BytesIO(head),
        nombre="parte-65536.txt",
        extension=".txt",
        ruta_relativa="respaldo.sql!texto/65536-131072",
        tamano=VENTANA,
        permitir_contenedor_hoja=False,
        tipo_padre=tipo_padre,
    )


class TestHerenciaEnReglas:
    @pytest.mark.parametrize("padre", [SQL, "text/plain", "text/xml"])
    @pytest.mark.parametrize("libmagic", ["text/html", None], ids=["libmagic", "sin_libmagic"])
    def test_ventana_html_hereda_el_tipo_del_padre_y_va_a_hot(
        self, padre: str, libmagic: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No solo de SQL: en Matrix hay 4.551 lotes con padre text/plain y 3 ventanas
        HTML con padre text/xml. Con libmagic, el `text/html` sale ya en T1; sin ella, en
        T2. Las dos vías tienen que heredar."""
        monkeypatch.setattr(
            reglas, "_LIBMAGIC", _LibmagicFalsa(libmagic) if libmagic is not None else None
        )
        r = _lote(_html(), tipo_padre=padre)
        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == padre
        assert r.senales["tipo_heredado"] is True
        assert r.senales["tipo_ventana"] == "text/html"
        assert r.motivo != "fuera_de_lista_blanca"

    def test_ventana_que_ya_entra_conserva_su_tipo(self) -> None:
        """La herencia solo cambia lo que hoy se rechaza: los 255.614 lotes SQL indexados
        como text/csv no se tocan."""
        csv = b"id,nombre,monto\n" + b"1,ana,10.5\n2,luis,22.0\n3,eva,9.99\n" * 50
        r_csv = _lote(csv)
        assert r_csv.tipo_real == "text/csv"
        assert "tipo_heredado" not in r_csv.senales

    def test_ventana_de_blob_binario_sigue_en_frio_como_lote_ilegible(self) -> None:
        r = _lote(_blob())
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "lote_ilegible"
        assert "tipo_heredado" not in r.senales
        assert r.senales["tipo_padre"] == SQL

        crudo = random.Random(11).randbytes(VENTANA)  # binario sin escapar, con NUL
        r_crudo = _lote(crudo)
        assert r_crudo.ruta is RutaDecision.COLD
        assert r_crudo.motivo == "lote_ilegible"

    def test_candado_de_control_c0_frena_binario_que_pasa_por_imprimible(self) -> None:
        """`ratio_imprimibles` deja pasar un 3 % de basura; los bytes de control C0 no. Las
        ventanas binarias de Matrix llegaban a 0,896 de imprimibles con el umbral en 0,9."""
        texto = bytearray(b"texto con bytes de control intercalados en cada linea\n" * 1200)
        for i in range(0, len(texto), 33):
            texto[i] = 0x01 + i % 7
        head = b"\x00" + bytes(texto)  # un NUL al inicio: T1 lo manda a revisar (nulos)
        r = _lote(head)
        assert r.senales["texto_legible"] is True  # por imprimibles, pasaría
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "lote_ilegible"

    @pytest.mark.parametrize(
        ("control", "ruta"),
        [(0.0186, RutaDecision.HOT), (0.0331, RutaDecision.COLD)],
        ids=["html_legitimo_1.86", "blob_minimo_3.31"],
    )
    def test_umbral_c0_entre_el_html_y_el_blob_medidos(
        self, control: float, ruta: RutaDecision
    ) -> None:
        """Medido en Matrix con el troceo nuevo: dos ventanas HTML legítimas con 1,86 % de
        C0 (y 0,974 de imprimibles) y los BLOB desde 3,31 %. Con el candado en 1 % las
        HTML se quedaban en frío solo por él."""
        head = _con_controles(_html(1200)[:VENTANA], control)
        r = _lote(head)
        assert r.senales["texto_legible"] is True
        assert r.senales["control_c0"] == pytest.approx(control, abs=0.001)
        assert r.ruta is ruta
        if ruta is RutaDecision.HOT:
            assert r.tipo_real == SQL and r.senales["tipo_heredado"] is True
        else:
            assert r.motivo == "lote_ilegible"

    def test_ventana_utf8_cortada_a_mitad_de_caracter_hereda(self) -> None:
        """10 ventanas HTML UTF-8 de Matrix se quedaban en frío: el head de 64 KB partía una
        comilla “ (E2 80 9C), el UTF-8 estricto fallaba y latin-1 hacía de sus 0x80-0x9F
        controles C1 (0,767-0,871 de imprimibles)."""
        linea = "<p>“Sí” — “no” — “quizá” … “nota”</p>\n".encode()
        cuerpo = b"<html><body>\n" + linea * 3000
        corte = cuerpo.index(b"\xe2", VENTANA - 100) + 2  # E2 80 y falta el tercer byte
        head = cuerpo[:corte]
        texto, encoding = reglas._decodificar(head)
        assert encoding == "latin-1"
        assert reglas._ratio_imprimibles(texto or "") < PERILLAS.ratio_imprimibles_min

        r = _lote(head)
        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == SQL
        assert r.senales["tipo_heredado"] is True
        assert r.senales["encoding"] == "utf-8"

    def test_ventana_vacia_es_lote_vacio_y_no_fuera_de_lista(self) -> None:
        r = _lote(b"")
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "lote_vacio"
        assert r.tipo_real is None  # no `application/x-empty`: no es un tipo, es un fallo

        # Un lote de SQLite (sin tipo de padre, pero hoja) vacío también es `lote_vacio`:
        # el precalificador decide que ese, un hueco de rowid, NO es un fallo.
        r_tabla = _lote(b"", tipo_padre=None)
        assert r_tabla.motivo == "lote_vacio"

        # Fuera de los lotes, igual: declarado > 0 y servido vacío tiene motivo propio.
        r_archivo = precalificar_contenido(
            PERILLAS,
            head=b"",
            abrible=io.BytesIO(b""),
            nombre="datos.sql",
            extension=".sql",
            ruta_relativa="Matrix.rar!datos.sql",
            tamano=4096,
        )
        assert r_archivo.ruta is RutaDecision.COLD
        assert r_archivo.motivo == "servido_vacio"

    @pytest.mark.parametrize(
        ("head", "tipo_firma"),
        [
            (b"x" * 257 + b"ustar de una linea de texto\n" + b"mas texto legible\n" * 60, "tar"),
            (b"ORC y luego texto legible de la ventana\n" * 60, "orc"),
            (b"TAPE y luego texto legible\n" * 60, "mssql-backup"),
        ],
        ids=["ustar_en_257", "orc", "tape"],
    )
    def test_firma_corta_en_una_ventana_no_la_convierte_en_contenedor(
        self, head: bytes, tipo_firma: str
    ) -> None:
        r = _lote(head)
        assert not r.senales.get("es_contenedor")
        assert r.motivo != "contenedor_pendiente_t3"
        assert r.tipo_real == SQL
        assert r.senales["tipo_ventana"].endswith(tipo_firma)
        assert r.ruta is RutaDecision.HOT

    def test_ventana_con_firma_de_imagen_no_se_rutea_como_imagen(self) -> None:
        head = b"\xff\xd8\xff\xe0" + random.Random(5).randbytes(VENTANA - 4)
        r = _lote(head, ocr_activo=True)
        assert not r.motivo.startswith("imagen")
        assert "ocr" not in r.senales
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "lote_ilegible"

    def test_ventana_utf16_sin_bom_de_un_sql_hereda_y_va_a_hot(self) -> None:
        """Las ventanas 2..N del .sql UTF-16 empiezan con el byte alto huérfano del salto
        de línea (0x00) y sin BOM: el detector de nulos las daba por binario."""
        padre = b"\xff\xfe" + _utf16_modo_texto(LINEAS_SQL[:2000])
        inicio = padre.index(b"\n", VENTANA) + 1  # el troceo alinea al byte 0x0A
        head = padre[inicio : inicio + VENTANA]
        assert head[:1] == b"\x00"
        r = _lote(head)
        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == SQL
        assert r.senales["utf16"] is True
        assert r.senales["tipo_heredado"] is True


class TestSqlGanaAHtml:
    def test_sql_con_html_en_un_insert_es_sql(self, tmp_path: Path) -> None:
        """13 .sql de Matrix (1,75 GB) iban a frío como `text/html` porque sus INSERT
        guardan páginas: el `<html` de las columnas le ganaba a las sentencias SQL."""
        contenido = (
            b"INSERT INTO paginas VALUES (1, '<html><body><p>Aviso de privacidad</p>"
            b"</body></html>');\n" * 40
        )

        def decidir(nombre: str, datos: bytes) -> Any:
            ruta = tmp_path / nombre
            ruta.write_bytes(datos)
            return precalificar_archivo(
                PERILLAS,
                ruta,
                nombre=nombre,
                extension=Path(nombre).suffix,
                ruta_relativa=nombre,
                tamano=len(datos),
            )

        r = decidir("respaldo.sql", contenido)
        assert r.tipo_real == SQL
        assert r.ruta is RutaDecision.HOT

        # La extensión solo desempata entre dos señales del contenido; no crea el tipo ni
        # mete el HTML en la lista blanca.
        assert decidir("pagina.sql", _html()).tipo_real == "text/html"
        assert decidir("pagina.sql", _html()).ruta is RutaDecision.COLD
        assert decidir("respaldo.txt", contenido).tipo_real == "text/html"

    def test_sql_que_libmagic_llama_html_solo_se_rescata_si_t2_ve_sql(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si libmagic rechaza un .sql, T2 decide, pero SOLO a favor del SQL. Un .sql de
        Matrix que libmagic llama `text/html` no trae `<html` en sus 2 primeros KB ni
        sentencias: T2 lo leía como `application/xml` y lo mandaba a HOT."""
        monkeypatch.setattr(reglas, "_LIBMAGIC", _LibmagicFalsa("text/html"))

        def decidir(nombre: str, datos: bytes) -> Any:
            ruta = tmp_path / nombre
            ruta.write_bytes(datos)
            return precalificar_archivo(
                PERILLAS,
                ruta,
                nombre=nombre,
                extension=".sql",
                ruta_relativa=nombre,
                tamano=len(datos),
            )

        insert = b"INSERT INTO paginas VALUES (1, '<div><p>Aviso</p></div>');\n" * 40
        r = decidir("respaldo.sql", insert)
        assert r.tipo_real == SQL and r.ruta is RutaDecision.HOT
        assert r.senales["tipo_libmagic"] == "text/html"

        fragmento = b"<div><table><tr><td>Aviso de privacidad</td></tr></table></div>\n" * 40
        r_html = decidir("fragmento.sql", fragmento)
        assert r_html.tipo_real == "text/html"
        assert r_html.ruta is RutaDecision.COLD
        assert r_html.motivo == "fuera_de_lista_blanca"


class TestExtractorUtf16:
    def _extraer(self, datos: bytes) -> str:
        r = extraer(
            PerillasWorker(), io.BytesIO(datos), tipo_real=SQL, nombre="x.sql", tamano=len(datos)
        )
        assert r.texto is not None
        return r.texto

    def test_utf16le_con_cr_sueltos_sale_sin_nul(self) -> None:
        """El doc del .sql UTF-16 de 430 MB entró al índice con un 49,9 % de U+0000."""
        datos = b"\xff\xfe" + _utf16_modo_texto(LINEAS_SQL[:300])
        texto = self._extraer(datos)
        assert "\x00" not in texto
        assert "�" not in texto
        assert "José Muñoz Peña" in texto
        assert "\r\r" not in texto  # el CR suelto no sobrevive
        assert texto.count("INSERT INTO personas") == 300

        # Un lote de ventana: sin BOM y empezando por el byte alto huérfano del salto.
        inicio = datos.index(b"\n", 5000) + 1
        lote = self._extraer(datos[inicio:])
        assert "\x00" not in lote
        assert "José Muñoz Peña" in lote

        # Y un texto latino con algún NUL suelto NO se toma por UTF-16.
        con_nul = "Peña\x00Muñoz;CDMX\n".encode("cp1252") * 200
        assert "Peña" in self._extraer(con_nul)

    def test_utf16_bien_formado_le_y_be(self) -> None:
        texto = "\r\n".join(LINEAS_SQL[:100])
        for bom, codec in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
            sale = self._extraer(bom + texto.encode(codec))
            assert "\x00" not in sale
            assert sale.count("José Muñoz Peña") == 100

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
    @pytest.mark.parametrize(
        "texto",
        [
            "一一一一一一一一一一统一\n" * 200,  # los NUL casi todos del lado CJK
            ("一一一一一一一一一一一一\n" + "linea 12 abc\n") * 200,  # mezclada
        ],
        ids=["cjk_puro", "cjk_y_ascii"],
    )
    def test_con_bom_manda_el_bom_y_no_la_paridad(self, codec: str, texto: str) -> None:
        """一 es U+4E00: su NUL es el byte BAJO y cae del lado contrario al de un carácter
        latino. Alinear por la paridad de los NUL desplazaba todo el texto un byte (o,
        mezclado con ASCII, las líneas donde dominaba el CJK)."""
        bom = b"\xff\xfe" if codec == "utf-16-le" else b"\xfe\xff"
        assert self._extraer(bom + texto.encode(codec)).strip() == texto.strip()


# ---------------------------------------------------------------- de punta a punta


class _Cursor:
    def __init__(self, filas: list[tuple[str, str]]) -> None:
        self._filas = filas

    def fetchall(self) -> list[tuple[str, str]]:
        return self._filas


class _ConexionFalsa:
    """Solo responde la consulta del tipo del padre; lo demás de la cola va simulado."""

    def __init__(self, tipos: dict[str, str]) -> None:
        self.tipos = tipos
        self.consultas: list[tuple[str, Any]] = []

    def __enter__(self) -> _ConexionFalsa:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.consultas.append((sql, params))
        ids = params[0]
        return _Cursor([(i, self.tipos[i]) for i in ids if i in self.tipos])


def _ventana_exacta(contenido: bytes, relleno: bytes = b"-") -> bytes:
    """Rellena hasta EXACTAMENTE una ventana, cerrando en salto de línea: así el lote
    siguiente empieza en inicio de línea y cada lote sirve justo su ventana."""
    assert len(contenido) <= VENTANA - 1
    return contenido + relleno * (VENTANA - 1 - len(contenido)) + b"\n"


def _fila_lote(padre_id: str, padre_ruta: str, desde: int) -> cola.FilaReclamada:
    rango = f"texto/{desde}-{desde + VENTANA}"
    return cola.FilaReclamada(
        archivo_id=f"{padre_id}:{desde}",
        disco_id="d1",
        ruta=f"{padre_ruta}!{rango}",
        nombre=f"parte-{desde}.txt",
        extension=".txt",
        tamano=VENTANA,
        mtime=datetime(2026, 9, 24, tzinfo=UTC),
        estado=Estado.PENDIENTE,
        intentos=0,
        origen_contenedor={
            "cadena": [padre_ruta, rango],
            "profundidad": 1,
            "contenedor_archivo_id": padre_id,
            "hoja": True,
        },
    )


class TestPrecalificarPendientes:
    def test_los_lotes_de_texto_se_deciden_con_el_tipo_del_padre(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sql = b"".join(ln.encode() + b"\n" for ln in LINEAS_SQL[:1400])
        tar = b"x" * 257 + b"ustar de una linea de texto\n" + b"mas texto legible\n" * 60
        (tmp_path / "respaldo.sql").write_bytes(
            _ventana_exacta(sql[: VENTANA - 200])
            + _ventana_exacta(_html())
            + _ventana_exacta(_blob(largo=VENTANA - 200)[:-1], relleno=b"Z")
            + _ventana_exacta(tar)
        )
        (tmp_path / "unicode.sql").write_bytes(b"\xff\xfe" + _utf16_modo_texto(LINEAS_SQL[:3000]))
        filas = [
            _fila_lote("sql-1", "respaldo.sql", VENTANA),
            _fila_lote("sql-1", "respaldo.sql", 2 * VENTANA),
            _fila_lote("sql-1", "respaldo.sql", 3 * VENTANA),
            _fila_lote("u16-1", "unicode.sql", VENTANA),
        ]
        conn = _ConexionFalsa({"sql-1": SQL, "u16-1": SQL})
        guardadas: dict[str, dict[str, Any]] = {}
        lotes = iter([filas, []])

        monkeypatch.setattr(psycopg, "connect", lambda _dsn: conn)
        monkeypatch.setattr(cola, "montajes", lambda _c: {"d1": str(tmp_path)})
        monkeypatch.setattr(cola, "sistema_pausado", lambda _c: False)
        monkeypatch.setattr(cola, "claim", lambda _c, **_kw: next(lotes))
        monkeypatch.setattr(
            cola, "guardar_precalificacion", lambda _c, aid, **kw: guardadas.__setitem__(aid, kw)
        )
        monkeypatch.setattr(cola, "insertar_pendientes", lambda _c, nuevas: len(nuevas))
        monkeypatch.setattr(
            cola, "marcar_error", lambda *a, **_k: pytest.fail(f"marcar_error: {a[-1]}")
        )
        monkeypatch.setattr(
            cola, "fallo_transitorio", lambda *_a, **kw: pytest.fail(f"transitorio: {kw}")
        )

        resumen = precalificador.precalificar_pendientes(Config())

        assert resumen.errores == 0 and resumen.re_encolados == 0
        # UNA consulta por lote de claim, con los padres de los lotes de texto
        assert len(conn.consultas) == 1
        assert sorted(conn.consultas[0][1][0]) == ["sql-1", "u16-1"]

        html = guardadas[f"sql-1:{VENTANA}"]
        assert html["ruta"] is RutaDecision.HOT and html["tipo_real"] == SQL
        assert html["senales"]["tipo_heredado"] is True

        blob = guardadas[f"sql-1:{2 * VENTANA}"]
        assert blob["ruta"] is RutaDecision.COLD and blob["motivo"] == "lote_ilegible"

        tar_lote = guardadas[f"sql-1:{3 * VENTANA}"]
        assert not tar_lote["senales"].get("es_contenedor")
        assert tar_lote["tipo_real"] == SQL and tar_lote["ruta"] is RutaDecision.HOT

        # La ventana UTF-16 puede llegar cruda (0x00 inicial, sin BOM) o ya decodificada por
        # el troceo; de las dos formas es texto del padre y va a HOT como SQL.
        u16 = guardadas[f"u16-1:{VENTANA}"]
        assert u16["ruta"] is RutaDecision.HOT and u16["tipo_real"] == SQL

    def test_lo_servido_vacio_va_a_error_y_no_a_frio(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un archivo que declara bytes y llega vacío, o una ventana de texto vacía, es un
        fallo nuestro: en frío se leía como «no hay nada» (106.140 ventanas de Matrix y 41
        entradas que `unar` dejó en 0 B). A ERROR se ve y `reprocesar-errores` lo recoge."""
        (tmp_path / "vaciado.sql").write_bytes(b"")
        (tmp_path / "respaldo.sql").write_bytes(b"INSERT INTO t VALUES (1);\n" * 100)
        suelto = cola.FilaReclamada(
            archivo_id="vaciado",
            disco_id="d1",
            ruta="vaciado.sql",
            nombre="vaciado.sql",
            extension=".sql",
            tamano=4096,
            mtime=datetime(2026, 9, 24, tzinfo=UTC),
            estado=Estado.PENDIENTE,
            intentos=0,
        )
        # Ventana más allá del final: el padre ya no mide lo que medía al trocearse.
        lote = _fila_lote("sql-1", "respaldo.sql", 10 * VENTANA)
        conn = _ConexionFalsa({"sql-1": SQL})
        errores: dict[str, str] = {}
        lotes = iter([[suelto, lote], []])

        monkeypatch.setattr(psycopg, "connect", lambda _dsn: conn)
        monkeypatch.setattr(cola, "montajes", lambda _c: {"d1": str(tmp_path)})
        monkeypatch.setattr(cola, "sistema_pausado", lambda _c: False)
        monkeypatch.setattr(cola, "claim", lambda _c, **_kw: next(lotes))
        monkeypatch.setattr(
            cola, "guardar_precalificacion", lambda _c, aid, **kw: pytest.fail(f"guardada {aid}")
        )
        monkeypatch.setattr(
            cola,
            "marcar_error",
            lambda _c, aid, _de, motivo, **_k: errores.__setitem__(aid, motivo),
        )

        resumen = precalificador.precalificar_pendientes(Config())

        assert resumen.errores == 2 and resumen.procesados == 0
        assert errores["vaciado"].startswith("servido_vacio: declara 4096 B")
        assert errores[lote.archivo_id].startswith("lote_vacio:")

    def test_un_lote_de_sqlite_vacio_no_es_un_fallo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`tabla_lotes` trocea por RANGOS de rowid y un rango puede caer en un hueco: ese
        lote vacío es legítimo y sigue en frío, no en ERROR."""
        monkeypatch.setattr(contenedores, "abrir_entrada", lambda *_a, **_k: io.BytesIO(b""))
        tabla = _fila_lote("db-1", "base.db", 0)
        assert tabla.origen_contenedor is not None
        tabla.origen_contenedor["cadena"] = ["base.db", "tabla/personas/rowid/1-5000"]

        resultado, nuevas = precalificador._procesar_fila(PERILLAS, PerillasWorker(), "raiz", tabla)
        assert resultado.ruta is RutaDecision.COLD
        assert resultado.motivo == "lote_vacio"
        assert nuevas == []

        texto = _fila_lote("sql-1", "respaldo.sql", VENTANA)
        with pytest.raises(precalificador.ServidoVacio, match="lote_vacio"):
            precalificador._procesar_fila(PERILLAS, PerillasWorker(), "raiz", texto, tipo_padre=SQL)

    def test_solo_los_lotes_de_texto_piden_el_tipo_del_padre(self) -> None:
        """Un lote de SQLite/CSV se sirve como NDJSON: heredarle `application/vnd.sqlite3`
        lo mandaría al extractor de SQLite. Tampoco lo pide un archivo suelto."""
        texto = _fila_lote("sql-1", "respaldo.sql", VENTANA)
        ndjson = _fila_lote("db-1", "base.db", 0)
        assert ndjson.origen_contenedor is not None
        ndjson.origen_contenedor["cadena"] = ["base.db", "tabla/personas/0-5000"]
        suelto = cola.FilaReclamada(
            archivo_id="suelto",
            disco_id="d1",
            ruta="nota.txt",
            nombre="nota.txt",
            extension=".txt",
            tamano=10,
            mtime=datetime(2026, 9, 24, tzinfo=UTC),
            estado=Estado.PENDIENTE,
            intentos=0,
        )
        conn = _ConexionFalsa({"sql-1": SQL, "db-1": "application/vnd.sqlite3"})

        tipos = precalificador._tipos_de_padres(conn, [texto, ndjson, suelto])  # type: ignore[arg-type]

        assert tipos == {texto.archivo_id: SQL}
        assert [p[0] for _, p in conn.consultas] == [["sql-1"]]
        assert precalificador._tipos_de_padres(conn, [ndjson, suelto]) == {}  # type: ignore[arg-type]
        assert len(conn.consultas) == 1  # sin lotes de texto, ni una consulta
