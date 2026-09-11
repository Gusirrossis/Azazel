"""Un archivo tabular PLANO (CSV/NDJSON) tratado como CONTENEDOR: cada lote de filas,
una entrada — como `tabla_lotes` hace con SQLite.

El problema, con números del corpus: el extractor tabular lee solo `calidad_max_bytes`
(10 MB) y trunca el texto a `extractor_max_chars` (100k chars). Un CSV de 100 MB pierde
todo tras 10 MB (36.657 CSV medidos así); incluso uno chico pierde filas tras 100k chars.
La solución es la de SQLite: **explotar el archivo en lotes**, cada lote su propio doc.

El lote se sirve como **NDJSON**, que ya procesa el plugin tabular (perfil de calidad +
texto por identidad). Ni extractor nuevo ni camino paralelo.

**Cómo se trocea, y por qué difiere de SQLite** (que usa rangos de rowid = un índice):

- **NDJSON**: cada `\n` crudo es frontera de registro —los saltos dentro de strings JSON
  van escapados como `\\n`—, así que se planifica SIN leer el archivo: ventanas de bytes
  fijas. Al SERVIR se alinea a línea con la regla de solapamiento (saltar la primera línea
  parcial —pertenece al lote anterior— y terminar la que cruza el borde). Cada línea se
  sirve EXACTAMENTE una vez, sin pre-escaneo.
- **CSV**: un `\n` DENTRO de un campo entrecomillado NO es fin de registro. Un corte por
  bytes crudo partiría el registro en dos filas basura —corrupción, peor que truncar—, y
  no se puede resincronizar localmente (hay que conocer la paridad de comillas desde el
  byte 0). Hace falta UNA pasada que rastree las comillas (sin parsear campos) y ponga una
  frontera cada N registros: O(N) tiempo, O(1) memoria. Las fronteras caen en fin de
  registro exacto, así que cada lote son registros COMPLETOS.

Las constantes de corte son INMUTABLES: cambiarlas mueve las fronteras y por tanto el
`ruta_interna`/`archivo_id`, y el disco se re-cataloga entero (ver `tabla_lotes`).
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO

from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("tabla_plana")

#: Registros por lote (CSV). Igual criterio que SQLite (500): un doc de decenas de KB.
REGISTROS_POR_LOTE_CSV = 500
#: Ventana de bytes por lote (NDJSON). ~64 KiB deja el volcado de texto holgadamente bajo
#: `extractor_max_chars` (100k) y, al no leer el archivo para planificar, los bytes son la
#: única magnitud barata.
BYTES_POR_LOTE_NDJSON = 64 * 1024
#: Tope de lotes por archivo. Como en SQLite, solo TRUNCA la lista (marca `topado`), nunca
#: mueve una frontera: re-planificar con un tope mayor es incremental, no duplica.
MAX_LOTES_POR_ARCHIVO = 1_000_000

_BLOQUE = 1024 * 1024
_NL = 0x0A  # '\n'


@dataclass(frozen=True)
class LotePlano:
    formato: str  # "csv" | "ndjson"
    desde: int  # offset de byte inicial (inclusive)
    hasta: int  # offset de byte final (exclusive)

    @property
    def ruta_interna(self) -> str:
        """Determinista y auto-descriptiva: el paso reconstruye el rango sin re-planificar.
        Si dependiera del orden de exploración, el `archivo_id` cambiaría entre corridas y
        el disco se duplicaría entero (ver `tabla_lotes.Lote.ruta_interna`)."""
        return f"{self.formato}/{self.desde}-{self.hasta}"


def _parsear(ruta_interna: str) -> LotePlano:
    formato, _, rango = ruta_interna.partition("/")
    desde, _, hasta = rango.partition("-")
    return LotePlano(formato, int(desde), int(hasta))


def _decodificar(datos: bytes) -> str:
    """Tolerante al encoding (igual criterio que el extractor de texto): un CSV en
    Latin-1/CP1252 no puede perder los acentos por adivinar UTF-8."""
    import codecs

    try:
        return codecs.getincrementaldecoder("utf-8-sig")().decode(datos, final=True)
    except UnicodeDecodeError:
        pass
    for enc in ("cp1252", "latin-1"):
        try:
            return datos.decode(enc)
        except UnicodeDecodeError:
            continue
    return datos.decode("latin-1", errors="replace")


class _Fuente:
    """Abstrae Path o file-like seekable (un CSV anidado llega como SpooledTemporaryFile,
    que es seekable — no hace falta materializarlo a disco como con SQLite)."""

    def __init__(self, fuente: str | Path | IO[bytes]) -> None:
        if isinstance(fuente, (str, Path)):
            self._f: IO[bytes] = open(fuente, "rb")  # noqa: SIM115
            self._cerrar = True
            self.tamano = os.path.getsize(fuente)
        else:
            self._f = fuente
            self._cerrar = False
            self.tamano = self._f.seek(0, os.SEEK_END)

    def leer(self, desde: int, n: int) -> bytes:
        self._f.seek(desde)
        return self._f.read(n)

    def bloques(self):
        self._f.seek(0)
        while bloque := self._f.read(_BLOQUE):
            yield bloque

    def cerrar(self) -> None:
        if self._cerrar:
            self._f.close()


# ------------------------------------------------------------------ NDJSON


def planificar_ndjson(
    fuente: str | Path | IO[bytes],
    *,
    bytes_por_lote: int = BYTES_POR_LOTE_NDJSON,
    max_lotes: int | None = None,
) -> list[LotePlano]:
    """Ventanas de bytes contiguas. NO lee el archivo: el solapamiento se resuelve al
    servir. La última ventana llega hasta el tamaño real."""
    if max_lotes is None:
        max_lotes = MAX_LOTES_POR_ARCHIVO
    src = _Fuente(fuente)
    try:
        tam = src.tamano
    finally:
        src.cerrar()
    lotes: list[LotePlano] = []
    desde = 0
    while desde < tam and len(lotes) < max_lotes:
        hasta = min(desde + bytes_por_lote, tam)
        lotes.append(LotePlano("ndjson", desde, hasta))
        desde = hasta
    return lotes


def _servir_ndjson(src: _Fuente, lote: LotePlano, *, limite_bytes: int) -> IO[bytes]:
    """Regla de solapamiento: si `desde>0`, se descarta la primera línea parcial (es del
    lote anterior); se emiten líneas hasta pasar `hasta`, terminando la que cruza el borde.
    Los bytes son ya JSON válido → pass-through, cero re-serialización."""
    spool: IO[bytes] = SpooledTemporaryFile(max_size=limite_bytes)  # noqa: SIM115,forma como tabla_lotes
    # Saltar la primera línea SOLO si `desde` cae a mitad de línea (el byte previo no es
    # '\n'): esa línea la sirvió el lote anterior al cruzar su borde. Si `desde` cae justo
    # en un inicio de línea, NO se salta —el lote anterior terminó antes y esa línea es de
    # este lote—; saltarla la perdería.
    saltar = False
    if lote.desde > 0:
        src._f.seek(lote.desde - 1)
        saltar = src._f.read(1) != b"\n"
    src._f.seek(lote.desde)
    if saltar:
        _saltar_linea(src._f)
    escritos = 0
    pos = src._f.tell()
    while pos < lote.hasta:
        linea = src._f.readline()
        if not linea:
            break
        if not linea.endswith(b"\n"):
            linea += b"\n"
        if escritos + len(linea) > limite_bytes:
            break
        if linea.strip():  # no encolar líneas en blanco
            spool.write(linea)
            escritos += len(linea)
        pos = src._f.tell()
    # terminar la línea que cruza `hasta`: readline ya la trajo entera si `pos` la cruzó.
    spool.seek(0)
    return spool


def _saltar_linea(f: IO[bytes]) -> None:
    f.readline()


# ------------------------------------------------------------------ CSV


def _dialecto(cabeza: bytes) -> tuple[str, str]:
    """Delimitador y comilla, sniffeados sobre la cabecera (determinista sobre los mismos
    bytes). Fallback conservador a coma/comilla-doble."""
    try:
        muestra = _decodificar(cabeza[:8192])
        d = csv.Sniffer().sniff(muestra, delimiters=",;\t|")
        return d.delimiter, d.quotechar or '"'
    except Exception:
        return ",", '"'


def _fronteras_csv(src: _Fuente, comilla: int, registros_por_lote: int, max_lotes: int):
    """UNA pasada quote-aware. Devuelve (fin_cabecera, [offsets de fin de registro de datos
    cada N registros, incluido el EOF]).

    Máquina de estados a nivel de BYTE (no parsea campos): dentro de comillas, un `\\n` no
    es fin de registro; `""` es una comilla escapada (sigue dentro). El primer registro es
    la cabecera y no cuenta como dato.
    """
    en_comillas = False
    posible_cierre = False  # vimos una comilla dentro de comillas: escape o cierre según el siguiente byte
    offset = 0
    registros_datos = 0
    fin_cabecera: int | None = None
    fronteras: list[int] = []

    for bloque in src.bloques():
        for b in bloque:
            offset += 1  # offset = posición DESPUÉS de este byte
            if posible_cierre:
                posible_cierre = False
                if b == comilla:
                    continue  # "" escapada: seguimos dentro de comillas, byte consumido
                en_comillas = False  # la comilla previa era el CIERRE; procesar b normal
            if b == comilla:
                if en_comillas:
                    posible_cierre = True
                else:
                    en_comillas = True
            elif b == _NL and not en_comillas:
                if fin_cabecera is None:
                    fin_cabecera = offset  # fin de la cabecera
                else:
                    registros_datos += 1
                    if registros_datos % registros_por_lote == 0:
                        fronteras.append(offset)
                        if len(fronteras) >= max_lotes:
                            return fin_cabecera or offset, fronteras
    # cola: un último registro sin '\n' final (o datos tras la última frontera)
    if fin_cabecera is None:
        fin_cabecera = offset  # archivo de una sola línea (solo cabecera)
    if (not fronteras or fronteras[-1] < offset) and offset > fin_cabecera:
        fronteras.append(offset)
    return fin_cabecera, fronteras


def planificar_csv(
    fuente: str | Path | IO[bytes],
    *,
    registros_por_lote: int = REGISTROS_POR_LOTE_CSV,
    max_lotes: int | None = None,
) -> list[LotePlano]:
    """Una pasada quote-aware: lotes de registros COMPLETOS sobre la región de datos (la
    cabecera se readjunta al servir, no va en los rangos)."""
    if max_lotes is None:
        max_lotes = MAX_LOTES_POR_ARCHIVO
    src = _Fuente(fuente)
    try:
        cabeza = src.leer(0, 8192)
        _, comilla = _dialecto(cabeza)
        fin_cab, fronteras = _fronteras_csv(src, ord(comilla), registros_por_lote, max_lotes)
    finally:
        src.cerrar()
    lotes: list[LotePlano] = []
    desde = fin_cab
    for hasta in fronteras:
        if hasta > desde:
            lotes.append(LotePlano("csv", desde, hasta))
            desde = hasta
    return lotes


def _servir_csv(src: _Fuente, lote: LotePlano, *, limite_bytes: int) -> IO[bytes]:
    """Lee el rango (registros completos) + readjunta la cabecera, parsea con el dialecto
    sniffeado y emite un objeto JSON por registro (las claves son las cabeceras)."""
    cabeza = src.leer(0, 8192)
    delim, comilla = _dialecto(cabeza)
    # cabecera = primer registro completo (quote-aware, barato)
    fin_cab, _ = _fronteras_csv(src, ord(comilla), REGISTROS_POR_LOTE_CSV, 1)
    cabecera_bytes = src.leer(0, fin_cab)
    datos_bytes = src.leer(lote.desde, lote.hasta - lote.desde)
    texto = _decodificar(cabecera_bytes) + _decodificar(datos_bytes)
    lector = csv.reader(io.StringIO(texto), delimiter=delim, quotechar=comilla)
    spool: IO[bytes] = SpooledTemporaryFile(max_size=limite_bytes)  # noqa: SIM115
    escritos = 0
    columnas: list[str] | None = None
    for fila in lector:
        if columnas is None:
            columnas = [c or f"col{i}" for i, c in enumerate(fila)]
            continue
        registro = {
            columnas[i] if i < len(columnas) else f"col{i}": v for i, v in enumerate(fila)
        }
        linea = (json.dumps(registro, ensure_ascii=False) + "\n").encode("utf-8")
        if escritos + len(linea) > limite_bytes:
            break
        spool.write(linea)
        escritos += len(linea)
    spool.seek(0)
    return spool


# ------------------------------------------------------------------ API pública


def servir_lote(
    fuente: str | Path | IO[bytes],
    ruta_interna: str,
    *,
    umbral_memoria: int,
    limite_bytes: int,
) -> IO[bytes]:
    """El lote como NDJSON, listo para el plugin tabular. Misma firma que
    `tabla_lotes.servir_lote`."""
    lote = _parsear(ruta_interna)
    src = _Fuente(fuente)
    try:
        if lote.formato == "ndjson":
            return _servir_ndjson(src, lote, limite_bytes=limite_bytes)
        return _servir_csv(src, lote, limite_bytes=limite_bytes)
    finally:
        src.cerrar()


def explorar(
    perillas, fuente: str | Path | IO[bytes], formato: str, mtime_ns: int
) -> tuple[list[tuple[str, str, int, int]], str | None, bool]:
    """Entradas `(ruta_interna, nombre, tamano, mtime_ns)`, motivo si no se pudo, y si el
    archivo quedó PARCIAL (se alcanzó el tope de lotes). Espejo de `tabla_lotes.explorar`."""
    try:
        if formato == "ndjson":
            lotes = planificar_ndjson(fuente, max_lotes=perillas.t3_entradas_max)
        else:
            lotes = planificar_csv(fuente, max_lotes=perillas.t3_entradas_max)
    except Exception as exc:  # archivo hostil / ilegible como tabla plana
        log.warning("tabla_plana_no_explorable", formato=formato, error=str(exc)[:150])
        return [], "contenedor_corrupto", False
    if not lotes:
        return [], None, False
    topado = len(lotes) >= perillas.t3_entradas_max
    if topado:
        log.warning("tabla_plana_parcial", formato=formato, lotes=len(lotes))
    entradas = [
        (lt.ruta_interna, f"{formato}-{lt.desde}.ndjson", lt.hasta - lt.desde, mtime_ns)
        for lt in lotes
    ]
    return entradas, None, topado
