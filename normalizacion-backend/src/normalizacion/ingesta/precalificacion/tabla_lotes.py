"""Una base SQLite tratada como CONTENEDOR: cada lote de filas, una entrada.

El problema que resuelve, con números del corpus real: una base tuya de 346.748 filas
extraída como documento único produce ~200 filas de texto (el 0,06 %), porque el índice
guarda un doc por archivo con el texto topado a `extractor_max_chars`. Todo lo demás es
invisible para la búsqueda: la persona de la fila 200.000 no existe para nadie.

La solución no es subir el tope —eso solo mueve el problema y engorda cada documento—
sino la que Azazel ya usa con los ZIP: **explotar el contenedor en entradas**. Aquí cada
entrada es un LOTE de filas, la cola las procesa por BFS como cualquier otra entrada
interna, y cada lote acaba siendo su propio documento buscable. De 200 filas indexadas
se pasa al 100 % de la tabla.

El lote se sirve como **NDJSON**, no como texto suelto: así lo recoge el plugin tabular
que ya existe, con su perfil de calidad y su orden por identidad. Ni un extractor nuevo
ni un camino paralelo.

**Cómo se trocea, y por qué importa.** Con `LIMIT n OFFSET m`, SQLite recorre y descarta
las m primeras filas: el último lote de una tabla de 10 M hace un scan completo, y el
coste total es cuadrático. Cuando la tabla tiene `rowid` —casi todas— se trocea por
RANGOS de rowid, que es una búsqueda por índice: `WHERE rowid BETWEEN a AND b`. Los
rangos pueden venir con huecos (filas borradas) y entonces el lote sale más corto; eso
es correcto, solo significa que ese trozo tenía menos filas.

`OFFSET` queda como respaldo para las tablas `WITHOUT ROWID`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO

from normalizacion.core.config import PerillasFiltro
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("tabla_lotes")

#: Filas por lote. 500 produce un documento de decenas de KB: suficiente para que el
#: texto sea útil y bastante pequeño para que el índice no se llene de documentos
#: gigantes que hay que traer enteros en cada acierto.
FILAS_POR_LOTE = 500

#: Tope de lotes por BASE. Sólo es el valor por omisión de las llamadas directas: en el
#: pipeline manda `PerillasFiltro.t3_sqlite_lotes_max`.
#:
#: Era 20 000 —10 M de filas— y eso NO era un detalle de implementación: medido en la
#: luna de Lilith, dejaba fuera 276.606.467 filas (el 58 % del corpus) mientras la base
#: seguía figurando como HECHO. Un tope que corta datos tiene que verse en la fila, no
#: sólo en un log que desaparece al recrear el contenedor — por eso `explorar` devuelve
#: ahora si topó, y el precalificador lo escribe en las señales.
MAX_LOTES_POR_BASE = 1_000_000

_IGNORADAS = ("sqlite_", "_litestream")


@dataclass(frozen=True)
class Lote:
    tabla: str
    modo: str  # "rowid" | "offset"
    desde: int
    hasta: int

    @property
    def ruta_interna(self) -> str:
        """Determinista y auto-descriptiva: el paso reconstruye la consulta sin
        recalcular la división. Si el nombre dependiera del orden de exploración, el
        `archivo_id` cambiaría entre corridas y el disco se duplicaría entero."""
        return f"{self.tabla}/{self.modo}-{self.desde}-{self.hasta}"


def _abrir(ruta: str | Path) -> sqlite3.Connection:
    """Solo lectura y sin tocar el WAL. Ver el módulo `extractores/sqlite.py`."""
    texto = str(ruta).replace("?", "%3f").replace("#", "%23")
    con = sqlite3.connect(f"file:{texto}?mode=ro&immutable=1", uri=True, timeout=5.0)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _tablas(con: sqlite3.Connection) -> list[str]:
    filas = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    return [n for (n,) in filas if not n.lower().startswith(_IGNORADAS)]


def _cita(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _tiene_rowid(con: sqlite3.Connection, tabla: str) -> bool:
    try:
        con.execute(f"SELECT rowid FROM {_cita(tabla)} LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False  # WITHOUT ROWID, o una vista


def planificar(
    ruta: str | Path,
    *,
    filas_por_lote: int = FILAS_POR_LOTE,
    max_lotes: int | None = None,
    tablas_omitidas: list[str] | None = None,
) -> list[Lote]:
    """Los lotes en que se trocea la base. No lee ni una fila de datos.

    `max_lotes` sólo TRUNCA la lista: el `paso` se calcula del rango de rowid y de
    `filas_por_lote`, nunca del tope. Por eso subirlo añade la cola que faltaba sin
    mover un solo lote existente — mismos límites, mismo `ruta_interna`, mismo
    `archivo_id`. Re-planificar con un tope mayor es incremental, no duplica nada.

    `None` resuelve a `MAX_LOTES_POR_BASE` AQUÍ, no en la firma: un valor por omisión
    se fija al definir la función, así que ponerlo arriba haría que sustituir la
    constante (en pruebas, o desde fuera) dejara de tener efecto sin avisar.
    """
    if max_lotes is None:
        max_lotes = MAX_LOTES_POR_BASE
    lotes: list[Lote] = []
    con = _abrir(ruta)
    try:
        for tabla in _tablas(con):
            try:
                total = con.execute(f"SELECT count(*) FROM {_cita(tabla)}").fetchone()[0] or 0
            except sqlite3.Error:
                # Una tabla/vista que revienta el count NO se puede trocear: sus filas
                # no entran al índice NUNCA. Se registra para que `explorar` lo propague
                # como exploración PARCIAL; sin eso la base entra en HECHO con esa tabla
                # fuera y su copia parcial es indistinguible de una entera — el mismo
                # agujero silencioso que el tope de lotes.
                if tablas_omitidas is not None:
                    tablas_omitidas.append(tabla)
                continue
            if total == 0:
                continue

            if _tiene_rowid(con, tabla):
                fila = con.execute(
                    f"SELECT min(rowid), max(rowid) FROM {_cita(tabla)}"
                ).fetchone()
                lo, hi = (fila or (None, None))
                if lo is None:
                    # count>0 pero sin rango de rowid utilizable: las filas quedarían
                    # fuera del troceo. También es exploración parcial, no un cero limpio.
                    if tablas_omitidas is not None:
                        tablas_omitidas.append(tabla)
                    continue
                # Paso por rango, no por conteo: el rowid puede tener huecos y no hace
                # falta que los lotes salgan iguales, solo que cubran TODO el rango.
                paso = max(1, (int(hi) - int(lo) + 1) * filas_por_lote // max(total, 1))
                inicio = int(lo)
                while inicio <= int(hi) and len(lotes) < max_lotes:
                    fin = min(inicio + paso - 1, int(hi))
                    lotes.append(Lote(tabla, "rowid", inicio, fin))
                    inicio = fin + 1
            else:
                desplazamiento = 0
                while desplazamiento < total and len(lotes) < max_lotes:
                    lotes.append(
                        Lote(tabla, "offset", desplazamiento, desplazamiento + filas_por_lote)
                    )
                    desplazamiento += filas_por_lote

            if len(lotes) >= max_lotes:
                log.warning("sqlite_lotes_topados", tabla=tabla, tope=max_lotes)
                break
    finally:
        con.close()
    return lotes


def _parsear(ruta_interna: str) -> Lote:
    tabla, _, resto = ruta_interna.rpartition("/")
    modo, _, rango = resto.partition("-")
    desde, _, hasta = rango.partition("-")
    return Lote(tabla, modo, int(desde), int(hasta))


def servir_lote(
    ruta_fs: str | Path, ruta_interna: str, *, umbral_memoria: int, limite_bytes: int
) -> IO[bytes]:
    """El lote como NDJSON, listo para que lo recoja el plugin tabular."""
    lote = _parsear(ruta_interna)
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral_memoria)  # noqa: SIM115
    escritos = 0
    con = _abrir(ruta_fs)
    try:
        tabla = _cita(lote.tabla)
        if lote.modo == "rowid":
            cur = con.execute(
                f"SELECT * FROM {tabla} WHERE rowid BETWEEN ? AND ?", (lote.desde, lote.hasta)
            )
        else:
            cur = con.execute(
                f"SELECT * FROM {tabla} LIMIT ? OFFSET ?",
                (lote.hasta - lote.desde, lote.desde),
            )
        columnas = [d[0] for d in cur.description or []]
        for fila in cur:
            registro = {
                c: (v if isinstance(v, (str, int, float, type(None))) else str(v))
                for c, v in zip(columnas, fila, strict=False)
            }
            linea = (json.dumps(registro, ensure_ascii=False) + "\n").encode("utf-8")
            if escritos + len(linea) > limite_bytes:
                break  # tope duro: un lote no puede crecer sin límite
            spool.write(linea)
            escritos += len(linea)
    finally:
        con.close()
    spool.seek(0)
    return spool


def estimar_bytes(filas_por_lote: int = FILAS_POR_LOTE) -> int:
    """Tamaño aproximado de un lote, para los guards y la priorización.

    Es una estimación a propósito: medirlo de verdad exigiría materializar todos los
    lotes durante la EXPLORACIÓN, que es justo lo que el diseño evita ("listar SIN
    extraer").
    """
    return filas_por_lote * 200


def explorar(
    perillas: PerillasFiltro, ruta_fs: str | Path, mtime_ns: int
) -> tuple[list[tuple[str, str, int, int]], str | None, bool]:
    """Entradas `(ruta_interna, nombre, tamano, mtime_ns)`, motivo si no se pudo, y si
    la base quedó PARCIAL (troceada a medias: se alcanzó el tope de lotes, O una tabla
    no se pudo leer y sus filas no entran al índice).

    Ese tercer valor existe porque una exploración parcial es una pérdida de datos
    silenciosa: la base se explora "bien", entra en HECHO, y nadie sabe que le falta
    una tabla o la cola. Quien llama lo propaga a las señales de la fila para que sea
    consultable, no sólo registrable — es lo que impide que la federación se salte el
    barrido de una base a la que le falta contenido.

    Devuelve tuplas y no `EntradaContenedor` para no crear un import circular con
    `contenedores`, que es quien tiene ese tipo y quien llama aquí.
    """
    omitidas: list[str] = []
    try:
        lotes = planificar(
            ruta_fs, max_lotes=perillas.t3_sqlite_lotes_max, tablas_omitidas=omitidas
        )
    except sqlite3.DatabaseError as exc:
        log.warning("sqlite_no_explorable", error=str(exc)[:150])
        return [], "contenedor_corrupto", False
    if not lotes and not omitidas:
        return [], None, False
    if len(lotes) > perillas.t3_entradas_max:
        return [], "guard_entradas", False
    # `topado` = PARCIAL por cualquier causa: el tope de lotes, o una tabla ilegible.
    # En los dos casos la base entra en HECHO con filas sin indexar, así que quien
    # federa NO puede saltarse el barrido — la misma pérdida silenciosa que el tope.
    topado = len(lotes) >= perillas.t3_sqlite_lotes_max or bool(omitidas)
    if topado:
        log.warning(
            "sqlite_base_parcial",
            ruta=str(ruta_fs),
            tope=perillas.t3_sqlite_lotes_max,
            lotes=len(lotes),
            tablas_omitidas=omitidas[:20],
        )
    tam = estimar_bytes()
    entradas = [(lt.ruta_interna, f"{lt.tabla}-{lt.desde}.ndjson", tam, mtime_ns) for lt in lotes]
    return entradas, None, topado
