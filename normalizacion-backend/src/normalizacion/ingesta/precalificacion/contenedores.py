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
import gzip
import lzma
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
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
    ratios: list[float],
    inicio: float,
    formato: str,
) -> ResultadoExploracion | None:
    if len(entradas) > perillas.t3_entradas_max:
        return ResultadoExploracion(False, "guard_entradas", (), formato)
    if sum(e.tamano for e in entradas) > perillas.t3_descomprimido_max_bytes:
        return ResultadoExploracion(False, "guard_descomprimido", (), formato)
    if any(r > perillas.t3_ratio_compresion_max for r in ratios):
        return ResultadoExploracion(False, "guard_ratio", (), formato)
    if time.monotonic() - inicio > perillas.t3_timeout_s:
        return ResultadoExploracion(False, "guard_timeout", (), formato)
    return None


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
    guard = _validar_guards(perillas, entradas, ratios, inicio, "zip")
    return guard or ResultadoExploracion(True, None, tuple(entradas), "zip")


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
    # En 7z "sólido" el tamaño comprimido por entrada puede no existir → solo
    # aplican los guards de total/entradas/timeout (el ratio es por-archivo zip)
    ratios = [int(i.uncompressed or 0) / max(int(i.compressed), 1) for i in infos if i.compressed]
    guard = _validar_guards(perillas, entradas, ratios, inicio, "7z")
    return guard or ResultadoExploracion(True, None, tuple(entradas), "7z")


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
    guard = _validar_guards(perillas, entradas, ratios, inicio, formato)
    return guard or ResultadoExploracion(True, None, tuple(entradas), formato)


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
    ratios = [sum(e.tamano for e in entradas) / max(comprimido, 1)]
    guard = _validar_guards(perillas, entradas, ratios, inicio, "tar")
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
    ratios = [total / max(comprimido, 1)]
    guard = _validar_guards(perillas, entradas, ratios, inicio, formato)
    return guard or ResultadoExploracion(True, None, tuple(entradas), formato)


# ------------------------------------------------------------------ despacho


_EXPLORADORES: dict[str, Callable[[PerillasFiltro, Path | IO[bytes]], ResultadoExploracion]] = {
    "application/zip": _explorar_zip,
    "application/x-7z-compressed": _explorar_7z,
    "application/x-rar-compressed": _explorar_rar,
    "application/x-tar": _explorar_tar,
    "application/gzip": lambda p, f: _explorar_flujo(p, f, "gz"),
    "application/x-bzip2": lambda p, f: _explorar_flujo(p, f, "bz2"),
    "application/x-xz": lambda p, f: _explorar_flujo(p, f, "xz"),
}


def explorar(perillas: PerillasFiltro, fuente: Path | IO[bytes], tipo: str) -> ResultadoExploracion:
    """Lista el contenedor y valida guards. NUNCA lanza por archivo hostil: devuelve flag."""
    explorador = _EXPLORADORES.get(tipo)
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
# un temporal y servir cada entrada como una lectura de disco (O(N)). La caché es
# por PROCESO (los workers son procesos separados; cada uno extrae una vez) y tiene
# un tope de disco con evicción LRU. Se limpia al salir el proceso.

_CACHE_7Z_LOCK = threading.Lock()
_CACHE_7Z: OrderedDict[tuple[str, int, int], tuple[Path, int]] = OrderedDict()
_CACHE_7Z_BYTES = 0
_CACHE_7Z_MAX_BYTES = int(os.environ.get("NORM_T3_CACHE_DISCO_BYTES", str(5 * 1024**3)))


def _limpiar_cache_7z() -> None:
    with _CACHE_7Z_LOCK:
        for destino, _ in _CACHE_7Z.values():
            shutil.rmtree(destino, ignore_errors=True)
        _CACHE_7Z.clear()


atexit.register(_limpiar_cache_7z)


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


def _dir_7z_extraido(ruta_fs: Path) -> Path:
    """Extrae el 7z COMPLETO una sola vez (cacheado por proceso) y devuelve el
    directorio temporal. Todas las entradas se sirven de ahí — O(N), no O(N²)."""
    import py7zr

    st = ruta_fs.stat()
    clave = (str(ruta_fs.resolve()), st.st_size, st.st_mtime_ns)
    global _CACHE_7Z_BYTES
    with _CACHE_7Z_LOCK:
        cacheado = _CACHE_7Z.get(clave)
        if cacheado is not None and cacheado[0].is_dir():
            _CACHE_7Z.move_to_end(clave)
            return cacheado[0]

        destino = Path(tempfile.mkdtemp(prefix="norm7z_"))
        try:
            with py7zr.SevenZipFile(ruta_fs, mode="r") as sz:
                sz.extractall(path=destino)
        except Exception:
            shutil.rmtree(destino, ignore_errors=True)
            raise
        _abrir_lectura_arbol(destino)

        tam = _tam_arbol(destino)
        _CACHE_7Z[clave] = (destino, tam)
        _CACHE_7Z_BYTES += tam
        # Evicción LRU por presupuesto de disco (jamás desaloja el recién creado)
        while _CACHE_7Z_BYTES > _CACHE_7Z_MAX_BYTES and len(_CACHE_7Z) > 1:
            vieja_clave, (viejo_dir, viejo_tam) = next(iter(_CACHE_7Z.items()))
            if vieja_clave == clave:
                break
            _CACHE_7Z.popitem(last=False)
            _CACHE_7Z_BYTES -= viejo_tam
            shutil.rmtree(viejo_dir, ignore_errors=True)
        return destino


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
            if cab.startswith(b"PK\x03\x04"):
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
            else:
                raise OSError(f"paso de cadena con formato no soportado: {entrada}")
            fobj.close()
            fobj = siguiente
            ruta_fs_actual = None
        fobj.seek(0)
        return fobj
    except ContenedorInseguro:
        fobj.close()
        raise
    except Exception as exc:
        fobj.close()
        if isinstance(exc, OSError):
            raise
        raise OSError(f"cadena irresoluble {cadena}: {exc}") from exc
