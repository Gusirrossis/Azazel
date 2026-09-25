"""T3: contenedores por archivo interno — listar SIN extraer + guards anti zip-bomb.

Formatos explorables: **ZIP** (stdlib), **7z** (py7zr, puro Python) y **RAR**.
El RAR se **lista** y se **extrae** con `unrar`, el oficial de RARLab: el decodificador
RAR5 de `unar` (The Unarchiver) dejó 49 entradas de 'Matrix.rar' a 0 B o truncadas y
`lsar` leyó mal el tamaño de otra, con el archivo sano. `7zz` y `lsar`/`unar` quedan de
respaldo si falta `unrar`. Todos leen el RAR en sitio, sin copiar los GB. Las cadenas
anidadas son multi-formato: un CSV dentro de un 7z dentro de un ZIP se resuelve paso a
paso.

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
import io
import json
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
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


class ExtraccionIncompleta(ContenedorIlegible):
    """Una entrada listada que la extracción NO dejó entera: falta en el árbol o no mide lo
    que declara el listado. PERMANENTE (el árbol de la caché no va a cambiar) y a la
    vista: antes se servía tal cual, y `unar` había dejado 45 entradas de 'Matrix.rar' a
    0 B y 3 truncadas. Las de 0 B llegaron a libmagic como `application/x-empty` y se
    fueron a COLD con motivo `fuera_de_lista_blanca`, así que un fallo nuestro se leía
    como «no hay nada». Las truncadas se indexaron sin su cola."""


class FalloDelNodo(OSError):
    """La herramienta falló por el NODO, no por el archivo: la mataron (SIGTERM al parar
    los workers, SIGKILL del OOM), no pudo leer el disco o se quedó sin memoria. Es un
    `OSError`, es decir, transitorio con tope, y `explorar` lo deja pasar en vez de
    convertirlo en `contenedor_corrupto`. Si no, un `unrar lt` interrumpido marcaba el RAR
    como corrupto y se volvía a listar con `lsar`, que no ve su entrada de 21 GB."""


class DiscoInsuficiente(FalloDelNodo):
    """La extracción no cabe en el disco de la caché. Es transitorio porque habla del nodo,
    no del archivo, y se resuelve cuando hay espacio. Se lanza ANTES de escribir nada,
    porque extraer igualmente llenaba el disco (el nodo ya llegó una vez al 99 %)."""


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
    #: Entradas listadas sin un tamaño creíble (ver `_parse_json_lsar`): no se encolan y
    #: dejan el contenedor `topado`. Van aparte porque `topado` también lo pone el
    #: aislamiento por ratio, y desde la fila no se distinguiría una cosa de la otra.
    tamano_ilegible: int = 0


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


# ------------------------------------------------------------------ RAR (unrar; respaldo 7zz/lsar)


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


_UNRAR_CACHE: list[str | None] = []


def _unrar_bin() -> str:
    """Ruta a `unrar`, el extractor OFICIAL de RARLab. Cacheada por proceso.

    De lo que cabe en la imagen, es el que abre bien los RAR: sobre 'Matrix.rar' (RAR5,
    sano: `unrar t` da rc=0 con 0 errores), `unar` 1.10.1 —y también 1.10.8— falla en las
    mismas 48 entradas, y `unrar` las extrae enteras con el CRC de la cabecera. El `7zz`
    de Debian no trae el códec RAR. Debian trae `unrar` en non-free (ver
    deploy/Dockerfile)."""
    if not _UNRAR_CACHE:
        candidatos = ("/opt/homebrew/bin/unrar", "/usr/local/bin/unrar")
        _UNRAR_CACHE.append(
            shutil.which("unrar") or next((c for c in candidatos if Path(c).exists()), None)
        )
        if _UNRAR_CACHE[0] is None:
            # Una vez por proceso y a la vista: sin unrar los RAR caen a lsar/unar. En
            # 'Matrix.rar' eso deja sin encolar la entrada de 21 GB y 49 entradas en ERROR.
            log.warning(
                "unrar_ausente",
                pista="los RAR se listan con lsar y se extraen con unar; ver deploy/Dockerfile",
            )
    binario = _UNRAR_CACHE[0]
    if binario is None:
        raise FileNotFoundError("unrar no encontrado en PATH (Debian: paquete `unrar`, non-free)")
    return binario


def _hay_unrar() -> bool:
    try:
        _unrar_bin()
    except FileNotFoundError:
        return False
    return True


def _entorno_unrar() -> dict[str, str]:
    """El nombre y el mtime de cada entrada forman su `archivo_id`, y `unrar` los imprime
    según el entorno:
      - el mtime, en hora LOCAL: sobre el mismo RAR, con TZ=America/Mexico_City sale 6 h
        antes que con TZ=UTC (medido). Se fija UTC, que es lo que dio `lsar`.
      - el nombre, en Linux, con `wcsrtombs` del locale (`setlocale(LC_ALL, "")` en
        rar.cpp). Sin locale UTF-8 cambia el único nombre no ASCII de 'Matrix.rar'
        (medido: 1132 de 1133 iguales), y también el archivo que escribe `unrar x`, que
        ya no casaría con el listado. En macOS unrar escribe UTF-8 siempre, así que
        forzar C.UTF-8 no le afecta aunque ese locale no exista allí.
    Hoy la imagen trae LANG=C.UTF-8. Se fija aquí para no depender de ello."""
    return {**os.environ, "TZ": "UTC", "LC_ALL": "C.UTF-8"}


#: Códigos de salida de `unrar` que hablan del NODO y no del archivo (errhnd.hpp): 5
#: escritura, 6 apertura, 8 memoria, 9 crear archivo, 12 error de E/S al leer el RAR y 255
#: interrumpido. Un rc negativo es una señal no atrapada (el SIGKILL del OOM). Se
#: reintentan. Los demás (1 aviso, 2 fatal, 3 CRC o archivo truncado, 4 bloqueado, 10 sin
#: archivos, 11 contraseña…) no cambian por reintentar.
_RC_UNRAR_TRANSITORIOS = frozenset({5, 6, 8, 9, 12, 255})


def _rc_unrar_transitorio(rc: int) -> bool:
    return rc < 0 or rc in _RC_UNRAR_TRANSITORIOS


#: Un tamaño declarado por encima de esto no es un tamaño, es un campo mal leído. `lsar`
#: 1.10.1 da 18.446.744.073.659.426.162 (2^64 - 50.125.454) para una entrada de
#: 21.424.711.026 B de 'Matrix.rar': son los 32 bits bajos del tamaño real, extendidos
#: con signo. 2^62 B son 4 EiB y ningún archivo real se acerca.
_TAMANO_INCREIBLE = 2**62


def _entero(valor: object) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int | float | str):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


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


def _parse_lt_unrar(salida: str) -> tuple[list[EntradaContenedor], list[float], list[str]]:
    """Parsea `unrar lt`, que imprime un bloque `Clave: valor` por cabecera y separa los
    bloques con una línea en blanco. Solo cuenta `Type: File`: las carpetas y los
    enlaces simbólicos no se sirven.

    Las redirecciones de RAR5 (`Hard link` y `File reference`, las copias de `rar -oi`)
    tampoco se cuentan, aunque `unrar x` las escribe como archivos regulares. Apuntan a
    otra entrada del MISMO RAR, que sí se lista y se sirve, así que su contenido entra
    igual; solo se pierde la ruta duplicada. No se aceptan sin medir qué `Size` declaran
    (el que un enlace duro lleva en la cabecera lo decide `rar`, que es cerrado): si no
    coincide con lo extraído, la verificación daría la copia por rota.

    Devuelve las entradas, sus ratios ALINEADOS con ellas (0.0 = sin dato, nunca se
    aísla) y las rutas cuyo tamaño no se pudo leer (ver `_parse_json_lsar`).

    El valor de `Name` no se recorta: un nombre puede acabar en espacio."""
    entradas: list[EntradaContenedor] = []
    ratios: list[float] = []
    desconocidas: list[str] = []
    for bloque in salida.split("\n\n"):
        campos: dict[str, str] = {}
        for linea in bloque.splitlines():
            clave, sep, val = linea.lstrip().partition(": ")
            if sep and clave not in campos:
                campos[clave] = val
        ruta = campos.get("Name", "").replace("\\", "/")
        if not ruta or campos.get("Type") != "File":
            continue
        tamano = _entero(campos.get("Size"))
        if tamano is None or not 0 <= tamano < _TAMANO_INCREIBLE:
            desconocidas.append(ruta)
            continue
        empacado = _entero(campos.get("Packed size")) or 0
        entradas.append(
            EntradaContenedor(
                ruta_interna=ruta,
                nombre=ruta.rsplit("/", 1)[-1],
                tamano=tamano,
                mtime_ns=_mtime_ns_iso(campos.get("mtime", "")),
            )
        )
        ratios.append(tamano / empacado if empacado > 0 else 0.0)
    return entradas, ratios, desconocidas


def _cerrar_listado_rar(
    perillas: PerillasFiltro,
    entradas: list[EntradaContenedor],
    ratios: list[float],
    desconocidas: list[str],
    inicio: float,
    formato: str,
    listador: str,
    *,
    con_error: bool = False,
) -> ResultadoExploracion:
    """Aislado por ratio, guards y `topado`, igual que en `_listar_con_7zz`. Una entrada de
    tamaño ilegible también deja la exploración `topada`: no se encola, y así la copia
    parcial se ve en la fila en vez de perderse en silencio. Un listado que el listador
    terminó con error (`con_error`, ver `_listar_con_unrar`) también queda `topado`."""
    if desconocidas:
        log.warning(
            "rar_tamano_ilegible",
            listador=listador,
            entradas=len(desconocidas),
            pista="unrar lista el tamaño real; lsar 1.10.1 lo lee mal por encima de 4 GiB",
        )
    entradas, aisladas = _aislar_ratio(entradas, ratios, perillas.t3_ratio_compresion_max)
    guard = _validar_guards(perillas, entradas, inicio, formato)
    return guard or ResultadoExploracion(
        True,
        None,
        tuple(entradas),
        formato,
        topado=aisladas > 0 or bool(desconocidas) or con_error,
        tamano_ilegible=len(desconocidas),
    )


def _listar_con_unrar(
    perillas: PerillasFiltro, ruta: str, inicio: float, formato: str
) -> ResultadoExploracion:
    """Lista un RAR con `unrar lt`. Timeout duro = guard_timeout.

    Un listado SIN archivos cuenta como corrupto, porque `unrar lt` sobre algo que no es
    un RAR sale con rc=0 y sin entradas (medido). Pasarlo por un contenedor vacío lo
    daría por explorado sin encolar nada.

    Con rc≠0 y archivos listados, se usa el listado y la exploración queda `topada`. Es
    el RAR truncado, habitual en descargas a medias: sobre un prefijo de 40 MB de
    'Matrix.rar', `unrar lt` sale con rc=1 y lista las 18 entradas, y `unrar x` recupera
    17 enteras. Darlo por corrupto hacía caer a `lsar`, que es otra herramienta. Así el
    explorador y el extractor usan siempre la misma y la entrada que falte sale como
    `extraccion_incompleta`.

    Si falla el NODO (señal, memoria, E/S) no se prueba otro listador, porque el archivo
    no ha dicho nada: `FalloDelNodo`, que se reintenta."""
    try:
        proc = subprocess.run(
            [_unrar_bin(), "lt", "-p-", "--", ruta],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=perillas.t3_timeout_s,
            env=_entorno_unrar(),
        )
    except subprocess.TimeoutExpired:
        return ResultadoExploracion(False, "guard_timeout", (), formato)
    if _rc_unrar_transitorio(proc.returncode):
        raise FalloDelNodo(f"unrar lt salió con {proc.returncode} (recursos del nodo)")
    entradas, ratios, desconocidas = _parse_lt_unrar(proc.stdout.decode("utf-8", "replace"))
    if not entradas and not desconocidas:
        return ResultadoExploracion(False, "contenedor_corrupto", (), formato)
    if proc.returncode != 0:
        log.warning(
            "rar_listado_con_error",
            archivo=ruta,
            rc=proc.returncode,
            entradas=len(entradas) + len(desconocidas),
            pista="RAR truncado o dañado: se encola lo listado y lo irrecuperable sale como "
            "extraccion_incompleta",
        )
    return _cerrar_listado_rar(
        perillas,
        entradas,
        ratios,
        desconocidas,
        inicio,
        formato,
        "unrar",
        con_error=proc.returncode != 0,
    )


_LSAR_CACHE: list[str | None] = []


def _lsar_bin() -> str:
    """Ruta a `lsar` (el listador de The Unarchiver, junto a `unar`). Cacheada."""
    if not _LSAR_CACHE:
        candidatos = ("/opt/homebrew/bin/lsar", "/usr/local/bin/lsar")
        _LSAR_CACHE.append(
            shutil.which("lsar") or next((c for c in candidatos if Path(c).exists()), None)
        )
    binario = _LSAR_CACHE[0]
    if binario is None:
        raise FileNotFoundError("lsar no encontrado (parte de unar: brew install unar)")
    return binario


def _parse_json_lsar(salida: str) -> tuple[list[EntradaContenedor], list[float], list[str]]:
    """Parsea `lsar --json`. Omite carpetas. El ratio sale de XADFileSize/XADCompressedSize
    y va ALINEADO con las entradas (0.0 = sin dato, nunca se aísla). Antes solo se añadía
    si había tamaño comprimido, y bastaba un archivo vacío para desalinear la lista, lo
    que dejaba sin aislar TODO el RAR (`_aislar_ratio` no aísla nada si no cuadra).

    Un XADFileSize ilegible o ≥ 2^62 (ver `_TAMANO_INCREIBLE`) NO es un tamaño, y esa
    entrada va a la tercera lista, fuera de las entradas. No hay tamaño con el que pueda
    entrar a la cola:
      - 0 la mata en T0 como `kill_t0:vacio`;
      - uno pequeño impide trocearla y se indexa una muestra;
      - el tamaño forma su `archivo_id`, así que uno inventado crea una identidad que el
        listado bueno no reconocerá.
    Tampoco puede contar en el ratio (antes se aislaba como bomba) ni en el guard de
    descomprimido (1,8e19 > 1 TiB manda a COLD el contenedor ENTERO). Con `unrar`, que es
    quien lista en la imagen, el tamaño sale bien (21.424.711.026 B) y esto no ocurre. Sin
    `unrar`, la entrada no se pierde en silencio: el contenedor queda `topado`, se
    registra `rar_tamano_ilegible` y, al extraer, cuenta como mala."""
    entradas: list[EntradaContenedor] = []
    ratios: list[float] = []
    desconocidas: list[str] = []
    try:
        datos = json.loads(salida)
    except ValueError:  # salida no-JSON (archivo ilegible)
        return entradas, ratios, desconocidas
    if not isinstance(datos, dict):
        return entradas, ratios, desconocidas
    for e in datos.get("lsarContents", []):
        if e.get("XADIsDirectory"):
            continue
        ruta = str(e.get("XADFileName") or "").replace("\\", "/")
        if not ruta:
            continue
        tamano = _entero(e.get("XADFileSize"))
        if tamano is None or not 0 <= tamano < _TAMANO_INCREIBLE:
            desconocidas.append(ruta)
            continue
        entradas.append(
            EntradaContenedor(
                ruta_interna=ruta,
                nombre=ruta.rsplit("/", 1)[-1],
                tamano=tamano,
                mtime_ns=_mtime_ns_iso(str(e.get("XADLastModificationDate") or "")),
            )
        )
        comp = _entero(e.get("XADCompressedSize")) or 0
        ratios.append(tamano / comp if comp > 0 else 0.0)
    return entradas, ratios, desconocidas


def _listar_con_lsar(
    perillas: PerillasFiltro, ruta: str, inicio: float, formato: str
) -> ResultadoExploracion:
    """Lista un RAR con lsar (The Unarchiver): decodifica RAR5, que 7-Zip rechaza."""
    try:
        proc = subprocess.run(
            [_lsar_bin(), "--json", "--", ruta],
            capture_output=True,
            timeout=perillas.t3_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ResultadoExploracion(False, "guard_timeout", (), formato)
    if proc.returncode != 0:
        return ResultadoExploracion(False, "contenedor_corrupto", (), formato)
    entradas, ratios, desconocidas = _parse_json_lsar(proc.stdout.decode("utf-8", "replace"))
    if not entradas and not desconocidas:
        return ResultadoExploracion(False, "contenedor_corrupto", (), formato)
    return _cerrar_listado_rar(perillas, entradas, ratios, desconocidas, inicio, formato, "lsar")


def _explorar_rar(perillas: PerillasFiltro, fuente: Path | IO[bytes]) -> ResultadoExploracion:
    """Lista un RAR con `unrar` si está, que es también quien lo extrae
    (`_dir_rar_extraido`). Si no, prueba 7-Zip (`7zz`, que solo sirve si trae el códec
    RAR: el del paquete Debian `7zip` no lo trae y `7zz l` sale con rc=2) y por último
    `lsar`.

    Se pasa a la siguiente herramienta solo si la anterior no está o da el archivo por
    corrupto. Un guard (timeout, entradas, bytes) es una respuesta, no un fallo de la
    herramienta, y un `FalloDelNodo` sube tal cual para reintentarse. Un archivo del
    filesystem se lista en sitio (sin copiar los GB); un RAR anidado se vuelca a un
    temporal."""
    inicio = time.monotonic()
    ruta, es_temporal = _ruta_temporal_de(fuente)
    try:
        primero: ResultadoExploracion | None = None
        for listar in (_listar_con_unrar, _listar_con_7zz, _listar_con_lsar):
            try:
                r = listar(perillas, ruta, inicio, "rar")
            except FileNotFoundError:
                continue
            if r.ok or r.motivo != "contenedor_corrupto":
                return r
            primero = primero or r
        if primero is None:
            raise FileNotFoundError("ningún listador de RAR disponible (unrar, 7zz, lsar)")
        return primero
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
    """Lista el contenedor y valida guards. NUNCA lanza por archivo hostil: devuelve flag.
    Solo lanza `FalloDelNodo`, que no habla del archivo y el precalificador reintenta."""
    explorador = _EXPLORADORES.get(tipo)
    if explorador is None and tipo.startswith("text/"):
        explorador = _explorar_texto  # text/x-c, text/x-php… cualquier text/* es troceable
    if explorador is None:
        return ResultadoExploracion(False, "formato_no_soportado", (), tipo)
    try:
        return explorador(perillas, fuente)
    except FalloDelNodo:
        # Convertido en `contenedor_corrupto`, el contenedor se quedaría preservado SIN
        # explorar, en HOT, hasta que alguien lo re-explore a mano. Y todo por un SIGTERM
        # al parar los workers.
        raise
    except (ImportError, FileNotFoundError) as exc:
        # Falta py7zr o el binario (7zz, unrar, lsar) → preservar íntegro sin explorar.
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
#: Entradas LISTADAS que la extracción no dejó enteras (JSON ruta → {declarado,
#: extraido}). Se escribe junto al marcador, antes del rename atómico, y
#: `_servir_desde_arbol` no sirve ninguna de ellas.
_ARCHIVO_MALAS = ".norm_malas"

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
    """Al salir borra SOLO los `.tmp.<pid>.<hilo>` a medias de ESTE proceso (extracciones
    que no llegaron al rename atómico). Los dirs COMPLETOS y compartidos SE QUEDAN: son la
    caché persistente que evita re-extraer en el próximo arranque."""
    with contextlib.suppress(OSError):
        for d in _CACHE_BASE.glob(f".*.tmp.{os.getpid()}.*"):
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


def _clave_persistente(ruta_fs: Path, etiqueta: str = "") -> str:
    """Hash determinista de (ruta resuelta, tamaño, mtime): el mismo contenedor cae SIEMPRE
    en el mismo dir, así que cualquier proceso lo reusa. Si el archivo cambia (mtime/tam),
    la clave cambia y se re-extrae — correcto.

    `etiqueta` mete en la clave la herramienta que extrajo el árbol, porque también
    forma parte de su identidad. La caché de 'Matrix.rar' la hizo `unar` y está marcada
    como completa con 49 entradas malas: reusarla serviría esas entradas malas para
    siempre. Sin etiqueta la clave es la de siempre, así que las cachés de 7z siguen
    valiendo."""
    st = ruta_fs.stat()
    material = f"{ruta_fs.resolve()}\0{st.st_size}\0{st.st_mtime_ns}"
    if etiqueta:
        material += f"\0{etiqueta}"
    return hashlib.sha256(material.encode()).hexdigest()


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
        # El marcador se va PRIMERO: mientras `rmtree` borra, quien busque una entrada ya
        # borrada debe ver un dir incompleto (reintentable), no una caché completa a la que
        # le falta la entrada (error permanente, ver `_servir_desde_arbol`).
        with contextlib.suppress(OSError):
            (d / _MARCADOR).unlink()
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


def _extraer_7z_con_unar(ruta_fs: Path, destino: Path) -> bool:
    """Extracción COMPLETA con The Unarchiver: respaldo de 7z para los filtros que py7zr
    no trae, y de RAR cuando falta `unrar`. Devuelve False si la extracción fue PARCIAL.

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
    extrajo_algo = any(destino.iterdir())
    if proc.returncode != 0:
        detalle = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:500]
        if not extrajo_algo:
            raise OSError(f"unar salió con {proc.returncode}: {detalle}")
        # PARCIAL: unar sale con rc≠0 en cuanto alguna entrada no decodifica. Aquí se
        # culpaba al archivo y era falso: 'Matrix.rar' está sano (`unrar t`: rc=0, 0
        # errores) y `unrar` extrae con el CRC correcto las 49 entradas que unar dejó a
        # 0 B o truncadas. Falla el decodificador RAR5 de unar: 1.10.1 y 1.10.8 fallan
        # en las mismas 48. Aun así el árbol NO se tira, porque las demás entradas están
        # bien. Lo que ya no se hace es darlo por bueno: `unar` también CREA los archivos
        # que no pudo sacar, vacíos o cortados, así que «las que falten fallarán luego»
        # no pasaba. Quien llama compara cada entrada con el listado (`_anotar_malas`),
        # y las que no miden lo declarado no se sirven.
        log.warning("unar_extraccion_parcial", archivo=str(ruta_fs), detalle=detalle)
        return False
    if not extrajo_algo:
        raise OSError("unar terminó con éxito pero no extrajo nada")
    return True


def _declarados(
    entradas: list[EntradaContenedor], desconocidas: list[str]
) -> dict[str, int | None]:
    """ruta → tamaño declarado por el listado (None = ilegible, ver `_parse_json_lsar`)."""
    declarados: dict[str, int | None] = {e.ruta_interna: e.tamano for e in entradas}
    declarados.update(dict.fromkeys(desconocidas))
    return declarados


#: Listar para extraer no pasa por las perillas: es el mismo tope que `t3_timeout_s`.
_TIMEOUT_LISTADO_S = 1800.0


def _listado_lsar(ruta_fs: Path) -> dict[str, int | None] | None:
    """Tamaños declarados según `lsar`, para verificar lo que dejó `unar`. None si no hay
    `lsar` o no puede listar el archivo."""
    try:
        proc = subprocess.run(
            [_lsar_bin(), "--json", "--", str(ruta_fs)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_LISTADO_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    entradas, _, desconocidas = _parse_json_lsar(proc.stdout.decode("utf-8", "replace"))
    return _declarados(entradas, desconocidas) if entradas or desconocidas else None


def _verificar_extraccion(
    raiz: Path, declarados: dict[str, int | None]
) -> dict[str, dict[str, int | None]]:
    """Compara cada entrada listada con lo que dejó el extractor en `raiz`. Es mala si no
    está, si no es un archivo regular (un enlace no se sigue), si no mide lo declarado o
    si no hay tamaño declarado con el que comprobarla."""
    malas: dict[str, dict[str, int | None]] = {}
    for ruta, declarado in declarados.items():
        try:
            st = (raiz / ruta).lstat()
        except OSError:
            malas[ruta] = {"declarado": declarado, "extraido": None}
            continue
        regular = stat.S_ISREG(st.st_mode)
        if declarado is None or not regular or st.st_size != declarado:
            malas[ruta] = {"declarado": declarado, "extraido": st.st_size if regular else None}
    return malas


def _anotar_malas(
    archivo: Path,
    destino: Path,
    declarados: dict[str, int | None] | None,
    *,
    completa: bool,
    via: str,
) -> None:
    """Deja en `destino` (antes del marcador) las entradas que la extracción no dejó
    enteras. Una extracción PARCIAL sin listado con el que separar lo entero de lo
    truncado no se acepta: sería servir a ciegas, que es justo el fallo que se cura."""
    if declarados is None:
        if completa:
            return
        raise ContenedorIlegible(
            f"extracción parcial de '{archivo.name}' con {via}, sin listado para verificarla"
        )
    _abrir_lectura_arbol(destino)  # un dir sin permisos haría parecer ausentes a sus hijos
    malas = _verificar_extraccion(destino, declarados)
    if not malas:
        return
    (destino / _ARCHIVO_MALAS).write_text(json.dumps(malas, ensure_ascii=False), encoding="utf-8")
    log.warning(
        "extraccion_incompleta",
        archivo=str(archivo),
        via=via,
        entradas_malas=len(malas),
        entradas_listadas=len(declarados),
        rc_cero=completa,
    )


def _entradas_malas(dir_ex: Path) -> dict[str, dict[str, int | None]]:
    try:
        malas: dict[str, dict[str, int | None]] = json.loads(
            (dir_ex / _ARCHIVO_MALAS).read_text(encoding="utf-8")
        )
    except FileNotFoundError:  # extracción sin entradas malas, o anterior a verificar
        return {}
    return malas


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
    # `-mmt1` = UN SOLO HILO. Medido en los .7z de INE (`vps-storage-01`): la
    # descompresión multi-hilo por defecto aloja un buffer/diccionario POR HILO y la
    # RAM del proceso trepa a ~10 GiB (8 hilos), que sumado al page-cache de escritura
    # OOM-mata el contenedor. Con un hilo el proceso se queda en ~0,5 GiB —el resto es
    # cache de disco reclamable— y la extracción es igual de rápida (está limitada por
    # el I/O de disco, no por la CPU de descompresión).
    proc = subprocess.run(
        [_7zz_bin(), "x", "-y", "-bd", "-mmt1", f"-o{destino}", "--", str(ruta_fs)],
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

    def _via_unar(rf: Path, dst: Path) -> None:
        # Solo se lista si hubo parcial: listar un 7z de 500.000 entradas no es gratis, y
        # 7zz y py7zr no dan un árbol parcial por bueno.
        if _extraer_7z_con_unar(rf, dst) is False:
            _anotar_malas(rf, dst, _listado_lsar(rf), completa=False, via="unar")

    intentos = (("7zz", _extraer_7z_con_7zz), ("unar", _via_unar), ("py7zr", _via_py7zr))
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


_CANDADOS_HILO: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def _candado_extraccion(clave: str) -> Iterator[None]:
    """Una sola extracción por clave a la vez, entre hilos y entre procesos.

    Sin esto, cada proceso que no ve el marcador extrae el contenedor ENTERO en su propio
    temporal, y `_exigir_disco_libre` se lo aprueba a todos a la vez porque nadie ha
    escrito aún: N workers escriben N veces 53,5 GB para 'Matrix.rar'. Así arranca justo su
    reproceso, porque al pasar a unrar la clave cambia y no hay caché. Quien esperaba
    vuelve a mirar el marcador al entrar y usa lo que dejó el otro.

    `flock` va por descripción de archivo abierto, así que separa también hilos de un
    mismo proceso. El candado de hilos cubre Windows (dev/tests), donde no hay `flock`."""
    with _CACHE_7Z_LOCK:
        entre_hilos = _CANDADOS_HILO.setdefault(clave, threading.Lock())
    with entre_hilos:
        if sys.platform == "win32":
            yield
            return
        import fcntl

        _CACHE_BASE.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_BASE / f".{clave}.lock", "ab") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # se suelta al cerrar
            yield


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
        with _candado_extraccion(clave):
            if not (destino / _MARCADOR).exists():  # otro la terminó mientras se esperaba
                _extraer_a_persistente(ruta_fs, clave, destino)
    _tocar(destino)
    with _CACHE_7Z_LOCK:
        _CACHE_7Z[clave] = destino
    return destino


def _hay_algun_archivo(raiz: Path) -> bool:
    """Para en el primer archivo: no hace falta recorrer los 53,5 GB de 'Matrix.rar'."""
    return any(archivos for _, _, archivos in os.walk(raiz))


def _extraer_rar_con_unrar(ruta_fs: Path, destino: Path) -> bool:
    """Extracción COMPLETA con `unrar` oficial. Devuelve False si fue PARCIAL: quien llama
    compara entonces cada entrada con el listado (`_anotar_malas`).

    `x` extrae con rutas, `-o+` sobrescribe sin preguntar, `-y` responde sí a todo,
    `-idq` es el modo silencioso (los errores siguen saliendo por stderr), `-p-` no pide
    contraseña (sin él, un RAR cifrado se queda esperando en stdin) y `--` cierra las
    opciones. El destino acaba en separador para que unrar lo trate como carpeta y no
    como máscara de nombres.

    Un rc≠0 no tira el árbol. Medido con unrar 6.21: sobre un RAR5 con 1 byte dañado en
    los datos de una entrada, rc=3 y las otras 3 de 4 idénticas byte a byte; sobre un
    prefijo de 40 MB de 'Matrix.rar' (RAR truncado), rc=3 y 17 de 18 enteras. Tirarlo
    entero dejaba 0 de 18 servidas, y unar al menos servía 14 (sin avisar de las otras 4).
    Lo roto no se cuela: sin `-kb`, unrar BORRA el archivo cuyo CRC no cuadra (el
    destructor de `File` elimina el que no llegó a cerrarse, extract.cpp), así que sale
    como ausente aunque mida lo declarado.

    Es fallo del RAR entero solo si no dejó ni un archivo: `ContenedorIlegible`. Si falla
    el nodo, `FalloDelNodo` sin mirar el árbol, que se tira y se reintenta: un unrar
    muerto a medias no dice nada de lo que no llegó a escribir."""
    proc = subprocess.run(
        [
            _unrar_bin(),
            "x",
            "-o+",
            "-y",
            "-idq",
            "-p-",
            "--",
            str(ruta_fs),
            os.path.join(str(destino), ""),
        ],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=_entorno_unrar(),
    )
    if proc.returncode == 0 and _hay_algun_archivo(destino):
        return True
    detalle = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:500]
    if _rc_unrar_transitorio(proc.returncode):
        raise FalloDelNodo(f"unrar salió con {proc.returncode} (recursos del nodo): {detalle}")
    if not _hay_algun_archivo(destino):
        raise ContenedorIlegible(
            f"unrar salió con {proc.returncode} sin extraer ningún archivo: {detalle}"
        )
    log.warning(
        "unrar_extraccion_parcial", archivo=str(ruta_fs), rc=proc.returncode, detalle=detalle
    )
    return False


def _listado_unrar(ruta_fs: Path) -> dict[str, int | None]:
    """Tamaños declarados según `unrar lt`, con los que se reserva disco y se verifica la
    extracción.

    Con rc≠0 y archivos listados se usa lo listado, igual que al explorar
    (`_listar_con_unrar`): es el RAR truncado, y lo que no se recupere sale como mala. Si
    no lista ningún archivo, `unrar` tampoco lo va a extraer, y eso es permanente.

    Si falla el nodo, `FalloDelNodo`. Antes todo rc≠0 era permanente, y con la marca de
    fallo un SIGTERM al parar los workers dejaba en ERROR cada entrada del RAR durante
    6 h sin reintentar nada (reproducido con rc=-15)."""
    try:
        proc = subprocess.run(
            [_unrar_bin(), "lt", "-p-", "--", str(ruta_fs)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_LISTADO_S,
            env=_entorno_unrar(),
        )
    except subprocess.TimeoutExpired as exc:
        raise FalloDelNodo(f"unrar lt no terminó en {_TIMEOUT_LISTADO_S:.0f} s") from exc
    if _rc_unrar_transitorio(proc.returncode):
        raise FalloDelNodo(f"unrar lt salió con {proc.returncode} (recursos del nodo)")
    entradas, _, desconocidas = _parse_lt_unrar(proc.stdout.decode("utf-8", "replace"))
    if not (entradas or desconocidas):
        detalle = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[-300:]
        raise ContenedorIlegible(
            f"unrar no puede listar '{ruta_fs.name}' (rc={proc.returncode}): {detalle}"
        )
    return _declarados(entradas, desconocidas)


#: Una extracción de RAR hecha con unrar no es la misma caché que una hecha con unar: la
#: etiqueta entra en la clave (ver `_clave_persistente`). Subir la versión invalida las
#: cachés de esa herramienta.
_ETIQUETAS_RAR = {True: "rar-unrar-v1", False: "rar-unar-v2"}

#: Tras un fallo PERMANENTE al extraer un RAR, el resto de sus entradas falla en el acto
#: en vez de extraerlo otra vez, porque cada una volvería a extraerlo ENTERO para fallar
#: igual. En 'Matrix.rar' son 1132 entradas, y solo `unrar t` ya tarda 310 s: más de 90 h
#: de disco al máximo sin avanzar. Es el patrón del 7z BCJ2, que escribió 104 GB en
#: reintentos. Solo llega aquí el RAR del que no sale NADA (no lista ningún archivo o no
#: se extrae ninguno): uno dañado a medias deja una caché completa con sus entradas malas
#: anotadas, que tampoco se re-extrae. La marca caduca sola, así que un reproceso tras
#: arreglar la causa no depende de que alguien la borre.
_FALLO_TTL_S = 6 * 3600


def _marca_fallo(clave: str) -> Path:
    return _CACHE_BASE / f".{clave}.fallo"


def _fallo_reciente(clave: str) -> str | None:
    marca = _marca_fallo(clave)
    try:
        if time.time() - marca.stat().st_mtime <= _FALLO_TTL_S:
            return marca.read_text(encoding="utf-8", errors="replace")
        marca.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _anotar_fallo(clave: str, motivo: str) -> None:
    with contextlib.suppress(OSError):
        _CACHE_BASE.mkdir(parents=True, exist_ok=True)
        _marca_fallo(clave).write_text(motivo[:2000], encoding="utf-8")


def _retirar_caches_rar_viejas(ruta_fs: Path, vigente: str) -> None:
    """Borra las extracciones de ESTE RAR que quedaron bajo otra clave: la de antes de
    verificar (sin etiqueta y hecha con unar) y la del otro extractor. Ya no se van a
    servir. Sin esto se quedan en disco hasta que el LRU llegue al tope, que es la mitad
    del disco libre y en la práctica no llega nunca: en Matrix son 32 GB muertos. Se
    borran DESPUÉS de tener la nueva completa, nunca antes."""
    for etiqueta in ("", *_ETIQUETAS_RAR.values()):
        clave = _clave_persistente(ruta_fs, etiqueta)
        if clave == vigente:
            continue
        with _CACHE_7Z_LOCK:
            _CACHE_7Z.pop(clave, None)
        vieja = _CACHE_BASE / clave
        if not vieja.is_dir():
            continue
        with contextlib.suppress(OSError):
            (vieja / _MARCADOR).unlink()
        shutil.rmtree(vieja, ignore_errors=True)
        log.warning("cache_rar_retirada", archivo=str(ruta_fs), dir=vieja.name)


def _dir_rar_extraido(ruta_fs: Path) -> Path:
    """Análogo a `_dir_7z_extraido` pero para RAR: extrae el archivo COMPLETO una sola
    vez (caché persistente compartida) y sirve las entradas desde ahí — O(N), no O(N²).

    En un RAR SOLID, sacar una entrada suelta obliga a descomprimir desde el principio,
    así que pedirlas una a una es cuadrático. Ojo: 'Matrix.rar' NO es sólido
    (XADIsSolid=0 en sus 1133 archivos), y el «ni una entrada en 45 min» que se atribuía
    aquí a la solidez no se reprodujo: unar sacó 58 entradas sueltas en menos de 15 s. La
    caché sigue haciendo falta para los RAR sólidos y porque cada lote de texto de 64 KB
    no puede pagar la extracción de su entrada.

    Extrae con `unrar` si está, verifica cada entrada contra el listado de la MISMA
    herramienta (las que no miden lo declarado no se sirven) y reserva disco por el total
    DECLARADO. Sin `unrar` usa `unar`, que puede dejar un árbol parcial: solo se acepta si
    `lsar` permite verificarlo. Comparte la caché en disco de los `.7z` (mismo cupo, mismo
    LRU); la clave lleva la herramienta. Una sola extracción por clave a la vez
    (`_candado_extraccion`).
    """
    con_unrar = _hay_unrar()
    clave = _clave_persistente(ruta_fs, _ETIQUETAS_RAR[con_unrar])
    with _CACHE_7Z_LOCK:
        memo = _CACHE_7Z.get(clave)
        if memo is not None and (memo / _MARCADOR).exists():
            return memo

    destino = _CACHE_BASE / clave
    if not (destino / _MARCADOR).exists():
        with _candado_extraccion(clave):
            if not (destino / _MARCADOR).exists():  # otro la terminó mientras se esperaba
                _extraer_rar_una_vez(ruta_fs, clave, destino, con_unrar=con_unrar)
    _tocar(destino)
    with _CACHE_7Z_LOCK:
        _CACHE_7Z[clave] = destino
    return destino


def _extraer_rar_una_vez(ruta_fs: Path, clave: str, destino: Path, *, con_unrar: bool) -> None:
    """Dentro del candado de la clave: respeta la marca de un fallo reciente, extrae y
    verifica, y retira las cachés viejas de este RAR cuando la nueva está completa."""
    previo = _fallo_reciente(clave)
    if previo is not None:
        raise ContenedorIlegible(
            f"{previo} [fallo reciente de este RAR: no se re-extrae hasta pasadas "
            f"{_FALLO_TTL_S // 3600} h]"
        )
    try:
        _extraer_rar_verificado(ruta_fs, clave, destino, con_unrar=con_unrar)
    except ContenedorIlegible as exc:
        _anotar_fallo(clave, str(exc))
        raise
    _marca_fallo(clave).unlink(missing_ok=True)
    _retirar_caches_rar_viejas(ruta_fs, clave)


def _extraer_rar_verificado(ruta_fs: Path, clave: str, destino: Path, *, con_unrar: bool) -> None:
    if con_unrar:
        declarados: dict[str, int | None] | None = _listado_unrar(ruta_fs)
        extraer: Callable[[Path, Path], bool | None] = _extraer_rar_con_unrar
    else:
        declarados = _listado_lsar(ruta_fs)
        extraer = _extraer_7z_con_unar
    via = "unrar" if con_unrar else "unar"

    def _extraer_y_verificar(rf: Path, dst: Path) -> None:
        completa = extraer(rf, dst) is not False
        _anotar_malas(rf, dst, declarados, completa=completa, via=via)

    # Se reserva por el total DECLARADO: tamaño x4 da 24,8 GB para Matrix, que descomprime
    # 53,5 GB. Sin listado queda la estimación de siempre.
    reservar = sum(t or 0 for t in declarados.values()) if declarados else None
    _extraer_a_persistente(ruta_fs, clave, destino, _extraer_y_verificar, reservar=reservar)


#: Lo que una extracción deja libre en el disco de la caché, como fracción del total.
_FRACCION_DISCO_INTOCABLE = 0.05


def _exigir_disco_libre(ruta_fs: Path, necesarios: int) -> None:
    """Falla ANTES de escribir si lo declarado no cabe en el disco de la caché dejando
    libre `_FRACCION_DISCO_INTOCABLE`. El guard de 1 TiB y el de ratio solo deciden qué
    se ENCOLA. La extracción escribe el archivo entero, y el LRU solo desaloja lo que
    hay en caché, no crea sitio que no existe."""
    try:
        uso = shutil.disk_usage(_CACHE_BASE)
    except OSError:  # pragma: no cover - sin poder medir, se sigue como antes
        return
    intocable = int(uso.total * _FRACCION_DISCO_INTOCABLE)
    if necesarios + intocable > uso.free:
        raise DiscoInsuficiente(
            f"disco_insuficiente: extraer '{ruta_fs.name}' escribe {necesarios} B declarados"
            f" y en {_CACHE_BASE} hay {uso.free} B libres, de los que {intocable} B no se tocan"
        )


def _extraer_a_persistente(
    ruta_fs: Path,
    clave: str,
    destino: Path,
    extractor: Callable[[Path, Path], None] | None = None,
    *,
    reservar: int | None = None,
) -> None:
    """Extrae a un tmp propio y lo renombra ATÓMICAMENTE a `destino`. Si otro proceso ganó
    la carrera (el destino ya tiene marcador), descarta su tmp y usa el del otro. Un
    `destino` sin marcador es una extracción muerta de un proceso anterior → se rehace.

    El marcador y el tamaño se escriben DENTRO del tmp antes del rename, así que un
    `destino` renombrado está SIEMPRE completo: ningún proceso ve una extracción a medias.

    `extractor` es el paso real (7z, rar…) — la caché en sí (clave, marcador, rename
    atómico, evicción LRU) es la misma para cualquier formato de contenedor completo.
    Por omisión se resuelve AQUÍ, al llamar (no como valor por defecto del parámetro,
    que Python liga al definir la función y congelaría la referencia).

    `reservar` es el total DECLARADO por el listado, si lo hay. Entonces además se
    exige que quepa en el disco (`_exigir_disco_libre`). Sin él se estima tamaño x4,
    que solo guía al LRU.
    """
    if extractor is None:
        extractor = _extraer_7z_a_disco
    _CACHE_BASE.mkdir(parents=True, exist_ok=True)
    marcador = destino / _MARCADOR
    if destino.exists() and not marcador.exists():
        shutil.rmtree(destino, ignore_errors=True)
    if reservar is None:
        _evictar_si_hace_falta(reservar=ruta_fs.stat().st_size * 4)
    else:
        _evictar_si_hace_falta(reservar=reservar)
        _exigir_disco_libre(ruta_fs, reservar)
    # El hilo va en el nombre: con un solo worker, el hilo del precalificador y el del
    # worker comparten pid, y uno podía borrarle al otro el árbol a medio extraer.
    tmp = _CACHE_BASE / f".{clave}.tmp.{os.getpid()}.{threading.get_ident()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        extractor(ruta_fs, tmp)
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


def _servir_desde_arbol(dir_ex: Path, entrada: str, limite: int, formato: str) -> IO[bytes]:
    """Abre EN SITIO una entrada ya extraída en la caché, sin copiarla.

    Antes se copiaba entera a un spool en cada petición. Con los lotes de texto eso es
    catastrófico: un lote de 64 KB de un `.sql` de 1,6 GB copiaba los 1,6 GB — por
    lote. Medido en la matriz ('Matrix.rar', 412 `.sql`, 29 GB): ~30 TB escritos en 4 h,
    disco al 75 % de presión y la corrida parada. El archivo de la caché no cambia
    mientras se lee (se escribe entero antes del rename atómico), así que el tope se
    comprueba exacto con `stat`, sin copiar para contar.

    Una entrada que la extracción no dejó entera NO se sirve: `ExtraccionIncompleta`,
    permanente y con el motivo `extraccion_incompleta` a la vista. Tampoco se sirve
    una que falta de un árbol COMPLETO, porque reintentar lee el mismo árbol. Solo si el
    marcador ya no está (el LRU lo está desalojando) es un `OSError` reintentable."""
    mala = _entradas_malas(dir_ex).get(entrada)
    if mala is not None:
        extraido = mala.get("extraido")
        # Sin archivo es lo normal con unrar: borra la entrada cuyo CRC no cuadra.
        dejo = "no la dejó" if extraido is None else f"dejó {extraido} B"
        raise ExtraccionIncompleta(
            f"extraccion_incompleta: la entrada '{entrada}' del {formato} declara "
            f"{mala.get('declarado')} B y la extracción {dejo}"
        )
    origen_fs = dir_ex / entrada
    if not origen_fs.is_file():
        if (dir_ex / _MARCADOR).exists():
            raise ExtraccionIncompleta(
                f"extraccion_incompleta: entrada no encontrada en {formato}: {entrada}"
            )
        raise OSError(f"entrada no encontrada en {formato} (caché desalojada): {entrada}")
    if origen_fs.stat().st_size > limite:
        raise ContenedorInseguro(f"entrada '{entrada}' excede el límite ({limite} B)")
    return origen_fs.open("rb")


def _paso_7z(
    fobj: IO[bytes], entrada: str, umbral: int, limite: int, *, ruta_fs: Path | None = None
) -> IO[bytes]:
    # 7z SÓLIDO: sacar una entrada descomprime el bloque entero. Si el 7z es un
    # archivo del filesystem, se extrae COMPLETO una vez (cacheado) y la entrada se
    # sirve como lectura de disco → O(N) en lugar de O(N²) (ver _dir_7z_extraido).
    if ruta_fs is not None:
        return _servir_desde_arbol(_dir_7z_extraido(ruta_fs), entrada, limite, "7z")

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
    """Sirve UNA entrada de un RAR. Con `ruta_fs` (archivo real en disco, el caso
    normal): extrae el RAR COMPLETO una sola vez a la caché persistente compartida
    (`_dir_rar_extraido`), verificado contra el listado, y sirve desde ahí — evita el
    O(n²) de extraer entrada por entrada en un RAR SOLID. Sin `ruta_fs` (RAR anidado
    dentro de otro contenedor, sin ruta propia): fallback por-entrada con `unar`, como
    antes."""
    if ruta_fs is not None:
        return _servir_desde_arbol(_dir_rar_extraido(ruta_fs), entrada, limite, "rar")

    # RAR anidado (stream, sin ruta en disco): fallback por-entrada con `unar`.
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


def _ruta_si_es_archivo_real(fobj: IO[bytes]) -> Path | None:
    """Ruta en disco del paso recién servido, si es un archivo REAL (una entrada abierta
    en sitio desde la caché), no un spool. Así el paso siguiente —el lote de texto/CSV,
    o una SQLite— lee en sitio en vez de sobre una copia o un temporal materializado."""
    nombre = getattr(fobj, "name", None)
    if isinstance(fobj, io.BufferedReader) and isinstance(nombre, str):
        ruta = Path(nombre)
        if ruta.is_file():
            return ruta
    return None


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
            ruta_fs_actual = _ruta_si_es_archivo_real(fobj)
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
