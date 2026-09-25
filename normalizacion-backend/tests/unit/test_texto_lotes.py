"""Un texto/log/SQL/dump grande troceado en ventanas: el 100 % del contenido, no los
primeros 100k chars. Espejo de `test_tabla_plana.py`. La propiedad es COBERTURA: la unión
de los trozos servidos reproduce el archivo entero, sin repetir ni perder — y el borde que
importa: un dato tras el byte 150k (más allá de lo que cabría en el doc único truncado)
ahora vive en un trozo buscable."""

from __future__ import annotations

import codecs
import io
import random

import pytest

from normalizacion.core.config import PerillasFiltro, PerillasWorker
from normalizacion.ingesta.precalificacion import texto_lotes
from normalizacion.ingesta.precalificacion.reglas import (
    RutaDecision,
    decodificar_utf16,
    precalificar_contenido,
)
from normalizacion.ingesta.precalificacion.texto_lotes import (
    ALCANCE_FRONTERA,
    BYTES_POR_LOTE,
    explorar,
    planificar,
    servir_lote,
)

GRANDE = 1 << 30
CURP = "GOMC800101HDFXXX01"
#: `texto.py` trunca a `extractor_max_chars` (100k): un lote mayor pierde su cola.
TOPE_EXTRACTOR = PerillasWorker().extractor_max_chars


def _servir_todo(datos: bytes, lotes) -> bytes:
    partes = []
    for lote in lotes:
        spool = servir_lote(
            io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
        )
        partes.append(spool.read())
    return b"".join(partes)


def _servidos(datos: bytes, **kw) -> list[bytes]:
    """Cada lote servido POR SEPARADO, como en producción (cada uno en su proceso)."""
    return [
        servir_lote(
            io.BytesIO(datos), lt.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
        ).read()
        for lt in planificar(io.BytesIO(datos), **kw)
    ]


def _servir_como_antes(datos: bytes, desde: int, hasta: int) -> bytes:
    """El servido anterior a acotar la frontera: saltar la línea parcial y terminar la que
    cruza el borde, SIN tope. Referencia para comprobar que el texto normal no cambia."""

    def frontera(x: int) -> int:
        if x <= 0:
            return 0
        i = datos.find(b"\n", x - 1)
        return i + 1 if i >= 0 else len(datos)

    return datos[frontera(desde) : frontera(hasta)]


class _LectorContado(io.BytesIO):
    """Un file-like que cuenta los bytes que se le leen."""

    def __init__(self, datos: bytes) -> None:
        super().__init__(datos)
        self.leidos = 0

    def read(self, n: int | None = -1) -> bytes:
        bloque = super().read(n)
        self.leidos += len(bloque)
        return bloque


def _assert_iguales(obtenido: str | bytes, esperado: str | bytes) -> None:
    """`==` sin el diff de pytest, que con líneas de cientos de KB tarda minutos: dice
    cuántas posiciones difieren y dónde empieza la primera."""
    if obtenido == esperado:
        return
    pares = list(zip(obtenido, esperado, strict=False))  # las longitudes pueden diferir
    primera = next((k for k, (a, b) in enumerate(pares) if a != b), len(pares))
    distintas = sum(a != b for a, b in pares)
    pytest.fail(
        f"{distintas} posiciones distintas; longitudes {len(obtenido)} y {len(esperado)}; "
        f"la primera en {primera}: {obtenido[primera : primera + 30]!r} "
        f"en vez de {esperado[primera : primera + 30]!r}"
    )


def _assert_troceo_sano(datos: bytes, servidos: list[bytes]) -> None:
    """(a) ningún lote vacío, (b) ninguno truncable por el extractor, (c) sin huecos ni
    solapes."""
    vacios = [i for i, s in enumerate(servidos) if not s]
    assert vacios == [], f"{len(vacios)} de {len(servidos)} lotes vacíos"
    assert max(len(s) for s in servidos) <= TOPE_EXTRACTOR
    _assert_iguales(b"".join(servidos), datos)


class TestCobertura:
    def test_la_union_de_trozos_es_el_archivo_entero(self) -> None:
        # 5000 líneas → varios cientos de KB → varias ventanas de 64 KiB
        datos = "".join(f"linea numero {i} con algo de relleno\n" for i in range(5000)).encode()
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=16 * 1024)
        assert len(lotes) > 1
        assert _servir_todo(datos, lotes) == datos  # sin pérdida ni duplicado

    def test_un_dato_tras_el_byte_150k_es_buscable(self) -> None:
        """EL bug: hoy el texto se trunca a 100k chars y una CURP en el byte 150k es
        invisible. Troceado, cae en un trozo que sí se indexa."""
        relleno = ("x" * 78 + "\n").encode()  # ~79 B por línea
        cabeza = relleno * 1900  # ~150 KB antes de la CURP
        datos = cabeza + (f"registro con {CURP} dentro\n").encode() + relleno * 500
        assert len(cabeza) > 130_000  # la CURP está bien pasado el corte de 100k
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=64 * 1024)
        recuperado = _servir_todo(datos, lotes)
        assert recuperado.count(CURP.encode()) == 1  # aparece EXACTAMENTE una vez
        # y en particular, en algún trozo servido (no perdido por el truncado)
        assert CURP.encode() in recuperado

    def test_linea_que_cruza_la_ventana_se_sirve_una_vez(self) -> None:
        # líneas de ~100 B con ventana de 256 B → varias líneas cruzan bordes
        datos = ("\n".join(f"L{i:04d}-" + "y" * 90 for i in range(200)) + "\n").encode()
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=256)
        assert _servir_todo(datos, lotes) == datos


class TestLineasLargas:
    """'Matrix.rar': dumps con INSERT extendidos de hasta 1.046.626 B por línea. La ventana
    que caía entera dentro de una línea larga salía VACÍA (106.148 lotes x-empty a COLD) y
    el lote donde empezaba la línea la servía ENTERA, ~1 MB que `texto.py` truncaba a 100k
    chars (≈5,3 GB de SQL fuera del índice)."""

    def test_el_limite_del_lote_cabe_en_el_extractor(self) -> None:
        # peor caso: frontera final a ALCANCE-1 tras `hasta` e inicial 3 B antes de `desde`
        assert BYTES_POR_LOTE + ALCANCE_FRONTERA + 2 <= TOPE_EXTRACTOR
        assert 2 * ALCANCE_FRONTERA <= BYTES_POR_LOTE  # los bordes vecinos no se cruzan

    def test_reproduccion_medida_insert_de_300kb(self) -> None:
        """La reproducción mínima de la investigación: 10 ventanas, 8 salían vacías."""
        insert = b"INSERT INTO t VALUES " + b"(1,2)," * 50_000 + b"(1,2);\n"
        datos = b"-- cabecera\n" + insert * 2
        servidos = _servidos(datos)
        assert len(servidos) == 10
        _assert_troceo_sano(datos, servidos)

    def test_lineas_de_1mb_sin_lotes_vacios_ni_gigantes(self) -> None:
        linea = b"INSERT INTO `personas` VALUES " + b"(7,'Juan','Perez')," * 55_000
        assert len(linea) > 1_000_000
        # la última línea sin \n final: el EOF no es un inicio de línea
        datos = b"-- volcado\n" + linea + b";\n" + linea + b";\n" + linea
        _assert_troceo_sano(datos, _servidos(datos))

    def test_linea_larga_que_termina_en_el_eof_no_vacia_el_ultimo_lote(self) -> None:
        datos = b"-- volcado\n" + b"(1,'x')," * 40_960 + b";\n"  # última ventana de 13 B
        assert len(datos) % BYTES_POR_LOTE < ALCANCE_FRONTERA
        _assert_troceo_sano(datos, _servidos(datos))

    def test_mezcla_de_lineas_cortas_y_largas_sigue_alineada_en_las_cortas(self) -> None:
        cortas = [f"INSERT INTO t VALUES ({i}, 'fila corta {i}');\n".encode() for i in range(3000)]
        largas = [
            b"INSERT INTO t VALUES " + b"(9,'relleno largo')," * 40_000 + b";\n",  # ~800 KB
            b"INSERT INTO u VALUES " + b"(8,'otro')," * 35_000 + b";\n",  # ~385 KB
        ]
        bloques = [
            b"".join(cortas), largas[0], b"".join(cortas[:1500]), largas[1], b"".join(cortas[:700])
        ]
        datos = b"".join(bloques)
        tramos_largos = []
        pos = 0
        for b in bloques:
            if b in largas:
                tramos_largos.append((pos, pos + len(b)))
            pos += len(b)

        servidos = _servidos(datos)
        _assert_troceo_sano(datos, servidos)
        fronteras, pos = [], 0
        for s in servidos[:-1]:
            pos += len(s)
            fronteras.append(pos)
        dentro = [f for f in fronteras if any(a < f < b for a, b in tramos_largos)]
        fuera = [f for f in fronteras if f not in dentro]
        assert dentro and fuera  # el caso ejercita los dos tipos de borde
        # (d) entre líneas cortas, cada corte cae en un inicio de línea: ninguna se parte
        assert all(datos[f - 1] == ord("\n") for f in fuera)

    @pytest.mark.parametrize("caracter", ["ñ", "€", "😀"])
    @pytest.mark.parametrize("prefijo", [b"", b"x", b"xy", b"xyz"])
    def test_el_corte_en_mitad_de_linea_no_parte_un_multibyte(
        self, caracter: str, prefijo: bytes
    ) -> None:
        # una sola línea de ~300 KB: todos los cortes caen dentro, en offsets múltiplos de
        # 64 KiB que el prefijo desalinea respecto a los caracteres de 2, 3 y 4 bytes
        datos = prefijo + (caracter * (300_000 // len(caracter.encode()))).encode()
        servidos = _servidos(datos)
        _assert_troceo_sano(datos, servidos)
        for s in servidos:
            s.decode("utf-8")  # estricto: ningún lote empieza ni acaba a mitad de carácter

    def test_un_trozo_de_linea_larga_pasa_la_puerta_como_texto(self) -> None:
        """La ventana vacía llegaba a libmagic como b'' → 'application/x-empty' → COLD
        'fuera_de_lista_blanca'. El trozo servido es texto y va a HOT."""
        insert = b"INSERT INTO t VALUES " + b"(1,'Ana Lopez','CDMX')," * 20_000 + b";\n"
        datos = b"-- cabecera\n" + insert * 2
        for i, s in enumerate(_servidos(datos)):
            r = precalificar_contenido(
                PerillasFiltro(), head=s[:65_536], abrible=io.BytesIO(s), nombre=f"parte-{i}.txt",
                extension=".txt", ruta_relativa="x", tamano=BYTES_POR_LOTE,
                permitir_contenedor_hoja=False,
            )
            assert r.tipo_real != "application/x-empty" and r.ruta == RutaDecision.HOT, r


class TestTextoNormalNoCambia:
    """Con líneas por debajo del alcance, cada lote sale BYTE A BYTE igual que antes de
    acotar la frontera: los lotes ya indexados de un texto normal conservan contenido y
    `hash_contenido`, y re-servir solo parte de un padre no deja huecos ni duplicados."""

    @pytest.mark.parametrize("bytes_por_lote", [BYTES_POR_LOTE, 16 * 1024])
    def test_mismos_bytes_que_el_servido_anterior(self, bytes_por_lote: int) -> None:
        rnd = random.Random(7)
        lineas = [
            ("z" * rnd.choice([0, 5, 80, 400, 3_000, 20_000]) + f" {i}\n").encode()
            for i in range(400)
        ]
        datos = b"".join(lineas)
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=bytes_por_lote)
        # si la última línea cubriera la última ventana, el caso cambia a propósito (test
        # siguiente): aquí la última ventana tiene un inicio de línea antes del EOF
        assert datos.find(b"\n", lotes[-1].desde - 1, len(datos) - 1) >= 0
        for lt in lotes:
            servido = servir_lote(
                io.BytesIO(datos), lt.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
            ).read()
            assert servido == _servir_como_antes(datos, lt.desde, lt.hasta)

    def test_la_ultima_linea_que_cubre_la_ultima_ventana_no_la_deja_vacia(self) -> None:
        """Líneas de 100 B y la última cruza el borde de la última ventana y acaba en el
        EOF: antes el último lote salía vacío. Ahora se lleva la línea entera."""
        datos = b"".join(f"{i:099d}\n".encode() for i in range(1967))  # 196.700 B
        assert datos.rfind(b"\n", 0, 3 * BYTES_POR_LOTE) + 1 < 3 * BYTES_POR_LOTE
        servidos = _servidos(datos)
        _assert_troceo_sano(datos, servidos)
        assert servidos[-1] == f"{1966:099d}\n".encode()  # la línea final, entera

    def test_un_lote_intermedio_no_lee_el_alcance_hacia_atras(self) -> None:
        """Hacia atrás del borde solo busca la regla del EOF: un lote intermedio lee su
        ventana y el alcance por delante (~98 KB), no ~131 KB. Al reprocesar ~23 GB de texto
        con el disco ya saturado, esa cuarta parte de la lectura sobraba."""
        datos = b"".join(f"{i:079d}\n".encode() for i in range(20_000))  # 1,6 MB
        lector = _LectorContado(datos)
        lote = planificar(io.BytesIO(datos))[10]
        servido = servir_lote(
            lector, lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
        ).read()
        assert servido == _servir_como_antes(datos, lote.desde, lote.hasta)
        cabecera = 4096  # la que lee cada lote para saber si el padre es UTF-16
        assert lector.leidos <= cabecera + BYTES_POR_LOTE + ALCANCE_FRONTERA + 16


def _viejos(datos: bytes, **kw) -> list[bytes]:
    lotes = planificar(io.BytesIO(datos), **kw)
    return [_servir_como_antes(datos, lt.desde, lt.hasta) for lt in lotes]


def _cambia_por_fuerza_bruta(datos: bytes, **kw) -> bool:
    return _servidos(datos, **kw) != _viejos(datos, **kw)


def _linea_de_90kb_sin_ventana_vacia() -> bytes:
    """El caso que la revisión encontró fuera de los 49 padres afectados: una línea de 90 KB
    —más que el alcance, menos que la ventana— que empieza 40 KB antes del borde 196.608.
    Ninguna ventana cae entera dentro de ella, así que nunca hubo lote vacío."""
    prefijo = b"".join(f"{i:099d}\n".encode() for i in range(1556)) + b"x" * 47 + b"\n"
    assert len(prefijo) == 3 * BYTES_POR_LOTE - 40 * 1024
    linea = b"INSERT INTO t VALUES " + b"(1,'x')," * 11_247 + b";\n"
    assert ALCANCE_FRONTERA < len(linea) < BYTES_POR_LOTE + 30_000
    return prefijo + linea + b"".join(f"{i:099d}\n".encode() for i in range(2000))


def _caso_cambia(nombre: str) -> tuple[bytes, bool]:
    cortas = b"".join(f"INSERT INTO t VALUES ({i}, 'fila {i}');\n".encode() for i in range(4000))
    rnd = random.Random(11)
    if nombre == "lineas_normales":
        largos = [0, 5, 80, 400, 3_000, 20_000]  # todas por debajo del alcance
        lineas = [("z" * rnd.choice(largos) + f" {i}\n").encode() for i in range(300)]
        return b"".join(lineas) + cortas, False
    if nombre == "una_linea_por_debajo_del_alcance":
        return cortas + b"y" * (ALCANCE_FRONTERA - 100) + b"\n" + cortas, False
    if nombre == "linea_de_90kb_sin_ventana_vacia":
        return _linea_de_90kb_sin_ventana_vacia(), True
    if nombre == "insert_de_1mb":
        return b"-- volcado\n" + b"(7,'Juan','Perez')," * 55_000 + b";\n" + cortas, True
    if nombre == "la_ultima_linea_cubre_la_ultima_ventana":
        return b"".join(f"{i:099d}\n".encode() for i in range(1967)), True
    if nombre == "utf16":
        return _utf16(_lineas_sql(2000), "utf-16-le", bom=True, fin="\r\n", modo_texto=False), True
    if nombre == "una_sola_ventana":
        return b"hola\n" * 10, False
    assert nombre == "vacio"
    return b"", False


class TestCambiaLoServido:
    """Qué padres hay que re-servir ENTEROS al pasar a este troceo. La revisión lo midió en
    Matrix: además de los 49 padres con lotes vacíos o UTF-16, cambian 39 lotes de otros 8
    padres con líneas de 48-90 KB (31 ya INDEXADO, 14 truncados en el índice por pasar de
    100 KB). Elegir por «tiene x-empty o es UTF-16» los dejaba fuera, y mezclar en un padre
    lotes re-servidos con lotes viejos pierde el tramo entre las dos fronteras."""

    @pytest.mark.parametrize(
        "nombre",
        [
            "lineas_normales",
            "una_linea_por_debajo_del_alcance",
            "linea_de_90kb_sin_ventana_vacia",
            "insert_de_1mb",
            "la_ultima_linea_cubre_la_ultima_ventana",
            "utf16",
            "una_sola_ventana",
            "vacio",
        ],
    )
    def test_coincide_con_comparar_lote_a_lote(self, nombre: str) -> None:
        datos, esperado = _caso_cambia(nombre)
        assert _cambia_por_fuerza_bruta(datos) is esperado
        assert texto_lotes.cambia_lo_servido(io.BytesIO(datos)) is esperado

    def test_la_linea_de_90kb_cambia_sin_haber_dejado_ninguna_ventana_vacia(self) -> None:
        datos = _linea_de_90kb_sin_ventana_vacia()
        viejos = _viejos(datos)
        assert all(viejos)  # nunca hubo x-empty: el criterio «tiene lotes vacíos» no lo ve
        assert max(len(v) for v in viejos) > TOPE_EXTRACTOR  # y estaba truncado en el índice
        assert texto_lotes.cambia_lo_servido(io.BytesIO(datos))

    def test_con_tope_de_lotes_solo_cuentan_los_bordes_planificados(self) -> None:
        datos = _linea_de_90kb_sin_ventana_vacia()  # el borde que cambia es el 3.º
        for max_lotes, esperado in [(2, False), (3, True)]:
            assert _cambia_por_fuerza_bruta(datos, max_lotes=max_lotes) is esperado
            assert texto_lotes.cambia_lo_servido(io.BytesIO(datos), max_lotes=max_lotes) is esperado

    def test_lee_unos_kb_por_borde_no_el_archivo(self) -> None:
        datos = b"".join(f"{i:079d}\n".encode() for i in range(20_000))  # 1,6 MB
        lector = _LectorContado(datos)
        assert not texto_lotes.cambia_lo_servido(lector)
        assert lector.leidos < len(datos) // 8


def _utf16(lineas: list[str], codec: str, *, bom: bool, fin: str, modo_texto: bool) -> bytes:
    cuerpo = b"".join((ln + fin).encode(codec) for ln in lineas)
    if modo_texto:  # lo que hace un stream en modo texto de Windows: CADA byte 0x0A
        cuerpo = cuerpo.replace(b"\n", b"\r\n")
    marca = codecs.BOM_UTF16_LE if codec == "utf-16-le" else codecs.BOM_UTF16_BE
    return marca * bom + cuerpo


def _lineas_sql(n: int) -> list[str]:
    return [
        f"INSERT INTO personas VALUES ({i},'José Pérez Núñez','Añil 12, Coyoacán',"
        f"'{CURP if i == n // 2 else 'SIN CURP'}');"
        for i in range(n)
    ]


def _nul_denso() -> bytes:
    """Los falsos positivos medidos en Matrix: .sql de un byte por carácter que abren con
    regiones densas de NUL (47 % en los primeros 4 KB) y siguen en ASCII."""
    return (b"\x00" * 7 + b"A") * 256 + b"\n" + b"INSERT INTO t VALUES (1,'x');\n" * 8_000


def _assert_utf16_servido(servidos: list[bytes], esperado: str) -> None:
    assert all(servidos), "ningún lote vacío"
    for s in servidos:
        assert b"\x00" not in s  # sin NUL residuales: T1 ⑤ ya no lo toma por binario
        assert not s.startswith((b"\xff\xfe", b"\xfe\xff"))
        s.decode("utf-8")  # estricto
    texto = b"".join(servidos).decode("utf-8")
    assert "\N{REPLACEMENT CHARACTER}" not in texto
    _assert_iguales(texto, esperado)


class TestUtf16:
    """'Matrix.rar': un .sql de 430 MB en UTF-16LE con BOM pasaba T2 entero, pero sus
    lotes 2..N empezaban por 0x00 y sin BOM, y T1 los tomaba por binario ('nulos'): 6.560
    de 6.561 a COLD. Encima, TODOS sus fines de línea eran `0D 00 0D 0A 00` (la traducción
    \\n→\\r\\n de un stream en modo texto sobre bytes UTF-16), que invierte la alineación de
    una línea a la siguiente."""

    def test_utf16le_con_bom_y_fines_de_linea_corruptos(self) -> None:
        lineas = _lineas_sql(6000)
        datos = _utf16(lineas, "utf-16-le", bom=True, fin="\r\n", modo_texto=True)
        assert datos.count(b"\r\x00\r\n\x00") == len(lineas)  # el patrón medido en Matrix
        assert len(datos) > 100_000
        servidos = _servidos(datos)
        assert len(servidos) > 10
        _assert_utf16_servido(servidos, "".join(ln + "\r\n" for ln in lineas))
        assert b"".join(servidos).count(CURP.encode()) == 1

    @pytest.mark.parametrize(
        ("codec", "bom", "fin", "modo_texto"),
        [
            ("utf-16-le", True, "\r\n", False),
            ("utf-16-le", True, "\n", False),
            ("utf-16-be", True, "\r\n", False),
            ("utf-16-le", False, "\r\n", False),
            ("utf-16-be", False, "\n", False),
            ("utf-16-le", True, "\r\n", True),
            ("utf-16-le", False, "\r\n", True),
            ("utf-16-be", True, "\r\n", True),
            ("utf-16-be", False, "\r\n", True),
        ],
    )
    def test_utf16_con_y_sin_bom_bien_formado_o_en_modo_texto(
        self, codec: str, bom: bool, fin: str, modo_texto: bool
    ) -> None:
        # caracteres cuyo UTF-16 lleva un byte 0x0A (上 U+4E0A, Ċ U+010A, ਅ U+0A05): partir
        # a ciegas por 0x0A los rompería, y en modo texto llevan un 0x0D insertado en
        # mitad de la línea que invierte la alineación del resto
        lineas = [f"{ln} 上Ċਅ 😀" if i % 7 == 0 else ln for i, ln in enumerate(_lineas_sql(4000))]
        datos = _utf16(lineas, codec, bom=bom, fin=fin, modo_texto=modo_texto)
        _assert_utf16_servido(_servidos(datos), "".join(ln + fin for ln in lineas))

    def test_utf16_sin_bom_con_emoji_al_principio_no_se_lee_al_reves(self) -> None:
        """Sin BOM, deducir el orden de bytes de en qué lado caen los NUL de la 1.ª línea
        fallaba si empieza por un emoji: su sustituto bajo U+DE00 lleva el 0x00 en el byte
        BAJO, y con el 0x0D que el modo texto mete en 上 la «línea» son 5 bytes."""
        lineas = ["😀上 volcado", *_lineas_sql(3000)]
        datos = _utf16(lineas, "utf-16-le", bom=False, fin="\r\n", modo_texto=True)
        _assert_utf16_servido(_servidos(datos), "".join(ln + "\r\n" for ln in lineas))

    def test_utf16_con_lineas_largas_corta_en_la_unidad_de_2_bytes(self) -> None:
        """Un INSERT de ~700 KB en UTF-16 en modo texto: los cortes caen en mitad de línea
        y la línea larga empieza en offset IMPAR (la anterior es impar por el 0x0D suelto),
        así que cortar en el offset par de la ventana partiría todas sus unidades; y cada
        上 lleva dentro un 0x0D insertado que vuelve a invertir la alineación."""
        larga = "INSERT INTO t VALUES " + "(1,'Ñandú 😀 Pérez 上')," * 15_000 + "(2,'fin');"
        lineas = ["-- cabecera", larga, "-- pie", larga]
        datos = _utf16(lineas, "utf-16-le", bom=True, fin="\r\n", modo_texto=True)
        assert len(datos) > 1_000_000
        servidos = _servidos(datos)
        assert max(len(s) for s in servidos) <= TOPE_EXTRACTOR
        _assert_utf16_servido(servidos, "".join(ln + "\r\n" for ln in lineas))

    def test_el_lote_utf16_servido_pasa_la_puerta_como_texto(self) -> None:
        lineas = _lineas_sql(3000)
        datos = _utf16(lineas, "utf-16-le", bom=True, fin="\r\n", modo_texto=True)
        for i, s in enumerate(_servidos(datos)):
            r = precalificar_contenido(
                PerillasFiltro(), head=s[:65_536], abrible=io.BytesIO(s), nombre=f"parte-{i}.txt",
                extension=".sql", ruta_relativa="x", tamano=BYTES_POR_LOTE,
                permitir_contenedor_hoja=False,
            )
            assert r.senales.get("detector") != "nulos" and r.ruta == RutaDecision.HOT, r

    @pytest.mark.parametrize("codec", ["utf-16-be", "utf-16-le"])
    @pytest.mark.parametrize("cortas_al_principio", [0, 1])
    @pytest.mark.parametrize("modo_texto", [True, False])
    def test_modo_texto_con_la_primera_linea_mas_larga_que_la_cabecera(
        self, codec: str, cortas_al_principio: int, modo_texto: bool
    ) -> None:
        """Un dump que abre con un INSERT de más de 64 KB no deja ver ningún salto en la
        cabecera, o solo uno. Decidido con esa cabecera, un UTF-16BE en modo texto no se
        detectaba: su salto `00 0D 0A` no contiene el `00 0A` bien formado y la alineación
        se invertía en cada salto (la mitad del texto, basura). Bien formado, no se puede
        tomar por modo texto."""
        # sin caracteres con un byte 0x0A (上, Ċ…): en modo texto llevan su 0x0D delante y
        # delatarían el modo texto sin necesidad de ver ningún salto
        larga = "INSERT INTO t VALUES " + "(1,'Ñandú Pérez 😀')," * 6_000 + "(2,'fin');"
        cortas = [f"-- comentario {i}" for i in range(cortas_al_principio)]
        lineas = [*cortas, larga, "-- pie", *_lineas_sql(300), larga, *_lineas_sql(300)]
        datos = _utf16(lineas, codec, bom=True, fin="\r\n", modo_texto=modo_texto)
        assert datos[:65_536].count(b"\n") <= cortas_al_principio
        servidos = _servidos(datos)
        assert max(len(s) for s in servidos) <= 3 * TOPE_EXTRACTOR // 2
        _assert_utf16_servido(servidos, "".join(ln + "\r\n" for ln in lineas))

    def test_una_region_de_relleno_se_sirve_como_nul_y_no_vacia(self) -> None:
        """Un lote que cae entero en una región de U+0000 salía vacío (los NUL se quitan), y
        vacío se lee como un fallo del troceo. Se sirve como lo que es: NUL, que T1 manda a
        frío como binario. El texto de alrededor sigue saliendo sin NUL."""
        cabeza = "".join(ln + "\r\n" for ln in _lineas_sql(1500))
        cola = "".join(ln + "\r\n" for ln in _lineas_sql(1500))
        datos = (
            codecs.BOM_UTF16_LE + cabeza.encode("utf-16-le") + b"\x00" * 400_000
            + cola.encode("utf-16-le")
        )
        inicio_relleno = 2 + len(cabeza.encode("utf-16-le"))
        servidos = _servidos(datos)
        lotes = planificar(io.BytesIO(datos))
        assert all(servidos), "ningún lote vacío"
        de_relleno = [
            s for lt, s in zip(lotes, servidos, strict=True)
            if lt.desde >= inicio_relleno + ALCANCE_FRONTERA
            and lt.hasta + ALCANCE_FRONTERA <= inicio_relleno + 400_000
        ]
        assert len(de_relleno) >= 3 and all(set(s) == {0} for s in de_relleno)
        _assert_iguales(b"".join(servidos).replace(b"\x00", b"").decode("utf-8"), cabeza + cola)

    @pytest.mark.parametrize(
        "nombre",
        ["le_con_bom", "le_sin_bom", "be_sin_bom", "polaco_sin_bom", "cirilico_sin_bom",
         "nul_denso", "sql_de_8_bits"],
    )
    def test_decide_utf16_con_el_mismo_criterio_que_reglas(self, nombre: str) -> None:
        """Si `reglas` (T1/T2, `texto.py`) da el padre por UTF-16, sus lotes se sirven
        decodificados, y si no, en crudo. Con una heurística propia (NUL 40-60 %, ≥ 95 % de
        bytes latinos) discrepaban casos reales: un volcado en polaco (Ł, Ż, ś son U+01xx:
        su byte 0x01 no es latino) o en cirílico con un 35 % de NUL los aceptaba `reglas` y
        aquí se servían en crudo, con los NUL que T1 manda a frío."""
        polaco = [
            f"INSERT INTO osoby VALUES ({i},'Łukasz Żółkiewski','ul. Świętokrzyska 12, Łódź');"
            for i in range(3000)
        ]
        cirilico = [
            f"INSERT INTO lyudi VALUES ({i},'Иван Петрович','Москва');" for i in range(3000)
        ]
        sql = _lineas_sql(3000)
        casos: dict[str, tuple[list[str], str, bool, bool]] = {
            "le_con_bom": (sql, "utf-16-le", True, True),
            "le_sin_bom": (sql, "utf-16-le", False, True),
            "be_sin_bom": (sql, "utf-16-be", False, False),
            "polaco_sin_bom": (polaco, "utf-16-le", False, False),
            "cirilico_sin_bom": (cirilico, "utf-16-le", False, False),
        }
        if nombre in casos:
            lineas, codec, bom, modo_texto = casos[nombre]
            datos = _utf16(lineas, codec, bom=bom, fin="\r\n", modo_texto=modo_texto)
            esperado: bytes | None = "".join(ln + "\r\n" for ln in lineas).encode()
        elif nombre == "nul_denso":
            datos, esperado = _nul_denso(), None
        else:
            datos, esperado = "".join(ln + "\n" for ln in sql).encode("utf-8"), None
        # el veredicto de `reglas` sobre la ventana T1 es el que manda
        assert (decodificar_utf16(datos[:4096]) is not None) is (esperado is not None)
        _assert_iguales(b"".join(_servidos(datos)), datos if esperado is None else esperado)

    def test_nul_denso_con_paridad_mezclada_no_es_utf16(self) -> None:
        """Los falsos positivos medidos: .sql de un byte por carácter con regiones densas
        de NUL (47 % en los primeros 4 KB, el resto ASCII). Pasan el umbral de NUL, pero la
        paridad está mezclada: se sirven tal cual, no «decodificados» a basura."""
        datos = _nul_denso()
        cab = datos[:4096]
        assert 0.40 <= cab.count(0) / len(cab) <= 0.60
        _assert_iguales(b"".join(_servidos(datos)), datos)


class TestIncremental:
    def test_subir_el_tope_no_mueve_los_trozos(self) -> None:
        datos = ("\n".join(f"linea {i}" for i in range(2000)) + "\n").encode()
        pocos = planificar(io.BytesIO(datos), bytes_por_lote=1024, max_lotes=3)
        muchos = planificar(io.BytesIO(datos), bytes_por_lote=1024, max_lotes=1000)
        assert [x.ruta_interna for x in pocos] == [x.ruta_interna for x in muchos[:3]]

    def test_las_ventanas_no_cambian_al_reprocesar(self) -> None:
        """El arreglo del servido NO puede mover `ruta_interna`: de ella sale el
        `archivo_id`, y reprocesar Matrix duplicaría cada lote en el índice."""
        lotes = planificar(io.BytesIO(b"x" * 200_000))
        assert [lt.ruta_interna for lt in lotes] == [
            "texto/0-65536", "texto/65536-131072", "texto/131072-196608", "texto/196608-200000",
        ]


class TestServeDeterminista:
    def test_servir_el_mismo_trozo_da_los_mismos_bytes(self) -> None:
        datos = ("\n".join(f"linea {i} zzz" for i in range(3000)) + "\n").encode()
        lote = planificar(io.BytesIO(datos), bytes_por_lote=16 * 1024)[1]
        a, b = (
            servir_lote(
                io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
            ).read()
            for _ in range(2)
        )
        assert a == b and a


class TestExplorar:
    def test_entradas_y_topado(self) -> None:
        datos = ("\n".join(f"linea {i} con relleno" for i in range(3000)) + "\n").encode()
        entradas, motivo, topado = explorar(PerillasFiltro(), io.BytesIO(datos), 123)
        assert motivo is None and not topado
        assert entradas and entradas[0][0].startswith("texto/")

    def test_topado_se_reporta(self) -> None:
        datos = ("\n".join("x" * 100 for _ in range(4000)) + "\n").encode()  # ~400 KB
        perillas = PerillasFiltro(t3_entradas_max=2)
        entradas, _motivo, topado = explorar(perillas, io.BytesIO(datos), 0)
        assert topado and len(entradas) == 2
