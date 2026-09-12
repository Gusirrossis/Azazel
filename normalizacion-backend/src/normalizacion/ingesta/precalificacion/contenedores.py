"""T3: contenedores por archivo interno — listar SIN extraer + guards anti zip-bomb.

Formatos explorables: **ZIP** (stdlib), **7z** (py7zr, puro Python) y **RAR**.
El RAR usa dos binarios notarizados (sin bypass de Gatekeeper): se **lista** con
7-Zip `7zz` (`brew install sevenzip`) y se **extrae** con `unar`/The Unarchiver
(`brew install unar`), que sí soporta los métodos RAR5 que el decodificador de
7-Zip rechaza. Ambos leen el RAR en sitio, sin copiar los GB. Las cadenas anidadas son
multi-formato: un CSV dentro de un 7z dentro de un ZIP se resuelve paso a paso.

Decisión del usuario: los comprimidos tienen PRIORIDAD (la mayoría de la
información útil viene dentro) y se tratan con cuidado — guards estrictos,
flags en vez de crashes, y preservación SIEMPRE.

Patrones: patool (solo el patrón de listar, GPLv3) · tika (ratio 100:1, flags) ·
plaso (path specs serializables, BFS vía la cola).
"""

from __future__ import annotations

import atexit
import bz2
import contextlib
import gzip
import hashlib
import lzma
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO, Any

from normalizacion.core.config import PerillasFiltro
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("contenedores")

EXPLORABLES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/gzip",
        "application/x-bzip2",
        "application/x-xz",
    }
)

_BLOQUE = 1024 * 1024
_MAGIA_7Z = b"7z\xbc\xaf\x27\x1c"
_MAGIA_RAR = b"Rar!"


class ContenedorInseguro(Exception):
    """Una entrada violó los límites al materializarla (defensa en profundidad)."""


class ContenedorIlegible(Exception):
    """El contenedor no se puede abrir con NINGUNA de las herramientas disponibles.

    Es PERMANENTE, y por eso no viaja como `OSError`: reintentarlo re-descomprime
    el archivo ENTERO para volver a fallar en la misma entrada. Medido en
    `vps-storage-01`: un 7z con filtro BCJ2 dejó una corrida 3 h sin procesar un
    solo archivo, quemando un núcleo y escribiendo 104 GB en reintentos.
    """


@dataclass(frozen=True)
class EntradaContenedor:
    """Una entrada interna, descrita SIN haberla extraído."""

    ruta_interna: str
    nombre: str
    tamano: int
    mtime_ns: int


@dataclass(frozen=True)
class ResultadoExploracion:
    ok: bool
    motivo: str | None  # guard_* | contenedor_corrupto | formato_no_soportado | None
    entradas: tuple[EntradaContenedor, ...]
    formato: str
    #: El contenedor se exploró BIEN pero incompleto: se alcanzó un tope y la cola no
    #: se enumeró. `ok` sigue siendo True —lo listado es válido— pero la copia es
    #: parcial, y eso tiene que llegar a la fila: si no, una base a medias es
    #: indistinguible de una entera. Opcional para no tocar a los demás formatos.
    topado: bool = False


def _mtime_ns(dt: datetime | None) -> int:
    if dt is None:
        dt = datetime(1980, 1, 1, tzinfo=UTC)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1_000_000_000


def _mtime_ns_tupla(fecha: tuple[int, ...]) -> int:
    if len(fecha) < 6:
        return _mtime_ns(None)
    try:
        a, m, d, h, mi, s = fecha[:6]
        return _mtime_ns(datetime(a, m, d, h, mi, s, tzinfo=UTC))
    except (ValueError, TypeError):  # fechas basura en archivos hostiles
        return _mtime_ns(None)


def _validar_guards(
    perillas: PerillasFiltro,
    entradas: list[EntradaContenedor],
    inicio: float,
    formato: str,
) -> ResultadoExploracion | None:
    """Guards de CONTENEDOR COMPLETO: conteo de entradas, bytes totales descomprimidos y
    tiempo de listado. El ratio de compresión ya NO veta aquí: se aísla por-entrada (ver
    `_aislar_ratio`), para que una sola entrada muy compresible no tire la cobertura de
    todas las demás."""
    if len(entradas) > perillas.t3_entradas_max:
        return ResultadoExploracion(False, "guard_entradas", (), formato)
    if sum(e.tamano for e in entradas) > perillas.t3_descomprimido_max_bytes:
        return ResultadoExploracion(False, "guard_descomprimido", (), formato)
    if time.monotonic() - inicio > perillas.t3_timeout_s:
        return ResultadoExploracion(False, "guard_timeout", (), formato)
    return None


def _aislar_ratio(
    entradas: list[EntradaContenedor], ratios: list[float], max_ratio: float
) -> tuple[list[EntradaContenedor], int]:
    """Aísla (NO explota) las entradas cuyo ratio de compresión supera `max_ratio` —una
    posible bomba— y devuelve el resto para explorar, más cuántas se aislaron.

    Antes UNA entrada sobre el umbral vetaba el contenedor ENTERO a COLD: datos legítimos
    muy compresibles (un CSV disperso, un log, un XML con relleno) hacían perder la
    cobertura de TODO lo demás. La entrada aislada se PRESERVA íntegra (el contenedor va a
    HOT), no se pierde: solo no se explota su contenido, y la exploración se marca parcial
    (`topado`). `ratios` va alineado con `entradas` (0 = sin dato de ratio → nunca se
    aísla); si no viene alineado, no se aísla nada."""
    if len(ratios) != len(entradas):
        return entradas, 0
    kept = [e for e, r in zip(entradas, ratios, strict=True) if r <= max_ratio]
    return kept, len(entradas) - len(kept)


# ------------------------------------------------------------------ ZIP


def _explorar_zip(perillas: PerillasFiltro, fuente: Path | IO[bytes]) -> ResultadoExploracion:
    inicio = time.monotonic()
    if not isinstance(fuente, Path):
        fuente.seek(0)
    with zipfile.ZipFile(fuente) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        entradas = [
            EntradaContenedor(
                ruta_interna=i.filename,
                nombre=i.filename.rsplit("/", 1)[-1],
                tamano=i.file_size,
                mtime_ns=_mtime_ns_tupla(i.date_time),
            )
            for i in infos
        ]
        ratios = [i.file_size / max(i.compress_size, 1) for i in infos]
    entradas, aisladas = _aislar_ratio(entradas, ratios, perillas.t3_ratio_compresion_max)
    guard = _validar_guards(perillas, entradas, inicio, "zip")
    return guard or ResultadoExploracion(True, None, tuple(entradas), "zip", topado=aisladas > 0)


# ------------------------------------------------------------------ 7z


def _explorar_7z(perillas: PerillasFiltro, fuente: Path | IO[bytes]) -> ResultadoExploracion:
    import py7zr

    inicio = time.monotonic()
    if not isinstance(fuente, Path):
        fuente.seek(0)
    with py7zr.SevenZipFile(fuente, mode="r") as sz:
        infos = [i for i in sz.list() if not i.is_directory]
    entradas = [
        EntradaContenedor(
            ruta_interna=i.filename,
            nombre=i.filename.rsplit("/", 1)[-1],
            tamano=int(i.uncompressed or 0),
            mtime_ns=_mtime_ns(i.creationtime),
        )
        for i in infos
    ]
    # En 7z "sólido" el tamaño comprimido por entrada puede no existir (0.0 = sin dato →
    # esa entrada no se aísla); las que sí lo traen se aíslan por-entrada como en zip.
    ratios = [
        int(i.uncompressed or 0) / max(int(i.compressed), 1) if i.compressed else 0.0
        for i in infos
    ]
    entradas, aisladas = _aislar_ratio(entradas, ratios, perillas.t3_ratio_compresion_max)
    guard = _validar_guards(perillas, entradas, inicio, "7z")
    return guard or ResultadoExploracion(True, None, tuple(entradas), "7z", topado=aisladas > 0)


# ------------------------------------------------------------------ RAR (vía 7-Zip)


def _ruta_temporal_de(fuente: Path | IO[bytes]) -> tuple[str, bool]:
    """7-Zip necesita una RUTA en disco. Un archivo del filesystem se usa tal cual
    (sin copiar los GB); un flujo anidado se vuelca a un temporal."""
    if isinstance(fuente, Path):
        return str(fuente), False
    fuente.seek(0)
    with tempfile.NamedTemporaryFile(suffix=".rar", delete=False) as tmp:
        while bloque := fuente.read(_BLOQUE):
            tmp.write(bloque)
        return tmp.name, True


_7ZZ_CACHE: list[str | None] = []


def _7zz_bin() -> str:
    """Ruta al binario 7-Zip (`7zz`, notarizado vía Homebrew). Cacheada por proceso."""
    if not _7ZZ_CACHE:
        candidatos = ("/opt/homebrew/bin/7zz", "/usr/local/bin/7zz")
        _7ZZ_CACHE.append(
            shutil.which("7zz")
            or shutil.which("7z")
            or next((c for c in candidatos if Path(c).exists()), None)
        )
    binario = _7ZZ_CACHE[0]
    if binario is None:
        raise FileNotFoundError("7zz no encontrado en PATH (instala con: brew install sevenzip)")
    return binario


_UNAR_CACHE: list[str | None] = []


def _unar_bin() -> str:
    """Ruta al binario The Unarchiver (`unar`, notarizado vía Homebrew). Cacheada."""
    if not _UNAR_CACHE:
        candidatos = ("/opt/homebrew/bin/unar", "/usr/local/bin/unar")
        _UNAR_CACHE.append(
            shutil.which("unar") or next((c for c in candidatos if Path(c).exists()), None)
        )
    binario = _UNAR_CACHE[0]
    if binario is None:
        raise FileNotFoundError("unar no encontrado en PATH (instala con: brew install unar)")
    return binario


def _mtime_ns_iso(valor: str) -> int:
    """'2026-03-20 13:49:04.0000000' → ns UTC. Vacío/ilegible → época por defecto."""
    valor = valor.strip()
    if not valor:
        return _mtime_ns(None)
    try:
        return _mtime_ns(datetime.strptime(valor[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC))
    except ValueError:  # fechas basura en archivos hostiles
        return _mtime_ns(None)


def _parse_slt_7zz(salida: str) -> tuple[list[EntradaContenedor], list[float]]:
    """Parsea `7zz l -slt`: bloques `clave = valor` tras la línea separadora '----------'.
    Omite carpetas (`Folder = +` o atributo D). El ratio sale de Size/Packed Size."""
    marcador = salida.find("\n----------\n")
    cuerpo = salida[marcador + len("\n----------\n") :] if marcador != -1 else ""
    entradas: list[EntradaContenedor] = []
    ratios: list[float] = []
    for bloque in cuerpo.split("\n\n"):
        campos: dict[str, str] = {}
        for linea in bloque.splitlines():
            clave, sep, val = linea.partition(" = ")
            if sep:
                campos[clave] = val
        ruta = campos.get("Path")
        if not ruta or campos.get("Folder") == "+" or "D" in campos.get("Attributes", ""):
            continue
        ruta = ruta.replace("\\", "/")
        try:
            tamano = int(campos.get("Size") or 0)
        except ValueError:
            tamano = 0
        entradas.append(
            EntradaContenedor(
                ruta_interna=ruta,
                nombre=ruta.rsplit("/", 1)[-1],
                tamano=tamano,
                mtime_ns=_mtime_ns_iso(campos.get("Modified", "")),
            )
        )
        try:
            empacado = int(campos.get("Packed Size") or 0)
        except ValueError:
            empacado = 0
        if empacado:
            ratios.append(tamano / max(empacado, 1))
    return entradas, ratios


def _listar_con_7zz(
    perillas: PerillasFiltro, ruta: str, inicio: float, formato: str
) -> ResultadoExploracion:
    """Lista un contenedor con 7-Zip. Timeout duro = guard_timeout; rc≠0 = corrupto."""
    try:
        proc = subprocess.run(
            [_7zz_bin(), "l", "-slt", "-sccUTF-8", "-p", "--", ruta],
            capture_output=True,
            timeout=perillas.t3_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ResultadoExploracion(False, "guard_timeout", (), formato)
    if proc.returncode != 0:
        return ResultadoExploracion(False, "contenedor_corrupto", (), formato)
    entradas, ratios = _parse_slt_7zz(proc.stdout.decode("utf-8", "replace"))
    entradas, aisladas = _aislar_ratio(entradas, ratios, perillas.t3_ratio_compresion_max)
    guard = _validar_guards(perillas, entradas, inicio, formato)
    return guard or ResultadoExploracion(True, None, tuple(entradas), formato, topado=aisladas > 0)


def _explorar_rar(perillas: PerillasFiltro, fuente: Path | IO[bytes]) -> ResultadoExploracion:
    """Lista un RAR con 7-Zip (`7zz`). Un archivo del filesystem se lista en sitio (sin
    copiar los GB); un RAR anidado dentro de otro contenedor se vuelca a un temporal."""
    inicio = time.monotonic()
    ruta, es_temporal = _ruta_temporal_de(fuente)
    try:
        return _listar_con_7zz(perillas, ruta, inicio, "rar")
    finally:
        if es_temporal:
            Path(ruta).unlink(missing_ok=True)


# ------------------------------------------------------------------ tar y flujos (stdlib)


def _tamano_de(fuente: Path | IO[bytes]) -> int:
    if isinstance(fuente, Path):
        return fuente.stat().st_size
    fuente.seek(0, 2)
    tamano = fuente.tell()
    fuente.seek(0)
    return tamano


def _explorar_tar(perillas: PerillasFiltro, fuente: Path | IO[bytes]) -> ResultadoExploracion:
    """tar plano o comprimido (`r:*` detecta gz/bz2/xz solo). Listar un tar comprimido
    descomprime el flujo completo — lento pero EXHAUSTIVO (decisión del usuario:
    explorar por completo aunque tarde)."""
    inicio = time.monotonic()
    comprimido = _tamano_de(fuente)
    if isinstance(fuente, Path):
        tf = tarfile.open(fuente, mode="r:*")  # noqa: SIM115 — se cierra en el with
    else:
        tf = tarfile.open(fileobj=fuente, mode="r:*")  # noqa: SIM115
    with tf:
        entradas = [
            EntradaContenedor(
                ruta_interna=m.name,
                nombre=m.name.rsplit("/", 1)[-1],
                tamano=m.size,
                mtime_ns=int(m.mtime) * 1_000_000_000,
            )
            for m in tf
            if m.isfile()
        ]
    # tar no comprime por-entrada (la compresión, si la hay, es del flujo .tar.gz): el
    # ratio sería whole-container, así que aquí solo protege el guard de bytes totales.
    guard = _validar_guards(perillas, entradas, inicio, "tar")
    return guard or ResultadoExploracion(True, None, tuple(entradas), "tar")


_ABRIDORES_FLUJO: dict[str, Callable[[IO[bytes]], Any]] = {
    "gz": lambda f: gzip.GzipFile(fileobj=f),
    "bz2": lambda f: bz2.BZ2File(f),
    "xz": lambda f: lzma.LZMAFile(f),  # noqa: SIM115 — los consumidores usan with
}


def _explorar_flujo(
    perillas: PerillasFiltro, fuente: Path | IO[bytes], formato: str
) -> ResultadoExploracion:
    """gz/bz2/xz: primero como tar comprimido (tgz…); si no, es UN solo miembro y su
    tamaño se MIDE descomprimiendo en streaming (no confiamos en el ISIZE de gzip,
    que da vuelta a los 4 GB)."""
    try:
        como_tar = _explorar_tar(perillas, fuente)
        # OJO: un flujo cuyo contenido empieza con ceros PARECE un "tar vacío válido"
        # — solo lo aceptamos como tar si trae entradas (o disparó un guard)
        if como_tar.entradas or not como_tar.ok:
            return como_tar
    except tarfile.ReadError:
        pass
    inicio = time.monotonic()
    comprimido = _tamano_de(fuente)
    crudo: IO[bytes] = fuente.open("rb") if isinstance(fuente, Path) else fuente
    try:
        total = 0
        with _ABRIDORES_FLUJO[formato](crudo) as flujo:
            while bloque := flujo.read(_BLOQUE):
                total += len(bloque)
                if total > perillas.t3_descomprimido_max_bytes:
                    return ResultadoExploracion(False, "guard_descomprimido", (), formato)
                if time.monotonic() - inicio > perillas.t3_timeout_s:
                    return ResultadoExploracion(False, "guard_timeout", (), formato)
    finally:
        if isinstance(fuente, Path):
            crudo.close()
    entradas = [EntradaContenedor("contenido", "contenido", total, _mtime_ns(None))]
    # El descomprimido ya se midió y topó arriba en streaming (guard de bytes): el ratio
    # whole-container sería redundante.
    guard = _validar_guards(perillas, entradas, inicio, formato)
    return guard or ResultadoExploracion(True, None, tuple(entradas), formato)


# ------------------------------------------------------------------ despacho


def _explorar_sqlite(
    perillas: PerillasFiltro, fuente: Path | IO[bytes]
) -> ResultadoExploracion:
    """Una base como contenedor: cada LOTE de filas es una entrada (ver `tabla_lotes`).

    Sin esto una base entra como un único documento con el texto topado, es decir, una
    muestra: en una tabla de 346.748 filas se indexaban ~200. Troceada, entra entera.
    """
    from . import tabla_lotes

    inicio = time.monotonic()
    ruta, es_copia = _ruta_temporal_de(fuente)
    try:
        mtime_ns = int(Path(ruta).stat().st_mtime * 1_000_000_000)
        crudas, motivo, topado = tabla_lotes.explorar(perillas, ruta, mtime_ns)
    finally:
        if es_copia:
            with contextlib.suppress(OSError):
                os.unlink(ruta)

    if motivo:
        return ResultadoExploracion(False, motivo, (), "sqlite")
    entradas = [EntradaContenedor(ri, nom, tam, mt) for ri, nom, tam, mt in crudas]
    # Los guards comunes también aquí: una base con millones de lotes es, a efectos
    # del pipeline, exactamente el mismo problema que una zip-bomb.
    if fallo := _validar_guards(perillas, entradas, inicio, "sqlite"):
        return fallo
    return ResultadoExploracion(True, None, tuple(entradas), "sqlite", topado=topado)


def _explorar_tabular_plano(
    perillas: PerillasFiltro, fuente: Path | IO[bytes], formato: str
) -> ResultadoExploracion:
    """Un CSV/NDJSON como contenedor: cada LOTE de filas es una entrada (ver `tabla_plana`).

    Sin esto, el extractor tabular lee solo los primeros `calidad_max_bytes` (10 MB) y
    trunca el texto a `extractor_max_chars`: un CSV grande se indexa como una muestra.
    Troceado, entra entero. A diferencia de SQLite NO hace falta materializar a disco: el
    stream anidado ya es seekable.
    """
    from . import tabla_plana

    inicio = time.monotonic()
    mtime_ns = int(fuente.stat().st_mtime * 1_000_000_000) if isinstance(fuente, Path) else _mtime_ns(None)
    crudas, motivo, topado = tabla_plana.explorar(perillas, fuente, formato, mtime_ns)
    if motivo:
        return ResultadoExploracion(False, motivo, (), formato)
    entradas = [EntradaContenedor(ri, nom, tam, mt) for ri, nom, tam, mt in crudas]
    if fallo := _validar_guards(perillas, entradas, inicio, formato):
        return fallo
    return ResultadoExploracion(True, None, tuple(entradas), formato, topado=topado)


def _explorar_texto(perillas: PerillasFiltro, fuente: Path | IO[bytes]) -> ResultadoExploracion:
    """Un texto grande (text/*, SQL, XML, rfc822) como contenedor de TROZOS de bytes (ver
    `texto_lotes`). Sin esto, `texto.py` lee solo `extractor_max_chars*4` bytes y trunca a
    100k chars: un dump/log/boletín grande pierde todo lo posterior."""
    from . import texto_lotes

    inicio = time.monotonic()
    mtime_ns = int(fuente.stat().st_mtime * 1_000_000_000) if isinstance(fuente, Path) else _mtime_ns(None)
    crudas, motivo, topado = texto_lotes.explorar(perillas, fuente, mtime_ns)
    if motivo:
        return ResultadoExploracion(False, motivo, (), "texto")
    entradas = [EntradaContenedor(ri, nom, tam, mt) for ri, nom, tam, mt in crudas]
    if fallo := _validar_guards(perillas, entradas, inicio, "texto"):
        return fallo
    return ResultadoExploracion(True, None, tuple(entradas), "texto", topado=topado)


def _explorar_documento(
    perillas: PerillasFiltro, fuente: Path | IO[bytes], formato: str
) -> ResultadoExploracion:
    """Un PDF/DOCX grande como contenedor de rangos de página/párrafo (ver
    `documento_lotes`). Un doc chico devuelve 0 entradas → se indexa como doc único."""
    from . import documento_lotes

    inicio = time.monotonic()
    mtime_ns = int(fuente.stat().st_mtime * 1_000_000_000) if isinstance(fuente, Path) else _mtime_ns(None)
    crudas, motivo, topado = documento_lotes.explorar(perillas, fuente, formato, mtime_ns)
    if motivo:
        return ResultadoExploracion(False, motivo, (), formato)
    entradas = [EntradaContenedor(ri, nom, tam, mt) for ri, nom, tam, mt in crudas]
    if fallo := _validar_guards(perillas, entradas, inicio, formato):
        return fallo
    return ResultadoExploracion(True, None, tuple(entradas), formato, topado=topado)


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_EXPLORADORES: dict[str, Callable[[PerillasFiltro, Path | IO[bytes]], ResultadoExploracion]] = {
    "application/zip": _explorar_zip,
    "application/x-7z-compressed": _explorar_7z,
    "application/x-rar-compressed": _explorar_rar,
    "application/x-tar": _explorar_tar,
    "application/gzip": lambda p, f: _explorar_flujo(p, f, "gz"),
    "application/x-bzip2": lambda p, f: _explorar_flujo(p, f, "bz2"),
    "application/x-xz": lambda p, f: _explorar_flujo(p, f, "xz"),
    "application/vnd.sqlite3": _explorar_sqlite,
    "text/csv": lambda p, f: _explorar_tabular_plano(p, f, "csv"),
    "application/x-ndjson": lambda p, f: _explorar_tabular_plano(p, f, "ndjson"),
    "text/plain": _explorar_texto,
    "application/sql": _explorar_texto,
    "message/rfc822": _explorar_texto,
    "application/xml": _explorar_texto,
    "application/pdf": lambda p, f: _explorar_documento(p, f, "pdf"),
    _DOCX_MIME: lambda p, f: _explorar_documento(p, f, "docx"),
}


def explorar(perillas: PerillasFiltro, fuente: Path | IO[bytes], tipo: str) -> ResultadoExploracion:
    """Lista el contenedor y valida guards. NUNCA lanza por archivo hostil: devuelve flag."""
    explorador = _EXPLORADORES.get(tipo)
    if explorador is None and tipo.startswith("text/"):
        explorador = _explorar_texto  # text/x-c, text/x-php… cualquier text/* es troceable
    if explorador is None:
        return ResultadoExploracion(False, "formato_no_soportado", (), tipo)
    try:
        return explorador(perillas, fuente)
    except (ImportError, FileNotFoundError) as exc:
        # Falta py7zr o el binario 7zz/unar → preservar íntegro sin explorar.
        log.warning("contenedor_sin_herramienta", error=str(exc)[:150])
        return ResultadoExploracion(False, "formato_no_soportado", (), tipo)
    except Exception:
        return ResultadoExploracion(False, "contenedor_corrupto", (), tipo)


# ------------------------------------------------------------------ caché de extracción 7z
#
# Un 7z "sólido" (default de 7-Zip con miles de archivos chicos) descomprime el
# BLOQUE ENTERO para sacar UNA sola entrada. Extraer entrada-por-entrada es O(N²):
# con 19 652 PDFs en un bloque de 458 MB, cada lectura re-descomprime los 458 MB
# → el pipeline nunca avanza. La cura: extraer el archivo COMPLETO una única vez a
# disco y servir cada entrada como una lectura (O(N)).
#
# La caché es PERSISTENTE y COMPARTIDA en disco (antes era por-proceso y se borraba al
# salir). El dir de un contenedor se nombra por el HASH de (ruta, tamaño, mtime), así que
# un proceso NUEVO —o un reinicio— REUSA lo ya extraído en vez de re-descomprimir 261 GB
# desde cero. Medido en `vps-storage-01`: cada reinicio pagaba ~2 h de re-extracción y el
# nodo pasaba más tiempo re-extrayendo lo mismo que procesando.

_CACHE_BASE = Path(
    os.environ.get("NORM_T3_CACHE_DIR") or (Path(tempfile.gettempdir()) / "norm7z_cache")
)
_MARCADOR = ".norm_completo"  # presente ⇒ la extracción de ese dir terminó ENTERA
_ARCHIVO_TAM = ".norm_tam"  # tamaño del árbol en bytes: evictar sin re-recorrerlo

_CACHE_7Z_LOCK = threading.Lock()
#: Memo EN-PROCESO (hash → dir persistente): evita re-hashear/re-stat dentro de un mismo
#: proceso. La fuente de verdad es el dir en disco, COMPARTIDO entre procesos.
_CACHE_7Z: dict[str, Path] = {}
_MINIMO_CACHE = 5 * 1024**3


def _tope_cache_por_defecto() -> int:
    """Sin tope explícito, la caché se dimensiona por el DISCO LIBRE, no por una
    constante.

    Eran 5 GB fijos (20 en algún nodo), y esto es un normalizador masivo: en
    `vps-storage-01` dos contenedores de la misma carpeta descomprimen 132 GB entre
    los dos. Con la caché por debajo del conjunto de trabajo, y las entradas de la
    cola entremezcladas —se reclaman por `archivo_id`, que es un hash—, cada salto de
    un contenedor al otro desalojaba 130 GB y los volvía a extraer. Medido: DIEZ HORAS
    sin indexar un solo documento, con el disco y la CPU al máximo. Un tope que no
    llega al conjunto de trabajo no limita: bloquea.

    Un tope sigue haciendo falta —sin ninguno se llena el disco, y este nodo ya llegó
    al 99 % una vez— pero tiene que salir de lo que hay, no de un número inventado.
    """
    try:
        libre = shutil.disk_usage(tempfile.gettempdir()).free
    except OSError:  # pragma: no cover - sin acceso al temporal
        return _MINIMO_CACHE
    # La mitad de lo libre: deja sitio de sobra para la ingesta y para el resto del
    # sistema, y aun así cabe un corpus comprimido entero varias veces.
    return max(_MINIMO_CACHE, int(libre * 0.5))


_CACHE_7Z_MAX_BYTES = int(
    os.environ.get("NORM_T3_CACHE_DISCO_BYTES") or _tope_cache_por_defecto()
)


def _limpiar_tmp_del_proceso() -> None:
    """Al salir borra SOLO los `.tmp.<pid>` a medias de ESTE proceso (extracciones que no
    llegaron al rename atómico). Los dirs COMPLETOS y compartidos SE QUEDAN: son la caché
    persistente que evita re-extraer en el próximo arranque."""
    with contextlib.suppress(OSError):
        for d in _CACHE_BASE.glob(f".*.tmp.{os.getpid()}"):
            shutil.rmtree(d, ignore_errors=True)


atexit.register(_limpiar_tmp_del_proceso)


def _limpiar_cache_7z() -> None:
    """Vacía la caché de extracción ENTERA: el memo en proceso Y los dirs persistentes en
    disco. Para tests y para un vaciado manual; el pipeline normal NUNCA la llama —la caché
    persiste a propósito entre procesos y reinicios—."""
    with _CACHE_7Z_LOCK:
        _CACHE_7Z.clear()
    with contextlib.suppress(OSError):
        if _CACHE_BASE.exists():
            shutil.rmtree(_CACHE_BASE, ignore_errors=True)


def _clave_persistente(ruta_fs: Path) -> str:
    """Hash determinista de (ruta resuelta, tamaño, mtime): el mismo contenedor cae SIEMPRE
    en el mismo dir, así que cualquier proceso lo reusa. Si el archivo cambia (mtime/tam),
    la clave cambia y se re-extrae — correcto."""
    st = ruta_fs.stat()
    material = f"{ruta_fs.resolve()}\0{st.st_size}\0{st.st_mtime_ns}".encode()
    return hashlib.sha256(material).hexdigest()


def _tam_guardado(d: Path) -> int:
    try:
        return int((d / _ARCHIVO_TAM).read_text())
    except (OSError, ValueError):  # dir de una versión anterior: recorrerlo una vez
        return _tam_arbol(d)


def _tocar(d: Path) -> None:
    """Marca el dir como usado reciente (LRU por mtime): un dir en uso no se evicta primero."""
    with contextlib.suppress(OSError):
        os.utime(d, None)


def _evictar_si_hace_falta(reservar: int) -> None:
    """LRU por mtime sobre la caché COMPARTIDA: quita los dirs completos más antiguos hasta
    que quepa `reservar`. Best-effort y tolerante a carreras —otro proceso puede estar
    leyendo uno; si desaparece, la extracción es idempotente y se rehace—. Solo toca dirs
    CON marcador (nunca un tmp en curso), y lee el tamaño de `.norm_tam` sin re-recorrer."""
    try:
        dirs = [d for d in _CACHE_BASE.iterdir() if d.is_dir() and (d / _MARCADOR).exists()]
    except OSError:
        return
    total = sum(_tam_guardado(d) for d in dirs)
    if total + reservar <= _CACHE_7Z_MAX_BYTES:
        return
    for d in sorted(dirs, key=lambda p: p.stat().st_mtime):
        if total + reservar <= _CACHE_7Z_MAX_BYTES:
            break
        t = _tam_guardado(d)
        shutil.rmtree(d, ignore_errors=True)
        total -= t
        # Desalojar TIENE que verse: si el conjunto de trabajo no cabe, esto ocurre en cada
        # salto entre contenedores y desde fuera se ve un nodo con el disco al máximo que no
        # avanza. Diez horas así en `vps-storage-01` antes de que nadie supiera por qué.
        log.warning(
            "cache_7z_evict",
            dir=d.name,
            bytes_desalojados=t,
            tope=_CACHE_7Z_MAX_BYTES,
            pista="si se repite, el tope no llega al conjunto de trabajo",
        )


def _tam_arbol(raiz: Path) -> int:
    total = 0
    for base, _, archivos in os.walk(raiz):
        for nombre in archivos:
            try:
                total += (Path(base) / nombre).stat().st_size
            except OSError:  # pragma: no cover - carrera con limpieza
                pass
    return total


def _abrir_lectura_arbol(raiz: Path) -> None:
    """py7zr respeta los permisos guardados en el 7z; las entradas creadas en
    Windows llegan sin modo Unix → 0o000 e ilegibles. El árbol es nuestro (temporal):
    forzamos lectura propia sobre todo él."""
    for base, dirs, archivos in os.walk(raiz):
        for d in dirs:
            try:
                (Path(base) / d).chmod(0o700)
            except OSError:  # pragma: no cover
                pass
        for nombre in archivos:
            try:
                (Path(base) / nombre).chmod(0o600)
            except OSError:  # pragma: no cover
                pass


def _extraer_7z_con_unar(ruta_fs: Path, destino: Path) -> None:
    """Extracción COMPLETA con The Unarchiver, para los filtros que py7zr no trae.

    `-no-directory` es obligatorio: por defecto unar añade una carpeta contenedora
    cuando el archivo tiene más de una entrada en la raíz, y entonces el árbol
    quedaría anidado un nivel de más respecto a lo que py7zr produce. `_paso_7z`
    resuelve `dir_ex / entrada` con las rutas guardadas en el 7z, así que ese nivel
    extra haría fallar TODAS las entradas — el mismo síntoma que se viene de curar.
    """
    proc = subprocess.run(
        [
            _unar_bin(),
            "-quiet",
            "-force-overwrite",
            "-no-directory",
            "-output-directory",
            str(destino),
            str(ruta_fs),
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        detalle = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:200]
        raise OSError(f"unar salió con {proc.returncode}: {detalle}")
    if not any(destino.iterdir()):
        raise OSError("unar terminó con éxito pero no extrajo nada")


def _extraer_7z_con_7zz(ruta_fs: Path, destino: Path) -> None:
    """Extracción COMPLETA con el binario 7zz (7-Zip nativo), en STREAMING a disco.

    A diferencia de `py7zr.extractall`, que descomprime el BLOQUE SOLID en RAM: sobre
    un 7z grande la memoria trepa hasta que el contenedor OOM-MATA el proceso ANTES de
    lanzar excepción —medido en `vps-storage-01`, los .7z de INE llevaban la RAM a los
    16 GB del contenedor sin procesar un solo lote, y por eso el fallback a `unar` no
    llegaba a correr—. 7zz descomprime incremental a disco: memoria acotada.

    `x` = extraer con rutas; `-y` = sí a todo; `-bd` = sin barra de progreso;
    `-o` fija el destino; `--` cierra las opciones (rutas que empiezan por `-`).
    """
    proc = subprocess.run(
        [_7zz_bin(), "x", "-y", "-bd", f"-o{destino}", "--", str(ruta_fs)],
        capture_output=True,
    )
    if proc.returncode != 0:
        detalle = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:200]
        raise OSError(f"7zz salió con {proc.returncode}: {detalle}")
    if not any(destino.iterdir()):
        raise OSError("7zz terminó con éxito pero no extrajo nada")


def _extraer_7z_a_disco(ruta_fs: Path, destino: Path) -> None:
    """Extrae el 7z entero a `destino`, probando extractores por orden de SEGURIDAD DE
    MEMORIA:

      1. `7zz` (7-Zip nativo) y 2. `unar` — STREAMING a disco, memoria ACOTADA.
      3. `py7zr` — último recurso, SOLO para entornos sin esos binarios (dev/tests):
         su `extractall` descomprime el bloque solid en RAM y sobre un 7z grande
         dispara el OOM del contenedor (medido en INE, `vps-storage-01`). En producción
         no se llega a py7zr porque 7zz está y va primero.

    Si los tres fallan, el archivo es de verdad ilegible → `ContenedorIlegible`.
    """

    def _via_py7zr(rf: Path, dst: Path) -> None:
        import py7zr

        with py7zr.SevenZipFile(rf, mode="r") as sz:
            sz.extractall(path=dst)

    intentos = (("7zz", _extraer_7z_con_7zz), ("unar", _extraer_7z_con_unar), ("py7zr", _via_py7zr))
    errores: list[str] = []
    for i, (nombre, extraer) in enumerate(intentos):
        for p in destino.iterdir():  # empezar de cero tras un intento fallido
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        try:
            extraer(ruta_fs, destino)
        except Exception as exc:
            errores.append(f"{nombre}={str(exc)[:150]}")
            continue
        if i:  # no fue el primario: dejar rastro de que se usó un fallback
            log.warning("7z_extraido_fallback", archivo=str(ruta_fs), via=nombre, previos=errores)
        return
    raise ContenedorIlegible(f"7z ilegible ({ruta_fs.name}): " + "; ".join(errores))


def _dir_7z_extraido(ruta_fs: Path) -> Path:
    """Extrae el 7z COMPLETO una sola vez (cacheado por proceso) y devuelve el
    directorio temporal. Todas las entradas se sirven de ahí — O(N), no O(N²).

    Extrae con 7zz (streaming a disco), NO con py7zr: su `extractall` descomprime el
    bloque solid en RAM y dispara el OOM del contenedor sobre archivos grandes.
    """
    clave = _clave_persistente(ruta_fs)
    with _CACHE_7Z_LOCK:
        memo = _CACHE_7Z.get(clave)
        if memo is not None and (memo / _MARCADOR).exists():
            return memo

    destino = _CACHE_BASE / clave
    if not (destino / _MARCADOR).exists():
        _extraer_a_persistente(ruta_fs, clave, destino)
    _tocar(destino)
    with _CACHE_7Z_LOCK:
        _CACHE_7Z[clave] = destino
    return destino


def _extraer_a_persistente(ruta_fs: Path, clave: str, destino: Path) -> None:
    """Extrae a un tmp propio y lo renombra ATÓMICAMENTE a `destino`. Si otro proceso ganó
    la carrera (el destino ya tiene marcador), descarta su tmp y usa el del otro. Un
    `destino` sin marcador es una extracción muerta de un proceso anterior → se rehace.

    El marcador y el tamaño se escriben DENTRO del tmp antes del rename, así que un
    `destino` renombrado está SIEMPRE completo: ningún proceso ve una extracción a medias.
    """
    _CACHE_BASE.mkdir(parents=True, exist_ok=True)
    marcador = destino / _MARCADOR
    if destino.exists() and not marcador.exists():
        shutil.rmtree(destino, ignore_errors=True)
    _evictar_si_hace_falta(reservar=ruta_fs.stat().st_size * 4)
    tmp = _CACHE_BASE / f".{clave}.tmp.{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        _extraer_7z_a_disco(ruta_fs, tmp)
        _abrir_lectura_arbol(tmp)
        (tmp / _ARCHIVO_TAM).write_text(str(_tam_arbol(tmp)))
        (tmp / _MARCADOR).write_bytes(b"")  # COMPLETO: marcado ANTES del rename atómico
        try:
            os.rename(tmp, destino)  # atómico en el mismo FS; falla si otro proceso ganó
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not marcador.exists():
                raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ------------------------------------------------------------------ abrir entradas


def _copiar_con_limite(origen: IO[bytes] | Any, destino: IO[bytes], limite: int, que: str) -> None:
    copiado = 0
    while bloque := origen.read(_BLOQUE):
        copiado += len(bloque)
        if copiado > limite:
            raise ContenedorInseguro(f"entrada '{que}' excede el límite ({limite} B)")
        destino.write(bloque)


def _paso_zip(fobj: IO[bytes], entrada: str, umbral: int, limite: int) -> IO[bytes]:
    # Sin context manager a propósito: el spool ES el valor de retorno
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral)  # noqa: SIM115
    fobj.seek(0)
    with zipfile.ZipFile(fobj) as zf, zf.open(entrada) as miembro:
        try:
            _copiar_con_limite(miembro, spool, limite, entrada)
        except ContenedorInseguro:
            spool.close()
            raise
    spool.seek(0)
    return spool


def _servir_desde_arbol(origen_fs: Path, entrada: str, umbral: int, limite: int) -> IO[bytes]:
    """Copia una entrada ya extraída (en disco) a un spool con tope duro."""
    if origen_fs.stat().st_size > limite:
        raise ContenedorInseguro(f"entrada '{entrada}' excede el límite ({limite} B)")
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral)  # noqa: SIM115
    try:
        with origen_fs.open("rb") as f:
            _copiar_con_limite(f, spool, limite, entrada)
    except ContenedorInseguro:
        spool.close()
        raise
    spool.seek(0)
    return spool


def _paso_7z(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None = None
) -> IO[bytes]:
    # 7z SÓLIDO: sacar una entrada descomprime el bloque entero. Si el 7z es un
    # archivo del filesystem, se extrae COMPLETO una vez (cacheado) y la entrada se
    # sirve como lectura de disco → O(N) en lugar de O(N²) (ver _dir_7z_extraido).
    if ruta_fs is not None:
        dir_ex = _dir_7z_extraido(ruta_fs)
        origen_fs = dir_ex / entrada
        if not origen_fs.is_file():
            raise OSError(f"entrada no encontrada en 7z: {entrada}")
        return _servir_desde_arbol(origen_fs, entrada, umbral, limite)

    # 7z ANIDADO dentro de otro contenedor (stream, sin ruta en disco): fallback
    # por-entrada. py7zr ≥1.0 ya no tiene read(): se extrae SOLO esa entrada a un
    # directorio temporal (el pre-check de tamaño va ANTES de descomprimir nada).
    import py7zr

    fobj.seek(0)
    with py7zr.SevenZipFile(fobj, mode="r") as sz:
        tamanos = {i.filename: int(i.uncompressed or 0) for i in sz.list()}
        if entrada not in tamanos:
            raise OSError(f"entrada no encontrada en 7z: {entrada}")
        if tamanos[entrada] > limite:
            raise ContenedorInseguro(f"entrada '{entrada}' excede el límite ({limite} B)")
        sz.reset()
        with tempfile.TemporaryDirectory() as tmpdir:
            sz.extract(path=tmpdir, targets=[entrada])
            extraido = Path(tmpdir) / entrada
            # py7zr aplica los permisos guardados en el 7z; las entradas creadas en
            # Windows (o con writestr) llegan sin modo Unix → 0o000 e ilegibles.
            # Forzamos lectura propia antes de abrir (el archivo es nuestro, temporal).
            extraido.chmod(0o600)
            spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral)  # noqa: SIM115
            try:
                with extraido.open("rb") as f:
                    _copiar_con_limite(f, spool, limite, entrada)
            except ContenedorInseguro:
                spool.close()
                raise
    spool.seek(0)
    return spool


def _paso_rar(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None = None
) -> IO[bytes]:
    """Extrae UNA entrada de un RAR con `unar` (soporta RAR5 que 7-Zip no decodifica)
    a un spool (RAM→disco) con tope duro. Usa el archivo en disco si se conoce
    (`ruta_fs`, sin copia); si no, vuelca el flujo a un temporal. Los RAR no-sólidos
    permiten extraer la entrada directamente sin descomprimir el archivo entero."""
    if ruta_fs is not None:
        ruta, es_temporal = str(ruta_fs), False
    else:
        ruta, es_temporal = _ruta_temporal_de(fobj)
    tmpdir = tempfile.mkdtemp()
    try:
        proc = subprocess.run(
            [_unar_bin(), "-quiet", "-force-overwrite", "-output-directory", tmpdir, ruta, entrada],
            capture_output=True,
        )
        # Extraemos exactamente UNA entrada-archivo → debe quedar un único regular.
        extraidos = [p for p in Path(tmpdir).rglob("*") if p.is_file()]
        if proc.returncode != 0 or not extraidos:
            raise OSError(f"unar no pudo extraer la entrada del rar: {entrada}")
        spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral)  # noqa: SIM115
        try:
            with extraidos[0].open("rb") as f:
                _copiar_con_limite(f, spool, limite, entrada)
        except ContenedorInseguro:
            spool.close()
            raise
        spool.seek(0)
        return spool
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if es_temporal:
            Path(ruta).unlink(missing_ok=True)


def _paso_tar(fobj: IO[bytes], entrada: str, umbral: int, limite: int) -> IO[bytes]:
    fobj.seek(0)
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral)  # noqa: SIM115
    with tarfile.open(fileobj=fobj, mode="r:*") as tf:
        try:
            miembro = tf.extractfile(entrada)
        except KeyError as exc:
            spool.close()
            raise OSError(f"entrada no encontrada en tar: {entrada}") from exc
        if miembro is None:
            spool.close()
            raise OSError(f"la entrada tar no es un archivo regular: {entrada}")
        with miembro:
            try:
                _copiar_con_limite(miembro, spool, limite, entrada)
            except ContenedorInseguro:
                spool.close()
                raise
    spool.seek(0)
    return spool


def _paso_flujo(fobj: IO[bytes], formato: str, entrada: str, umbral: int, limite: int) -> IO[bytes]:
    """gz/bz2/xz de un solo miembro: la entrada es siempre 'contenido'."""
    fobj.seek(0)
    spool: IO[bytes] = SpooledTemporaryFile(max_size=umbral)  # noqa: SIM115
    with _ABRIDORES_FLUJO[formato](fobj) as flujo:
        try:
            _copiar_con_limite(flujo, spool, limite, entrada)
        except ContenedorInseguro:
            spool.close()
            raise
    spool.seek(0)
    return spool


def _paso_sqlite(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None
) -> IO[bytes]:
    """Sirve un LOTE de filas como NDJSON (ver `tabla_lotes`).

    `sqlite3` necesita un archivo, no un stream. Si la base está en el filesystem
    —el caso normal— se lee en sitio y no se copian sus GB; solo cuando viene anidada
    dentro de otro contenedor hay que materializarla.
    """
    from . import tabla_lotes

    ruta = ruta_fs
    temporal: str | None = None
    if ruta is None:
        ruta_txt, es_copia = _ruta_temporal_de(fobj)
        ruta = Path(ruta_txt)
        temporal = ruta_txt if es_copia else None
    try:
        return tabla_lotes.servir_lote(
            ruta, entrada, umbral_memoria=umbral, limite_bytes=limite
        )
    finally:
        if temporal:
            with contextlib.suppress(OSError):
                os.unlink(temporal)


def _paso_tabular_plano(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None
) -> IO[bytes]:
    """Sirve un LOTE de un CSV/NDJSON como NDJSON (ver `tabla_plana`).

    A diferencia de SQLite, `tabla_plana` opera sobre un stream seekable, así que un CSV
    anidado NO se materializa: se sirve directo del spool que trajo el paso anterior.
    """
    from . import tabla_plana

    fuente: Path | IO[bytes] = ruta_fs if ruta_fs is not None else fobj
    return tabla_plana.servir_lote(fuente, entrada, umbral_memoria=umbral, limite_bytes=limite)


def _paso_texto(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None
) -> IO[bytes]:
    """Sirve un TROZO de un texto grande como texto crudo (ver `texto_lotes`). Opera sobre
    el stream seekable directo: un texto anidado NO se materializa."""
    from . import texto_lotes

    fuente: Path | IO[bytes] = ruta_fs if ruta_fs is not None else fobj
    return texto_lotes.servir_lote(fuente, entrada, umbral_memoria=umbral, limite_bytes=limite)


def _paso_documento(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None
) -> IO[bytes]:
    """Sirve un trozo de un PDF (mini-PDF) o DOCX (texto de párrafos) — ver `documento_lotes`."""
    from . import documento_lotes

    fuente: Path | IO[bytes] = ruta_fs if ruta_fs is not None else fobj
    return documento_lotes.servir_lote(fuente, entrada, umbral_memoria=umbral, limite_bytes=limite)


def abrir_entrada(
    raiz: Path, cadena: list[str], *, umbral_memoria: int, limite_bytes: int
) -> IO[bytes]:
    """Resuelve un path spec anidado MULTI-FORMATO (zip/7z/rar/tar/gz/bz2/xz en
    cualquier nivel).

    cadena[0] es la ruta en el filesystem; cada paso detecta el formato del
    contenedor actual por magic bytes (512 B: el magic de tar vive en el offset
    257) y extrae SOLO esa entrada a un spool (RAM hasta `umbral_memoria`, luego
    temporal) con tope duro `limite_bytes`.
    """
    fobj: IO[bytes] = (raiz / cadena[0]).open("rb")
    ruta_fs_actual: Path | None = raiz / cadena[0]
    try:
        for entrada in cadena[1:]:
            fobj.seek(0)
            cab = fobj.read(512)
            if entrada.startswith(("csv/", "ndjson/")):
                # Lote de un contenedor tabular plano: `entrada` es su `ruta_interna`
                # auto-descriptiva. CSV/NDJSON no tienen magic bytes que despachar, así
                # que se decide por el prefijo (igual que `Lote.ruta_interna` en SQLite).
                siguiente = _paso_tabular_plano(
                    fobj, entrada, umbral_memoria, limite_bytes, ruta_fs=ruta_fs_actual
                )
            elif entrada.startswith("texto/"):
                # Trozo de un texto grande troceado (ver `texto_lotes`), también por prefijo.
                siguiente = _paso_texto(
                    fobj, entrada, umbral_memoria, limite_bytes, ruta_fs=ruta_fs_actual
                )
            elif entrada.startswith(("pdf/", "docx/")):
                # Trozo de un PDF/DOCX troceado (ver `documento_lotes`), por prefijo.
                siguiente = _paso_documento(
                    fobj, entrada, umbral_memoria, limite_bytes, ruta_fs=ruta_fs_actual
                )
            elif cab.startswith(b"PK\x03\x04"):
                siguiente = _paso_zip(fobj, entrada, umbral_memoria, limite_bytes)
            elif cab.startswith(_MAGIA_7Z):
                siguiente = _paso_7z(
                    fobj, entrada, umbral_memoria, limite_bytes, ruta_fs=ruta_fs_actual
                )
            elif cab.startswith(_MAGIA_RAR):
                siguiente = _paso_rar(
                    fobj, entrada, umbral_memoria, limite_bytes, ruta_fs=ruta_fs_actual
                )
            elif cab.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")):
                # tar comprimido (tgz/tbz/txz) o flujo de un miembro: la exploración
                # nombra "contenido" a los miembros únicos — eso decide la rama
                formato = "gz" if cab[:2] == b"\x1f\x8b" else ("bz2" if cab[:3] == b"BZh" else "xz")
                if entrada == "contenido":
                    siguiente = _paso_flujo(fobj, formato, entrada, umbral_memoria, limite_bytes)
                else:
                    siguiente = _paso_tar(fobj, entrada, umbral_memoria, limite_bytes)
            elif len(cab) >= 262 and cab[257:262] == b"ustar":
                siguiente = _paso_tar(fobj, entrada, umbral_memoria, limite_bytes)
            elif cab.startswith(b"SQLite format 3\x00"):
                siguiente = _paso_sqlite(
                    fobj, entrada, umbral_memoria, limite_bytes, ruta_fs=ruta_fs_actual
                )
            else:
                raise OSError(f"paso de cadena con formato no soportado: {entrada}")
            fobj.close()
            fobj = siguiente
            ruta_fs_actual = None
        fobj.seek(0)
        return fobj
    except (ContenedorInseguro, ContenedorIlegible):
        # Ambas son PERMANENTES y viajan tal cual: envolverlas en `OSError` las
        # mandaría a la rama de reintentos del precalificador.
        fobj.close()
        raise
    except Exception as exc:
        fobj.close()
        if isinstance(exc, OSError):
            raise
        raise OSError(f"cadena irresoluble {cadena}: {exc}") from exc
