"""Walker del catálogo (Fase 1): inventario rápido del disco SIN abrir archivos.

- `os.scandir` en BFS con cola explícita de directorios (nunca recursión — lección plaso).
- Solo `stat`: nombre, ruta, tamaño, mtime → señales T0 gratis.
- Inserta en lotes idempotentes; re-catalogar el mismo disco no duplica.
- Un error de permisos/IO en una carpeta NO detiene el catálogo: se cuenta y se sigue.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from normalizacion.core import cola
from normalizacion.core.config import Config
from normalizacion.core.modelo import calcular_archivo_id, ruta_canonica, sanear_texto
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("catalogo")


@dataclass(frozen=True)
class ResumenCatalogo:
    disco_id: str
    archivos_vistos: int
    archivos_nuevos: int
    directorios: int
    errores: int


def construir_fila(
    *,
    disco_id: str,
    ruta_relativa: str,
    nombre: str,
    tamano: int,
    mtime_ns: int,
    prioridad: int = 0,
) -> cola.FilaCatalogo:
    """Construye la fila T0 de un archivo. La identidad usa disco_id + ruta relativa,
    así el mismo disco montado en otro punto da el MISMO archivo_id (re-scan idempotente)."""
    ruta_rel = ruta_canonica(ruta_relativa)
    nombre = sanear_texto(nombre)  # nombres no-UTF8 (RAID con archivos Latin-1) → limpios
    sufijo = Path(nombre).suffix.lower()
    return cola.FilaCatalogo(
        archivo_id=calcular_archivo_id(f"{disco_id}:{ruta_rel}", tamano, mtime_ns),
        disco_id=disco_id,
        ruta=ruta_rel,
        nombre=nombre,
        extension=sufijo if sufijo else None,
        tamano=tamano,
        mtime=datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC),
        prioridad=prioridad,
    )


def catalogar_disco(config: Config, raiz: Path, disco_id: str | None = None) -> ResumenCatalogo:
    """Cataloga un disco completo: BFS + inserciones en lotes + total por disco."""
    raiz = raiz.resolve()
    if not raiz.is_dir():
        raise NotADirectoryError(f"{raiz} no es un directorio montado")
    id_disco = disco_id or raiz.name

    vistos = nuevos = directorios = errores = 0
    lote: list[cola.FilaCatalogo] = []
    pendientes: deque[Path] = deque([raiz])

    with psycopg.connect(config.postgres_dsn) as conn:
        cola.upsert_disco(conn, id_disco, str(raiz))
        conn.commit()

        def vaciar_lote() -> None:
            nonlocal nuevos
            nuevos += cola.insertar_pendientes(conn, lote)
            conn.commit()  # cada lote queda durable: matar el proceso no pierde lo insertado
            lote.clear()

        while pendientes:
            carpeta = pendientes.popleft()
            try:
                with os.scandir(carpeta) as entradas:
                    for entrada in entradas:
                        try:
                            if entrada.is_symlink():
                                continue
                            if entrada.is_dir(follow_symlinks=False):
                                pendientes.append(Path(entrada.path))
                                directorios += 1
                            elif entrada.is_file(follow_symlinks=False):
                                st = entrada.stat(follow_symlinks=False)
                                sufijo = Path(entrada.name).suffix.lower()
                                lote.append(
                                    construir_fila(
                                        disco_id=id_disco,
                                        ruta_relativa=str(Path(entrada.path).relative_to(raiz)),
                                        nombre=entrada.name,
                                        tamano=st.st_size,
                                        mtime_ns=st.st_mtime_ns,
                                        # Hint de PRIORIDAD: orden por extensión (txt,
                                        # 7z, rar, zip…) y comprimidos antes que el
                                        # resto (la extensión solo ordena, JAMÁS
                                        # decide el tipo)
                                        prioridad=config.filtro.prioridad_para_extension(
                                            sufijo
                                        ),
                                    )
                                )
                                vistos += 1
                                if len(lote) >= config.worker.lote_insercion:
                                    vaciar_lote()
                        except OSError as exc:
                            errores += 1
                            log.warning("entrada_ilegible", ruta=entrada.path, error=str(exc))
            except OSError as exc:
                errores += 1
                log.warning("carpeta_ilegible", ruta=str(carpeta), error=str(exc))

        vaciar_lote()
        total = cola.actualizar_total_disco(conn, id_disco)
        conn.commit()

    resumen = ResumenCatalogo(
        disco_id=id_disco,
        archivos_vistos=vistos,
        archivos_nuevos=nuevos,
        directorios=directorios,
        errores=errores,
    )
    log.info(
        "catalogo_completo",
        disco=id_disco,
        vistos=vistos,
        nuevos=nuevos,
        total_en_cola=total,
        errores=errores,
    )
    return resumen
