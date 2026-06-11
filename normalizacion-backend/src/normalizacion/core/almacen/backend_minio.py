"""Backend MinIO/S3 del almacén permanente (intercambiable tras la interfaz)."""

from __future__ import annotations

from typing import IO, BinaryIO, cast

from minio import Minio
from minio.error import S3Error

from normalizacion.core.modelo import clave_almacen


class AlmacenMinio:
    """Content-addressed sobre un bucket S3 (MinIO en dev/staging)."""

    def __init__(
        self, *, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool = False
    ) -> None:
        self._cliente = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        if not self._cliente.bucket_exists(bucket):
            self._cliente.make_bucket(bucket)

    def existe(self, hash_contenido: str) -> bool:
        try:
            self._cliente.stat_object(self._bucket, clave_almacen(hash_contenido))
            return True
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise

    def guardar(self, hash_contenido: str, fuente: IO[bytes], tamano: int) -> None:
        if self.existe(hash_contenido):  # inmutable + dedup
            return
        self._cliente.put_object(
            self._bucket, clave_almacen(hash_contenido), cast(BinaryIO, fuente), length=tamano
        )

    def leer(self, hash_contenido: str) -> IO[bytes]:
        # La respuesta urllib3 cumple read()/close(); el caller la cierra
        return cast(
            IO[bytes], self._cliente.get_object(self._bucket, clave_almacen(hash_contenido))
        )
