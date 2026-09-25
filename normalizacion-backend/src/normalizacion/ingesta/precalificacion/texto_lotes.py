"""Un archivo de TEXTO grande (text/plain, SQL, XML, rfc822, JSON) tratado como
CONTENEDOR de trozos: cada ventana de bytes es una entrada, servida como texto, y
cada trozo se indexa como su propio doc.

El problema, con números: el extractor de texto (`texto.py`) lee `extractor_max_chars*4`
bytes y trunca a `extractor_max_chars` (100k). Un dump SQL, un log o un boletín de varios
MB pierde TODO lo posterior a 100k chars: no es buscable. Igual que un CSV grande antes de
`tabla_plana`.

La solución es la misma que para CSV/SQLite: trocear en lotes, cada lote su propio doc.
Aquí el trozo es una VENTANA DE BYTES (el texto ya es direccionable por bytes, no hace
falta parsearlo). Se planifica SIN leer el archivo (aritmética sobre el tamaño). Al SERVIR,
la ventana `[desde, hasta)` se convierte en `[frontera(desde), frontera(hasta))`, y la
frontera de un offset depende SOLO de los bytes cercanos a él: los dos lotes que comparten
un borde calculan el mismo corte, así que cada byte se sirve exactamente una vez (sin
huecos ni solapes) aunque cada lote se sirva en un proceso distinto. La frontera es el
primer inicio de línea a menos de `ALCANCE_FRONTERA`; si una línea larga no deja ninguno
cerca, se corta dentro de la línea sin partir un carácter (ver `_frontera`). En texto de
un byte por carácter se devuelven los bytes TAL CUAL (pass-through): el leaf servido lo
re-extrae `texto.py` sin tocarlo, ya bajo el límite de chars. Un padre UTF-16 se sirve
decodificado a UTF-8 (ver `_utf16_a_utf8`).

Las anclas (CURP/RFC) NO se heredan: se re-detectan por trozo, porque cada lote es una
pasada completa del pipeline y el worker corre `buscar_en_texto` sobre el texto de ESE
lote. Por eso el contenido post-100k pasa de invisible a resoluble a entidad.

La constante de corte es INMUTABLE: cambiarla mueve las fronteras y por tanto el
`ruta_interna`/`archivo_id` (ver `tabla_plana`). `ALCANCE_FRONTERA` no toca los ids, pero
cambiarla mueve lo SERVIDO en los bordes de las líneas largas: re-servir solo una parte de
los lotes de un padre con otro alcance deja líneas repetidas o perdidas en el índice. Lo
mismo al pasar del troceo sin tope a este: `cambia_lo_servido` dice qué padres hay que
re-servir, y hay que re-servirlos ENTEROS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO

from normalizacion.core.observabilidad import obtener_logger
from normalizacion.ingesta.precalificacion.reglas import decodificar_utf16

log = obtener_logger("texto_lotes")

#: Ventana de bytes por lote. ~64 KiB deja cada doc bajo `extractor_max_chars` (100k
#: chars) sin re-truncar, y —al no leer el archivo para planificar— los bytes son la
#: única magnitud barata.
BYTES_POR_LOTE = 64 * 1024
#: Tope de lotes por archivo. Como en `tabla_plana`, solo TRUNCA (marca `topado`), nunca
#: mueve una frontera: re-planificar con un tope mayor es incremental, no duplica.
MAX_LOTES_POR_ARCHIVO = 1_000_000
#: Cuánto se aleja una frontera del borde de su ventana buscando un inicio de línea.
#: Antes la búsqueda no tenía tope, y en 'Matrix.rar' (dumps con INSERT extendidos de
#: hasta 1.046.626 B por línea) eso rompía el troceo por los dos lados: la ventana que
#: caía entera dentro de una línea larga salía VACÍA (106.148 lotes `application/x-empty`
#: a COLD) y el lote donde empezaba la línea la servía ENTERA, ~1 MB que `texto.py`
#: truncaba a 100k chars (≈5,3 GB de SQL que nunca llegaron al índice). Con el tope, un
#: lote mide como mucho `BYTES_POR_LOTE + ALCANCE_FRONTERA + 2` = 98.306 B < 100k chars.
#: Y como el doble del alcance cabe en la ventana, las fronteras de dos bordes vecinos
#: nunca se cruzan.
#: Un texto con líneas normales (< 32 KiB) se sirve byte a byte igual que antes: sus
#: lotes ya indexados no cambian de contenido ni de `hash_contenido`. Pero basta UNA línea
#: más larga que el alcance para que cambien los lotes de sus bordes, aunque ninguna
#: ventana saliera vacía (ver `cambia_lo_servido`).
ALCANCE_FRONTERA = 32 * 1024

#: Bytes de cabecera del PADRE con los que se decide si es UTF-16: la ventana T1
#: (`bytes_t1`), la misma sobre la que decide `reglas`.
_MUESTRA_CODIFICACION = 4096
#: Bytes de cabecera con los que se decide si el UTF-16 pasó por un stream en modo texto:
#: basta que haya fines de línea, y un dump abre con comentarios de líneas cortas.
_MUESTRA_MODO_TEXTO = 64 * 1024
#: Hasta dónde se sigue leyendo si la cabecera no trae dos fines de línea (ver
#: `_modo_texto`). Holgado sobre la línea más larga medida en Matrix (1.046.626 B, el
#: doble en UTF-16), y sale de la caché de páginas: cada lote relee el mismo principio.
_MAX_BUSQUEDA_MODO_TEXTO = 4 * 1024 * 1024
#: Bytes tras un corte en mitad de línea UTF-16 con los que se deduce la paridad local.
_MUESTRA_PARIDAD = 512
#: Holgura del buffer de `servir_lote`: la búsqueda de un fin de línea empieza hasta 3
#: bytes antes de su rango (la marca UTF-16 del modo texto mide 3) y `_corte_en_linea`
#: retrocede hasta 3 bytes desde el borde.
_MARGEN = 4
#: Lectura por paso de `cambia_lo_servido`: un inicio de línea suele estar a pocos bytes
#: del borde, y solo junto a una línea larga hace falta mirar el alcance entero.
_PASO_BUSQUEDA = 4096

_LE = "utf-16-le"
_BE = "utf-16-be"


@dataclass(frozen=True)
class _Utf16:
    codec: str  # `_LE` / `_BE`
    #: El archivo pasó por un stream en modo texto de Windows, que convierte CADA byte
    #: 0x0A en 0D 0A aunque sea medio carácter UTF-16. El dump de Matrix es así: 859.021 de
    #: 859.021 fines de línea salen `0D 00 0D 0A 00`. Esa traducción se invierte EXACTA
    #: quitando el 0x0D que precede a cada 0x0A (un 0D 0A original quedó como 0D 0D 0A).
    modo_texto: bool

    @property
    def lf(self) -> bytes:
        """El salto de línea (U+000A) bien formado."""
        return b"\n\x00" if self.codec == _LE else b"\x00\n"

    @property
    def fin_linea(self) -> bytes:
        """El salto tal como está en el archivo. En modo texto, además, la marca de 3 bytes
        no casa con un 0x0A de un carácter no latino (上 U+4E0A, ਅ U+0A05…)."""
        return self.lf.replace(b"\n", b"\r\n") if self.modo_texto else self.lf


@dataclass(frozen=True)
class LoteTexto:
    desde: int  # offset de byte inicial (inclusive)
    hasta: int  # offset de byte final (exclusive)

    @property
    def ruta_interna(self) -> str:
        """Determinista y auto-descriptiva: el paso reconstruye el rango sin re-planificar.
        Si dependiera del orden de exploración, el `archivo_id` cambiaría entre corridas y
        el disco se duplicaría (ver `tabla_plana`)."""
        return f"texto/{self.desde}-{self.hasta}"


def _parsear(ruta_interna: str) -> LoteTexto:
    _, _, rango = ruta_interna.partition("/")
    desde, _, hasta = rango.partition("-")
    return LoteTexto(int(desde), int(hasta))


class _Fuente:
    """Abstrae Path o file-like seekable (un texto anidado llega como SpooledTemporaryFile,
    que es seekable — no hace falta materializarlo)."""

    def __init__(self, fuente: str | Path | IO[bytes]) -> None:
        if isinstance(fuente, (str, Path)):
            self._f: IO[bytes] = open(fuente, "rb")  # noqa: SIM115
            self._cerrar = True
            self.tamano = os.path.getsize(fuente)
        else:
            self._f = fuente
            self._cerrar = False
            self.tamano = self._f.seek(0, os.SEEK_END)

    def cerrar(self) -> None:
        if self._cerrar:
            self._f.close()


def planificar(
    fuente: str | Path | IO[bytes],
    *,
    bytes_por_lote: int = BYTES_POR_LOTE,
    max_lotes: int | None = None,
) -> list[LoteTexto]:
    """Ventanas de bytes contiguas. NO lee el archivo: el solapamiento se resuelve al
    servir. La última ventana llega hasta el tamaño real."""
    if max_lotes is None:
        max_lotes = MAX_LOTES_POR_ARCHIVO
    src = _Fuente(fuente)
    try:
        tam = src.tamano
    finally:
        src.cerrar()
    lotes: list[LoteTexto] = []
    desde = 0
    while desde < tam and len(lotes) < max_lotes:
        hasta = min(desde + bytes_por_lote, tam)
        lotes.append(LoteTexto(desde, hasta))
        desde = hasta
    return lotes


def _codificacion(f: IO[bytes]) -> _Utf16 | None:
    """El UTF-16 del PADRE, o None si es de un byte por carácter.

    Se decide con la cabecera del padre y no con la del lote: en 'Matrix.rar' un .sql de
    430 MB en UTF-16LE con BOM pasaba T2 entero, pero sus lotes 2..N empiezan por 0x00 y
    sin BOM, y el paso ⑤ de T1 los tomaba por binario ('nulos'): 6.560 de 6.561 a COLD.

    Sin BOM, si es UTF-16 lo decide `reglas.decodificar_utf16` sobre la ventana T1: el
    mismo criterio con el que T1/T2 y `texto.py` leen el padre y sus lotes. Aquí hubo una
    heurística propia con otros umbrales (NUL 40-60 % frente a 30-70 %), y con dos, un
    padre que `reglas` diera por UTF-16 y esta no se serviría en crudo —sus lotes volverían
    a caer por 'nulos'— y uno al revés se «decodificaría» a basura. En la caché de Matrix
    (1.135 entradas y 336 miembros de zip) las dos dan exactamente lo mismo.
    """
    f.seek(0)
    muestra = f.read(_MUESTRA_CODIFICACION)
    codec = {b"\xff\xfe": _LE, b"\xfe\xff": _BE}.get(muestra[:2])
    if codec is None and decodificar_utf16(muestra) is None:
        return None  # el caso de casi todos los textos: solo cuesta leer 4 KB por lote
    modo_texto = _modo_texto(f, muestra)
    if codec is None:
        # `reglas` da LE a todo UTF-16 sin BOM (el que llega es de Windows); el orden de
        # bytes lo dice la lectura que da texto: el latino leído al revés sale CJK. Mirar
        # solo en qué lado caen los NUL de la 1.ª línea fallaba con un emoji (el sustituto
        # bajo U+DE00 lleva el 0x00 en el byte BAJO): LE leído como BE.
        if modo_texto:
            muestra = muestra.replace(b"\r\n", b"\n")
        muestra = muestra[: len(muestra) // 2 * 2]
        codec = _LE if _plausibles(muestra, _LE) >= _plausibles(muestra, _BE) else _BE
    return _Utf16(codec, modo_texto)


def _modo_texto(f: IO[bytes], muestra: bytes) -> bool:
    """¿Pasó el UTF-16 por un stream en modo texto (ver `_Utf16.modo_texto`)? Bien formado,
    un 0x0A va tras 0x00 (`0D 00 0A 00`); en modo texto, SIEMPRE tras 0x0D.

    Hace falta ver algún 0x0A, y una primera línea más larga que la cabecera (un INSERT
    extendido) no deja ver ninguno: se sigue leyendo hasta ver dos. Antes se decidía solo
    con 64 KB y se exigían dos saltos, y un UTF-16BE en modo texto que abría con una línea
    así no se detectaba: su salto `00 0D 0A` no contiene el `00 0A` bien formado, ningún
    corte caía en fin de línea y la alineación se invertía en cada salto (en la revisión,
    con líneas de 330 k caracteres, salían mal 330.011 de 660.012). En LE no se notaba
    porque `0A 00` es sufijo de la marca `0D 0A 00`."""
    cab = muestra + f.read(_MUESTRA_MODO_TEXTO - len(muestra))
    saltos, crlf = cab.count(b"\n"), cab.count(b"\r\n")
    leidos, ultimo = len(cab), cab[-1:]
    while saltos < 2 and leidos < _MAX_BUSQUEDA_MODO_TEXTO:
        bloque = f.read(_MUESTRA_MODO_TEXTO)
        if not bloque:
            break
        saltos += bloque.count(b"\n")
        crlf += (ultimo + bloque).count(b"\r\n")  # un `0D 0A` partido entre dos lecturas
        leidos, ultimo = leidos + len(bloque), bloque[-1:]
    return saltos >= 1 and crlf >= 0.9 * saltos


def _frontera(buf: bytes, base: int, x: int, tam: int, cod: _Utf16 | None) -> int:
    """El corte que corresponde al borde de ventana `x`. Solo lee `buf` en
    `[x - 3, x + ALCANCE_FRONTERA)` y, con la regla 2, desde `x - ALCANCE_FRONTERA - 2`
    (`buf` empieza en el offset `base`): el lote que acaba en `x` y el que empieza en `x`
    obtienen el mismo corte.

    1. El PRIMER inicio de línea en `[x, x + ALCANCE_FRONTERA)`. Es la regla de siempre
       —saltar la línea parcial, terminar la que cruza el borde— con tope: un texto de
       líneas normales corta donde cortaba, y lo ya indexado no cambia.
    2. Si esa búsqueda llega al EOF sin encontrarlo, la última línea del archivo cruza `x`:
       se corta en SU inicio, si está a menos del alcance hacia atrás. El EOF no cuenta
       como inicio de línea porque dejaría VACÍO el último lote siempre que la línea
       final cubra su ventana (lo que pasa con cada dump de líneas largas).
    3. Si no, `x` está en mitad de una línea larga: se corta ahí, retrocediendo hasta 3
       bytes para no partir un carácter (`_corte_en_linea`).
    """
    if x <= 0:
        return 0
    if x >= tam:
        return tam
    fin = b"\n" if cod is None else cod.fin_linea
    n = len(fin)
    tope = min(x + ALCANCE_FRONTERA, tam)
    i = buf.find(fin, max(x - n - base, 0), tope - 1 - base)
    if i >= 0:
        return base + i + n
    if tope == tam:
        i = buf.rfind(fin, max(x - ALCANCE_FRONTERA + 1 - n - base, 0), x - 1 - base)
        if i >= 0:
            return base + i + n
    return _corte_en_linea(buf, base, x, tam, cod)


def _corte_en_linea(buf: bytes, base: int, x: int, tam: int, cod: _Utf16 | None) -> int:
    """Corte dentro de una línea larga, en un límite de carácter en `[x - 3, x]`."""
    p = x
    if cod is None:
        # UTF-8: no empezar el lote por un byte de continuación (10xxxxxx). En latin-1
        # retroceder sobre bytes 0x80-0xBF no rompe nada: el corte sigue en la línea.
        while p > max(x - 3, 1) and 0x80 <= buf[p - base] < 0xC0:
            p -= 1
        return p
    # UTF-16: la unidad de 2 bytes empieza en una paridad que puede no ser la del archivo
    # (en modo texto, cada 0x0D insertado la invierte); se deduce de los bytes justo
    # después de `x`, que están en la misma línea (no hubo fin de línea en el alcance).
    muestra = buf[x - base : min(x + _MUESTRA_PARIDAD, tam) - base]
    if cod.modo_texto:
        muestra = muestra.replace(b"\r\n", b"\n")
    p -= _desfase_utf16(muestra, cod.codec)
    if cod.modo_texto and buf[p - 1 - base : p + 1 - base] == b"\r\n":
        p -= 1  # no separar el 0x0D insertado de su 0x0A: lo quita el lote que los tenga
    alto = p + 1 if cod.codec == _LE else p
    if p >= 3 and alto < tam and 0xDC <= buf[alto - base] <= 0xDF:
        p -= 2  # sustituto bajo: no separarlo del alto que lo precede
    return p if p >= 1 else x


def _plausibles(unidades: bytes, codec: str) -> int:
    """Caracteres plausibles de `unidades` leídas con `codec`: ASCII (el texto latino
    desalineado o al revés sale CJK) y pares sustitutos válidos (un emoji desalineado sale
    'Ø' + CJK), menos los U+FFFD. Contar solo NUL fallaría con los emojis."""
    texto = unidades.decode(codec, "replace")
    astrales = len(unidades) // 2 - len(texto)  # un par sustituto = 1 carácter
    ascii_ = len(texto.encode("ascii", "ignore")) - texto.count("\x00")
    return ascii_ + astrales - texto.count("\N{REPLACEMENT CHARACTER}")


def _desfase_utf16(datos: bytes, codec: str) -> int:
    """0 si las unidades de `datos` empiezan en su byte 0, 1 si en el 1: la alineación con
    más caracteres plausibles. Si empatan (texto CJK, cirílico) se queda la de partida.

    Límite conocido: en un corte en mitad de línea de un texto NO latino en modo texto, la
    de partida es una moneda al aire, porque cada 0x0D insertado antes del corte invierte
    la paridad; si falla, el lote sale ilegible hasta el siguiente fin de línea. En la
    revisión, con CJK puro y líneas de 120 k caracteres, salieron mal el 85 % de los
    caracteres. Arreglarlo exige contar los 0x0D insertados desde el inicio de la línea,
    leyendo hacia atrás hasta él. En Matrix no pasa: su UTF-16 es un 99,955 % ASCII."""
    puntos = [
        _plausibles(datos[d : d + (len(datos) - d) // 2 * 2], codec) for d in (0, 1)
    ]
    return int(puntos[1] > puntos[0])


def _utf16_a_utf8(trozo: bytes, cod: _Utf16, *, al_inicio: bool) -> bytes:
    """El lote UTF-16 como UTF-8, sin BOM ni NUL: así T1/T2 lo leen como el texto que es.

    Primero se deshace el modo texto (ver `_Utf16.modo_texto`): el 0x0D insertado deja la
    línea con un número impar de bytes e invierte la alineación de lo que sigue, y
    decodificar el lote de corrido saca basura desde la primera línea. Después se
    decodifica línea a línea, eligiendo la alineación de cada una (`_decodificar_linea`).

    Los U+0000 se quitan: en UTF-8 el NUL devolvería el lote al paso ⑤ de T1 ('nulos').
    Salvo si no queda nada más: un lote que cae entero en una región de relleno se sirve
    como sus NUL, que es lo que es, y no vacío, que se lee como un fallo del troceo."""
    if al_inicio and trozo[:2] in (b"\xff\xfe", b"\xfe\xff"):
        trozo = trozo[2:]
    if cod.modo_texto:
        trozo = trozo.replace(b"\r\n", b"\n")
    lf = cod.lf
    partes: list[str] = []
    pos = 0
    while True:
        i = trozo.find(lf, pos)
        if i < 0:
            partes.append(_decodificar_linea(trozo[pos:], cod.codec))
            break
        partes.append(_decodificar_linea(trozo[pos:i], cod.codec))
        partes.append("\n")
        pos = i + len(lf)
    texto = "".join(partes)
    return (texto.replace("\x00", "") or texto).encode("utf-8", "replace")


def _decodificar_linea(linea: bytes, codec: str) -> str:
    """Una línea sin su fin. Normalmente llega alineada; elegir la alineación
    (`_desfase_utf16`) cubre un fin de línea falso —un carácter U+0Axx junto a uno U+xx00
    forma los bytes de U+000A a caballo de dos unidades— que la desalinearía."""
    desfase = _desfase_utf16(linea, codec)
    unidades = linea[desfase : desfase + (len(linea) - desfase) // 2 * 2]
    return unidades.decode(codec, "replace")


def servir_lote(
    fuente: str | Path | IO[bytes],
    ruta_interna: str,
    *,
    umbral_memoria: int,
    limite_bytes: int,
) -> IO[bytes]:
    """El trozo `[frontera(desde), frontera(hasta))` (ver `_frontera`): nunca vacío si su
    ventana tiene contenido, y la concatenación de todos los lotes reproduce el archivo.

    En texto de un byte por carácter es pass-through de bytes y mide como mucho 98.306 B.
    Un padre UTF-16 se sirve en UTF-8 (`_utf16_a_utf8`): esos mismos ≤ 98.306 B de origen
    son ≤ ~49 k caracteres, que en UTF-8 llegan a ~147 KB si son de 3 bytes (CJK); no
    importa, porque `texto.py` trunca por caracteres (100k), no por bytes."""
    lote = _parsear(ruta_interna)
    src = _Fuente(fuente)
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral_memoria)  # noqa: SIM115
    try:
        f = src._f
        tam = src.tamano
        desde, hasta = min(lote.desde, tam), min(lote.hasta, tam)
        cod = _codificacion(f)
        # Un solo read con lo que miran las dos fronteras y el trozo (~98 KB). Hacia atrás
        # del borde solo busca la regla 2 de `_frontera`, que solo aplica cerca del EOF: en
        # el resto de lotes, leer el alcance entero hacia atrás era 1/4 de la lectura.
        atras = ALCANCE_FRONTERA if hasta + ALCANCE_FRONTERA >= tam else 0
        base = max(0, desde - atras - _MARGEN)
        f.seek(base)
        buf = f.read(min(tam, hasta + ALCANCE_FRONTERA + _MARGEN) - base)
        ini = _frontera(buf, base, desde, tam, cod)
        fin = min(_frontera(buf, base, hasta, tam, cod), ini + limite_bytes)
        trozo = buf[ini - base : fin - base] if fin > ini else b""
        if cod is not None:
            trozo = _utf16_a_utf8(trozo, cod, al_inicio=ini == 0)
        spool.write(trozo)
    finally:
        src.cerrar()
    spool.seek(0)
    return spool


def cambia_lo_servido(
    fuente: str | Path | IO[bytes],
    *,
    bytes_por_lote: int = BYTES_POR_LOTE,
    max_lotes: int | None = None,
) -> bool:
    """¿Sirve este padre algún lote distinto que el troceo anterior, el de frontera sin
    tope? Es el criterio para elegir qué padres re-servir, y de cada uno hay que re-servir
    TODOS sus lotes, COLD e INDEXADO: mezclar lotes viejos y nuevos de un mismo padre pierde
    o repite el tramo entre la frontera vieja y la nueva, que puede ser casi toda una línea
    larga (hasta ~90 KB en esos 8 padres, ~1 MB en los dumps).

    «Padres con lotes vacíos o UTF-16» no basta. En Matrix cambian esos 49 y, además, 39
    lotes de otros 8 padres con líneas de 48-90 KB: más largas que el alcance y más cortas
    que la ventana, así que nunca dejaron una ventana vacía. 31 de esos lotes ya estaban
    INDEXADO, y 14 medían más de 100 KB: están truncados en el índice.

    La frontera vieja de un borde `x` era el primer inicio de línea desde `x`, sin tope; la
    nueva es la misma si y solo si hay uno antes de `x + ALCANCE_FRONTERA` y del EOF (que
    ya no cuenta). Un padre UTF-16 cambia siempre: antes se servía en crudo. Lee unos KB
    por borde, no el archivo. `max_lotes` tiene que ser el de la planificación del padre
    (`t3_entradas_max`): un padre topado tiene un borde más."""
    lotes = planificar(fuente, bytes_por_lote=bytes_por_lote, max_lotes=max_lotes)
    src = _Fuente(fuente)
    try:
        f, tam = src._f, src.tamano
        if _codificacion(f) is not None:
            return True
        return any(
            not _hay_inicio_de_linea(f, lt.hasta, min(lt.hasta + ALCANCE_FRONTERA, tam))
            for lt in lotes
            if lt.hasta < tam
        )
    finally:
        src.cerrar()


def _hay_inicio_de_linea(f: IO[bytes], x: int, tope: int) -> bool:
    """¿Empieza alguna línea en `[x, tope)`? Es decir, ¿hay un salto en `[x - 1, tope - 1)`?"""
    pos = x - 1
    f.seek(pos)
    while pos < tope - 1:
        bloque = f.read(min(_PASO_BUSQUEDA, tope - 1 - pos))
        if not bloque:
            return False
        if b"\n" in bloque:
            return True
        pos += len(bloque)
    return False


def explorar(
    perillas, fuente: str | Path | IO[bytes], mtime_ns: int
) -> tuple[list[tuple[str, str, int, int]], str | None, bool]:
    """Entradas `(ruta_interna, nombre, tamano, mtime_ns)`, motivo si no se pudo, y si el
    archivo quedó PARCIAL (se alcanzó el tope de lotes). Espejo de `tabla_plana.explorar`."""
    try:
        lotes = planificar(fuente, max_lotes=perillas.t3_entradas_max)
    except Exception as exc:  # archivo hostil / ilegible
        log.warning("texto_no_explorable", error=str(exc)[:150])
        return [], "contenedor_corrupto", False
    if not lotes:
        return [], None, False
    topado = len(lotes) >= perillas.t3_entradas_max
    if topado:
        log.warning("texto_parcial", lotes=len(lotes))
    entradas = [
        (lt.ruta_interna, f"parte-{lt.desde}.txt", lt.hasta - lt.desde, mtime_ns) for lt in lotes
    ]
    return entradas, None, topado
