"""Almacén permanente content-addressed — el componente que garantiza que el dato sobreviva.

Diseño (PROPUESTA §7): clave = sha256(bytes) en estructura ab/cd/abcd…, blobs INMUTABLES,
deduplicación nativa (si el hash ya existe, no se vuelve a copiar). La interfaz es
agnóstica a la tecnología (MinIO/Ceph/ZFS por definir): cambiar de backend no toca
el pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Protocol

from normalizacion.core.config import Config
from normalizacion.core.modelo import clave_almacen


class Almacen(Protocol):
    """Contrato mínimo del almacén (PROPUESTA §7.4)."""

    def existe(self, hash_contenido: str) -> bool:
        """¿El blob ya está guardado? (la pregunta del dedup)."""
        ...

    def guardar(self, hash_contenido: str, fuente: IO[bytes], tamano: int) -> None:
        """Persiste el blob en streaming. Idempotente: re-guardar el mismo hash es no-op."""
        ...

    def leer(self, hash_contenido: str) -> IO[bytes]:
        """Stream de lectura del blob (verificación y descargas de la API)."""
        ...


class AlmacenLocal:
    """Backend de directorio local: el content-addressed más simple posible.

    Para dev/tests y como referencia de la semántica. La escritura es vía archivo
    temporal + rename (un blob jamás queda a medias visible).
    """

    def __init__(self, raiz: Path) -> None:
        self._raiz = raiz
        raiz.mkdir(parents=True, exist_ok=True)

    def _ruta(self, hash_contenido: str) -> Path:
        return self._raiz / clave_almacen(hash_contenido)

    def existe(self, hash_contenido: str) -> bool:
        return self._ruta(hash_contenido).is_file()

    def guardar(self, hash_contenido: str, fuente: IO[bytes], tamano: int) -> None:
        destino = self._ruta(hash_contenido)
        if destino.is_file():  # inmutable + dedup: ya está, no se toca
            return
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(".tmp")
        with temporal.open("wb") as salida:
            while bloque := fuente.read(1024 * 1024):
                salida.write(bloque)
        temporal.replace(destino)

    def leer(self, hash_contenido: str) -> IO[bytes]:
        return self._ruta(hash_contenido).open("rb")


def crear_almacen(config: Config) -> Almacen:
    """Fábrica según config (`NORM_ALMACEN_BACKEND=minio|local`)."""
    if config.almacen_backend == "local":
        return AlmacenLocal(Path(config.almacen_local_raiz).expanduser())
    if config.almacen_backend == "minio":
        from .backend_minio import AlmacenMinio

        return AlmacenMinio(
            endpoint=config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            bucket=config.minio_bucket,
        )
    raise ValueError(f"backend de almacén desconocido: {config.almacen_backend}")
