"""Una VENTANA de texto (`texto/…`) nunca es una tabla: hereda el tipo de su padre.

Caso medido (Matrix): 255.614 ventanas de 64 KB de los .sql de 'Matrix.rar' indexadas como
`text/csv`. libmagic o T2 leían como CSV los INSERT con comas y la ventana CONSERVABA ese
tipo. Con la imagen v6 (sin 801345f) el extractor tabular dejaba 859 sin texto y 322 vacías;
eso ya lo arregla `tabular.py`, pero sigue lo demás: los VALORES de la primera fila como
NOMBRES de columna en `campos` y `perfil_calidad`, una ventana JSON/NDJSON cortada que
revienta sin texto y un `tipo_real` falso. Un padre CSV/NDJSON se trocea en lotes NDJSON, no
en ventanas `texto/`: un tipo tabular en una ventana es siempre una detección contra su padre.

Todos los heads son sintéticos e inventados.
"""

from __future__ import annotations

import io
import random
from datetime import UTC, datetime
from typing import Any

import pytest

from normalizacion.core import cache_extraccion, cola
from normalizacion.core.config import Config, PerillasFiltro
from normalizacion.core.modelo import Estado, RutaDecision
from normalizacion.ingesta.precalificacion import reglas
from normalizacion.ingesta.precalificacion.reglas import (
    ResultadoPrecalificacion,
    analizar_head,
    precalificar_contenido,
    refinar_tipo_texto,
)
from normalizacion.ingesta.workers import orquestador

PERILLAS = PerillasFiltro()
SQL = "application/sql"
VENTANA = 64 * 1024  # texto_lotes.BYTES_POR_LOTE
TABULARES = ["text/csv", "application/x-ndjson", "application/json"]


class _LibmagicFalsa:
    """libmagic con un veredicto fijo: la rama ④ de T1 no puede depender de la versión
    instalada (en Windows puede no estar y en la imagen es la 5.44)."""

    def __init__(self, tipo: str) -> None:
        self.tipo = tipo

    def from_buffer(self, _buf: bytes) -> str:
        return self.tipo


@pytest.fixture(autouse=True)
def _sin_libmagic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin libmagic por defecto: el camino (T1 o T2) lo fija cada test, no la máquina."""
    monkeypatch.setattr(reglas, "_LIBMAGIC", None)


def _filas_sql(n: int = 4000) -> bytes:
    """Una ventana que cae DENTRO de un INSERT de muchas filas, una por línea: sin ningún
    `INSERT INTO` a la vista, T2 solo ve filas con el mismo número de comas."""
    filas = (f"({i},'nombre{i}','ciudad{i % 7}',{i * 3}),\n" for i in range(n))
    return b"".join(f.encode() for f in filas)[:VENTANA]


def _ndjson(n: int = 3000) -> bytes:
    return b"".join(f'{{"id": {i}, "n": "valor{i}"}}\n'.encode() for i in range(n))[:VENTANA]


def _json() -> bytes:
    return b'{\n  "clave": "valor",\n  "lista": [1, 2, 3]\n}\n'


def _insert() -> bytes:
    return b"INSERT INTO t VALUES (1,'a','b'),(2,'c','d');\n" * 1400


def _prosa() -> bytes:
    # sin comas, puntos y coma, tabuladores ni barras: con ellos T2 ya la llama CSV
    return b"Esto es una nota de texto sin tabla ni sentencias solo prosa corrida.\n" * 800


def _filas_con_trozo_de_blob(fraccion: float) -> bytes:
    """Filas de un INSERT y, al final de la ventana, el principio de una fila con una foto en
    crudo escapada como mysqldump (sin NUL, LF, CR, comillas ni \\Z). Un ~10 % de los bytes
    de foto son control C0: con un 20-40 % de foto la ventana pasa del 2 % del candado."""
    crudo = bytearray(random.Random(5).randbytes(int(VENTANA * fraccion)))
    for i, b in enumerate(crudo):
        if b in b"\x00\n\r\x1a'\"\\":
            crudo[i] = 0x41
    inicio = b"(99999,'foto','"
    return _filas_sql(5000)[: VENTANA - len(crudo) - len(inicio)] + inicio + bytes(crudo)


HEADS_TABULARES = {
    "text/csv": _filas_sql,
    "application/x-ndjson": _ndjson,
    "application/json": _json,
}


def _tipo_t2(head: bytes) -> str:
    return refinar_tipo_texto(analizar_head(PERILLAS, head, preferir_sql=True))


def _lote(head: bytes, tipo_padre: str | None = SQL) -> ResultadoPrecalificacion:
    return precalificar_contenido(
        PERILLAS,
        head=head,
        abrible=io.BytesIO(head),
        nombre="parte-65536.txt",
        extension=".txt",
        ruta_relativa="respaldo.sql!texto/65536-131072",
        tamano=VENTANA,
        permitir_contenedor_hoja=False,
        tipo_padre=tipo_padre,
    )


# ---------------------------------------------------------------- filtro (reglas)


class TestVentanaConAspectoDeTablaHereda:
    @pytest.mark.parametrize("padre", [SQL, "text/plain"])
    @pytest.mark.parametrize("tipo_visto", TABULARES)
    @pytest.mark.parametrize("via", ["t2", "libmagic"])
    def test_hereda_el_tipo_del_padre(
        self, padre: str, tipo_visto: str, via: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo vea libmagic en T1 o T2 en el head, el tipo tabular de una ventana no se
        queda: la ventana toma el de su padre y va a HOT como texto de ese padre."""
        head = HEADS_TABULARES[tipo_visto]()
        if via == "libmagic":
            monkeypatch.setattr(reglas, "_LIBMAGIC", _LibmagicFalsa(tipo_visto))
        else:
            assert _tipo_t2(head) == tipo_visto  # precondición: T2 SÍ la lee como tabla

        r = _lote(head, tipo_padre=padre)

        assert r.tipo_real == padre
        assert r.ruta is RutaDecision.HOT
        assert r.motivo != "contenedor_pendiente_t3"
        assert r.senales["tipo_heredado"] is True
        assert r.senales["tipo_ventana"] == tipo_visto
        assert r.senales["tipo_padre"] == padre
        assert r.senales["detector"] == ("libmagic" if via == "libmagic" else "texto")

    def test_ventana_binaria_que_libmagic_llama_csv_va_a_frio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con el tipo tabular conservado, una ventana de bytes crudos que libmagic llamaba
        `text/csv` sumaba el peso tabular y entraba a HOT. Heredando, tiene que ser legible
        como cualquier otra ventana."""
        monkeypatch.setattr(reglas, "_LIBMAGIC", _LibmagicFalsa("text/csv"))
        r = _lote(random.Random(17).randbytes(VENTANA))
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "lote_ilegible"
        assert r.senales["texto_legible"] is False
        assert "tipo_heredado" not in r.senales
        assert r.senales["tipo_ventana"] == "text/csv"

    @pytest.mark.parametrize("fraccion", [0.2, 0.3, 0.4])
    def test_filas_con_un_trozo_de_blob_heredan_sin_candado_c0(self, fraccion: float) -> None:
        """Filas de INSERT con una foto al final de la ventana. v6 las mandaba a HOT como
        text/csv, sin candado. Heredar no puede mandarlas a frío por el candado de C0 (2 %):
        del 60 al 80 % de la ventana son filas de texto. Una tabla solo tiene que ser
        legible."""
        head = _filas_con_trozo_de_blob(fraccion)
        assert _tipo_t2(head) == "text/csv"  # precondición: T2 la lee como tabla

        r = _lote(head)

        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == SQL
        assert r.senales["tipo_heredado"] is True
        assert r.senales["tipo_ventana"] == "text/csv"
        assert r.senales["texto_legible"] is True
        assert r.senales["control_c0"] > reglas._CONTROL_C0_MAX_LOTE  # el candado la paraba

    def test_tabla_legible_con_mucho_control_c0_tambien_hereda(self) -> None:
        """Ni con 7,8 % de C0: si pasa `texto_legible` (0,92 de imprimibles) y T2 ve filas,
        es texto. Lo que decide el frío de una tabla es la legibilidad, no el candado."""
        filas = (f"({i},'dato\x1b{i}\x1bx',{i}),\n" for i in range(4000))
        head = b"".join(f.encode() for f in filas)[:VENTANA]
        assert _tipo_t2(head) == "text/csv"  # precondición

        r = _lote(head)

        assert r.ruta is RutaDecision.HOT
        assert r.tipo_real == SQL
        assert r.senales["tipo_heredado"] is True
        assert r.senales["control_c0"] > reglas._CONTROL_C0_MAX_LOTE


class TestLoQueNoCambia:
    """Guardas: pasan igual antes y después del cambio."""

    @pytest.mark.parametrize("libmagic", ["text/csv", None], ids=["libmagic", "sin_libmagic"])
    def test_archivo_sin_padre_csv_sigue_siendo_csv(
        self, libmagic: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if libmagic is not None:
            monkeypatch.setattr(reglas, "_LIBMAGIC", _LibmagicFalsa(libmagic))
        head = b"id,nombre,monto\n" + b"1,ana,10.5\n2,luis,22.0\n3,eva,9.99\n" * 50
        r = precalificar_contenido(
            PERILLAS,
            head=head,
            abrible=io.BytesIO(head),
            nombre="datos.csv",
            extension=".csv",
            ruta_relativa="datos.csv",
            tamano=len(head),
        )
        assert r.tipo_real == "text/csv"
        assert r.motivo == "contenedor_pendiente_t3"  # se trocea en lotes NDJSON
        assert "tipo_heredado" not in r.senales

    def test_lote_ndjson_de_una_tabla_conserva_su_tipo(self) -> None:
        """Un lote de SQLite/CSV (hoja, sin tipo de padre) se sirve como NDJSON y va al
        extractor tabular: ahí la tabla SÍ es el tipo real."""
        r = _lote(_ndjson(), tipo_padre=None)
        assert r.tipo_real == "application/x-ndjson"
        assert r.ruta is RutaDecision.HOT
        assert "tipo_heredado" not in r.senales

    @pytest.mark.parametrize("padre", [SQL, "text/plain"])
    @pytest.mark.parametrize(
        ("head", "tipo_propio"),
        [(_prosa(), "text/plain"), (_insert(), SQL)],
        ids=["prosa", "insert"],
    )
    def test_ventana_con_tipo_propio_de_texto_lo_conserva(
        self, padre: str, head: bytes, tipo_propio: str
    ) -> None:
        assert _tipo_t2(head) == tipo_propio  # precondición
        r = _lote(head, tipo_padre=padre)
        assert r.tipo_real == tipo_propio
        assert r.ruta is RutaDecision.HOT
        assert "tipo_heredado" not in r.senales

    def test_el_candado_c0_sigue_para_lo_que_no_es_tabla(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los mismos bytes con un tipo propio rechazado (`text/html` de libmagic) heredan
        CON el candado, como en v6: la excepción es solo para las tablas."""
        monkeypatch.setattr(reglas, "_LIBMAGIC", _LibmagicFalsa("text/html"))
        r = _lote(_filas_con_trozo_de_blob(0.3))
        assert r.senales["control_c0"] > reglas._CONTROL_C0_MAX_LOTE
        assert r.ruta is RutaDecision.COLD
        assert r.motivo == "lote_ilegible"
        assert r.senales["tipo_ventana"] == "text/html"

    def test_ventana_que_libmagic_llama_texto_de_otro_tipo_lo_conserva(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Excluir las tablas no estrecha lo demás: un `text/*` de libmagic sigue entrando."""
        monkeypatch.setattr(reglas, "_LIBMAGIC", _LibmagicFalsa("text/x-c"))
        r = _lote(_prosa())
        assert r.tipo_real == "text/x-c"
        assert "tipo_heredado" not in r.senales


# ---------------------------------------------------------------- worker (defensa)


def _fila(tipo_real: str, entrada: str | None = "texto/65536-131072") -> cola.FilaReclamada:
    """Una fila HOT como la deja el filtro. `entrada=None` = archivo suelto del disco."""
    origen = (
        {
            "cadena": ["respaldo.sql", entrada],
            "profundidad": 1,
            "contenedor_archivo_id": "padre-1",
            "hoja": True,
        }
        if entrada is not None
        else None
    )
    return cola.FilaReclamada(
        archivo_id="a-1",
        disco_id="d1",
        ruta=f"respaldo.sql!{entrada}" if entrada else "datos.csv",
        nombre="parte-65536.txt" if entrada else "datos.csv",
        extension=".txt" if entrada else ".csv",
        tamano=VENTANA,
        mtime=datetime(2026, 9, 25, tzinfo=UTC),
        estado=Estado.EN_PROCESO,
        intentos=0,
        origen_contenedor=origen,
        tipo_real=tipo_real,
        senales={},
    )


_TABLA_GUARDADA = cache_extraccion.Extraccion(
    hash_contenido="h" * 64,
    texto="",
    campos={"columnas": ["(0", "'nombre0'"]},
    perfil_calidad={"filas": 0, "columnas": {}},
    flags=[],
    confianza=None,
    motor="nativo",
    version_extractor="v2-ocr",
)


def _extraer(
    fila: cola.FilaReclamada,
    datos: bytes,
    monkeypatch: pytest.MonkeyPatch,
    guardada: cache_extraccion.Extraccion | None = None,
) -> tuple[Any, bool, list[str]]:
    consultas: list[str] = []

    def buscar(_conn: Any, hash_contenido: str, **_kw: Any) -> Any:
        consultas.append(hash_contenido)
        return guardada

    monkeypatch.setattr(cache_extraccion, "buscar", buscar)
    monkeypatch.setattr(cache_extraccion, "guardar", lambda *_a, **_kw: None)
    sin_conexion: Any = None  # `buscar`/`guardar` falsos: nadie la usa
    extraccion, reusada = orquestador._extraer_o_reusar(
        Config(), sin_conexion, io.BytesIO(datos), fila, "h" * 64
    )
    return extraccion, reusada, consultas


class TestDefensaDelWorker:
    @pytest.mark.parametrize("tipo", TABULARES)
    def test_ventana_v6_con_tipo_tabular_va_al_plugin_de_texto(
        self, tipo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Las filas que v6 dejó PRECALIFICADO con `text/csv` (o que `reprocesar-errores`
        devuelve con su tipo) no pasan otra vez por el filtro: el worker no las puede
        mandar al plugin tabular."""
        datos = _filas_sql()
        fila = _fila(tipo)

        extraccion, reusada, _ = _extraer(fila, datos, monkeypatch)

        assert not reusada
        assert extraccion.perfil_calidad is None  # ninguna fila tomada por cabecera
        assert set(extraccion.campos) == {"lineas"}
        assert extraccion.texto == datos.decode().strip()
        assert "ventana_tabular_como_texto" in extraccion.flags
        doc = orquestador._construir_doc(fila, "h" * 64, extraccion)
        assert doc.tipo_real == tipo  # la fila no se reescribe: el flag la delata
        assert "ventana_tabular_como_texto" in doc.limites_alcanzados

    @pytest.mark.parametrize("tipo", [SQL, "text/csv"])
    def test_ventana_no_reusa_la_tabla_guardada_por_hash(
        self, tipo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La caché va por hash: la extracción tabular guardada con v6 se le servía igual a
        la ventana ya re-decidida con el tipo de su padre (63.252 `text/csv` nativas en la
        caché de la matriz)."""
        datos = _filas_sql()
        extraccion, reusada, consultas = _extraer(
            _fila(tipo), datos, monkeypatch, guardada=_TABLA_GUARDADA
        )
        assert consultas == []
        assert not reusada
        assert "extraccion_reusada" not in extraccion.flags
        assert extraccion.perfil_calidad is None
        assert extraccion.texto == datos.decode().strip()

    @pytest.mark.parametrize(
        ("tipo", "entrada", "datos"),
        [
            ("text/csv", None, b"id,nombre,monto\n" + b"1,ana,10.5\n2,luis,22.0\n" * 50),
            ("application/x-ndjson", "csv/0-5000", _ndjson(200)),
        ],
        ids=["csv_suelto", "lote_ndjson"],
    )
    def test_lo_que_no_es_ventana_sigue_por_la_cache_y_el_plugin_tabular(
        self,
        tipo: str,
        entrada: str | None,
        datos: bytes,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Guarda: un CSV suelto y un lote NDJSON de tabla siguen igual."""
        fila = _fila(tipo, entrada)
        extraccion, reusada, consultas = _extraer(fila, datos, monkeypatch)
        assert consultas == ["h" * 64]
        assert not reusada
        assert extraccion.perfil_calidad is not None  # plugin tabular
        assert "ventana_tabular_como_texto" not in extraccion.flags

        _, reusada, _ = _extraer(fila, datos, monkeypatch, guardada=_TABLA_GUARDADA)
        assert reusada
