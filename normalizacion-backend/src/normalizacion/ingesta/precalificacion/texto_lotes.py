"""Un archivo de TEXTO grande (text/plain, SQL, XML, rfc822, JSON) tratado como
CONTENEDOR de trozos: cada ventana de bytes es una entrada, servida como texto crudo, y
cada trozo se indexa como su propio doc.

El problema, con números: el extractor de texto (`texto.py`) lee `extractor_max_chars*4`
bytes y trunca a `extractor_max_chars` (100k). Un dump SQL, un log o un boletín de varios
MB pierde TODO lo posterior a 100k chars: no es buscable. Igual que un CSV grande antes de
`tabla_plana`.

La solución es la misma que para CSV/SQLite: trocear en lotes, cada lote su propio doc.
Aquí el trozo es una VENTANA DE BYTES (el texto ya es direccionable por bytes, no hace
falta parsearlo). Se planifica SIN leer el archivo (aritmética sobre el tamaño); al SERVIR
se alinea a línea con la regla de solapamiento —saltar la primera línea parcial (es del
lote anterior), terminar la que cruza el borde— y se devuelven los bytes TAL CUAL
(pass-through, sin re-serializar): el leaf servido es texto y lo re-extrae `texto.py` sin
tocarlo, ya bajo el límite de chars.

Las anclas (CURP/RFC) NO se heredan: se re-detectan por trozo, porque cada lote es una
pasada completa del pipeline y el worker corre `buscar_en_texto` sobre el texto de ESE
lote. Por eso el contenido post-100k pasa de invisible a resoluble a entidad.

La constante de corte es INMUTABLE: cambiarla mueve las fronteras y por tanto el
`ruta_interna`/`archivo_id` (ver `tabla_plana`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO

from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("texto_lotes")

#: Ventana de bytes por lote. ~64 KiB deja cada doc holgadamente bajo `extractor_max_chars`
#: (100k chars) sin re-truncar, y —al no leer el archivo para planificar— los bytes son la
#: única magnitud barata.
BYTES_POR_LOTE = 64 * 1024
#: Tope de lotes por archivo. Como en `tabla_plana`, solo TRUNCA (marca `topado`), nunca
#: mueve una frontera: re-planificar con un tope mayor es incremental, no duplica.
MAX_LOTES_POR_ARCHIVO = 1_000_000


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


def servir_lote(
    fuente: str | Path | IO[bytes],
    ruta_interna: str,
    *,
    umbral_memoria: int,
    limite_bytes: int,
) -> IO[bytes]:
    """El trozo de texto CRUDO alineado a línea. Regla de solapamiento: si `desde>0` y cae
    a mitad de línea, se descarta esa primera línea parcial (la sirvió el lote anterior al
    cruzar su borde); se leen líneas hasta pasar `hasta`, terminando la que cruza. Cada
    línea se sirve EXACTAMENTE una vez. Pass-through de bytes: no se re-serializa nada."""
    lote = _parsear(ruta_interna)
    src = _Fuente(fuente)
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral_memoria)  # noqa: SIM115
    try:
        f = src._f
        # Saltar la primera línea SOLO si `desde` cae a mitad de línea (byte previo != \n):
        # esa línea la sirvió el lote anterior; si `desde` cae en inicio de línea, NO se
        # salta (perderla si no).
        saltar = False
        if lote.desde > 0:
            f.seek(lote.desde - 1)
            saltar = f.read(1) != b"\n"
        f.seek(lote.desde)
        if saltar:
            f.readline()
        escritos = 0
        pos = f.tell()
        while pos < lote.hasta:
            linea = f.readline()
            if not linea:
                break
            if escritos + len(linea) > limite_bytes:
                break  # tope duro: un lote no puede crecer sin límite
            spool.write(linea)
            escritos += len(linea)
            pos = f.tell()
    finally:
        src.cerrar()
    spool.seek(0)
    return spool


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
