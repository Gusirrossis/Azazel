"""Plugin SQLite: esquema completo + filas priorizadas por las columnas que llevan identidad.

Una base de datos no es un documento, y ahí está toda la dificultad. El índice guarda
un doc por archivo con el texto topado a `extractor_max_chars`; una base de 28 GB tiene
millones de filas. Volcarlas no cabe, y volcar "las primeras N" indexa el principio de
una tabla cualquiera — que casi nunca es donde está lo que se busca.

Lo que se hace en su lugar:

  1. **El esquema entero, siempre.** Tablas, columnas y recuentos son baratos y hacen
     la base localizable ("¿dónde había una tabla `padron` con columna `curp`?"), que
     es la mitad del trabajo cuando hay 123 bases.
  2. **Las filas, priorizando columnas con IDENTIDAD.** El presupuesto de texto se
     gasta primero en las columnas que parecen CURP, RFC, nombre, teléfono o correo.
     De ahí salen las anclas, y de las anclas las entidades: es la diferencia entre
     una base buscable por persona y una que solo se sabe que existe.

**Lo que este plugin NO hace, y conviene saberlo:** con el tope de caracteres, una base
grande entra como MUESTRA. Para indexar una base entera por persona hay que tratarla
como contenedor —cada tabla o cada lote de filas, una entrada propia— igual que se hace
con los ZIP. Eso es otro trabajo, no este.

Dos cuidados con datos que son de otro:

  · Se abre `mode=ro` **e `immutable=1`**. Sin `immutable`, SQLite ve un `-wal` y trata
    de recuperarlo, es decir, ESCRIBIR. Sobre una base viva de otro sistema eso es
    inaceptable, y sobre un montaje read-only además falla. `immutable` promete que
    nadie la está tocando: es lo correcto para un archivo montado `:ro`.
  · Nunca se ejecuta SQL de fuera. Solo introspección y `SELECT` sobre identificadores
    que se citan con comillas dobles y se escapan.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from typing import Any

from . import ContextoExtraccion, ResultadoExtraccion, registrar

#: Columnas cuyo NOMBRE sugiere que contienen identidad. El orden no importa; lo que
#: importa es que estas van primero al gastar el presupuesto de texto, porque son las
#: que producen anclas (CURP/RFC) y las que hacen encontrable a una persona.
_PISTAS_IDENTIDAD = (
    "curp", "rfc", "nss", "clave_elector", "claveelector", "elector", "ine", "credencial",
    "nombre", "apellido", "paterno", "materno", "razon_social", "razonsocial",
    "email", "correo", "telefono", "celular", "movil",
    "domicilio", "direccion", "calle", "colonia", "municipio", "estado", "cp",
    "fecha_nac", "nacimiento", "curp_", "folio", "expediente", "cuenta",
)

#: Tablas internas de SQLite y de extensiones: no son datos del usuario.
_TABLAS_IGNORADAS = re.compile(r"^(sqlite_|_litestream|spatial_ref_sys)", re.I)

_MAX_TABLAS_DETALLE = 200
_FILAS_POR_LOTE = 200


def _ruta_de(ctx: ContextoExtraccion) -> tuple[str, bool]:
    """Ruta real del archivo, o una copia temporal. Devuelve (ruta, es_copia).

    `sqlite3` necesita un archivo en el filesystem, no un stream. Como
    `umbral_memoria_bytes` son 64 KB y una base siempre pasa de eso, la fuente casi
    siempre YA está materializada en disco: usarla directamente evita duplicar en
    disco los 28 GB de la base más grande.
    """
    nombre = getattr(ctx.fuente, "name", None)
    if isinstance(nombre, str) and os.path.isfile(nombre):
        return nombre, False

    ctx.fuente.seek(0)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    try:
        while bloque := ctx.fuente.read(4 * 1024 * 1024):
            tmp.write(bloque)
            if ctx.vencido():
                break
    finally:
        tmp.close()
    return tmp.name, True


def _abrir_solo_lectura(ruta: str) -> sqlite3.Connection:
    """Conexión que NO puede escribir ni intentar recuperar el WAL (ver módulo)."""
    uri = f"file:{ruta.replace('?', '%3f').replace('#', '%23')}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _tablas(con: sqlite3.Connection) -> list[str]:
    filas = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    return [n for (n,) in filas if not _TABLAS_IGNORADAS.match(n)]


def _columnas(con: sqlite3.Connection, tabla: str) -> list[str]:
    cur = con.execute(f'PRAGMA table_info("{tabla.replace(chr(34), chr(34) * 2)}")')
    return [f[1] for f in cur.fetchall()]


def _puntuar(columna: str) -> int:
    bajo = columna.lower()
    return sum(1 for pista in _PISTAS_IDENTIDAD if pista in bajo)


def _orden_por_identidad(columnas: list[str]) -> list[str]:
    """Columnas con identidad primero; el resto conserva su orden original."""
    return sorted(columnas, key=lambda c: (-_puntuar(c), columnas.index(c)))


@registrar("application/vnd.sqlite3")
def extraer_sqlite(ctx: ContextoExtraccion) -> ResultadoExtraccion:
    ruta, es_copia = _ruta_de(ctx)
    flags: list[str] = []
    partes: list[str] = []
    presupuesto = ctx.perillas.extractor_max_chars
    detalle: dict[str, Any] = {}
    total_filas = 0
    con: sqlite3.Connection | None = None

    try:
        try:
            con = _abrir_solo_lectura(ruta)
            tablas = _tablas(con)
        except sqlite3.DatabaseError as exc:
            return ResultadoExtraccion(flags=[f"sqlite_ilegible:{type(exc).__name__}"])

        if len(tablas) > _MAX_TABLAS_DETALLE:
            flags.append("sqlite_tablas_truncadas")
            tablas = tablas[:_MAX_TABLAS_DETALLE]

        # --- 1. el esquema, siempre: es barato y hace la base localizable
        for tabla in tablas:
            if ctx.vencido():
                flags.append("sqlite_parcial")
                break
            try:
                cols = _columnas(con, tabla)
                n = con.execute(
                    f'SELECT count(*) FROM "{tabla.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            except sqlite3.Error:
                continue  # una tabla rota no invalida el resto de la base
            total_filas += n or 0
            detalle[tabla] = {"filas": n, "columnas": cols[:80]}
            partes.append(f"tabla {tabla} ({n} filas): {', '.join(cols[:80])}")

        # --- 2. las filas, gastando el presupuesto en lo que lleva identidad primero
        largo = sum(len(p) for p in partes)
        # Las tablas con columnas de identidad van antes: si el presupuesto se acaba,
        # que se acabe habiendo leído el padrón y no una tabla de configuración.
        con_identidad = sorted(
            detalle.items(),
            key=lambda kv: -max((_puntuar(c) for c in kv[1]["columnas"]), default=0),
        )
        for tabla, info in con_identidad:
            if largo >= presupuesto or ctx.vencido():
                if largo >= presupuesto:
                    flags.append("sqlite_texto_truncado")
                break
            cols = _orden_por_identidad(list(info["columnas"]))
            if not cols:
                continue
            sel = ", ".join(f'"{c.replace(chr(34), chr(34) * 2)}"' for c in cols[:20])
            try:
                cur = con.execute(
                    f'SELECT {sel} FROM "{tabla.replace(chr(34), chr(34) * 2)}" LIMIT ?',
                    (_FILAS_POR_LOTE,),
                )
                for fila in cur:
                    linea = " | ".join("" if v is None else str(v) for v in fila)
                    partes.append(linea)
                    largo += len(linea)
                    if largo >= presupuesto or ctx.vencido():
                        break
            except sqlite3.Error:
                continue
    finally:
        if con is not None:
            con.close()
        if es_copia:
            try:
                os.unlink(ruta)
            except OSError:
                pass

    texto = "\n".join(partes)[:presupuesto]
    campos: dict[str, Any] = {
        "sqlite_tablas": len(detalle),
        "sqlite_filas_total": total_filas,
        "sqlite_tablas_nombres": sorted(detalle)[:50],
    }
    # Si la base es grande, decirlo en el propio doc: el texto es una MUESTRA y quien
    # busque tiene que saber que la ausencia de un nombre aquí no prueba nada.
    if total_filas > _FILAS_POR_LOTE * max(1, len(detalle)):
        flags.append("sqlite_muestra")

    perfil = {
        "filas": total_filas,
        "columnas": sum(len(i["columnas"]) for i in detalle.values()),
        "tablas_detalle": dict(list(detalle.items())[:50]),
    }
    return ResultadoExtraccion(campos=campos, texto=texto, perfil_calidad=perfil, flags=flags)
