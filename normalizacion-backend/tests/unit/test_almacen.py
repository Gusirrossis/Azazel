"""Tests del almacén content-addressed (backend local, sin servicios)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.modelo import clave_almacen


def _hash(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


class TestAlmacenLocal:
    def test_roundtrip_guardar_leer(self, tmp_path: Path) -> None:
        almacen = AlmacenLocal(tmp_path)
        datos = b"contenido de prueba" * 100
        h = _hash(datos)
        almacen.guardar(h, io.BytesIO(datos), len(datos))
        assert almacen.existe(h)
        with almacen.leer(h) as f:
            assert f.read() == datos

    def test_no_existe_antes_de_guardar(self, tmp_path: Path) -> None:
        almacen = AlmacenLocal(tmp_path)
        assert not almacen.existe(_hash(b"nada"))

    def test_estructura_content_addressed(self, tmp_path: Path) -> None:
        """El blob vive en ab/cd/abcd… — derivado del hash, no del nombre original."""
        almacen = AlmacenLocal(tmp_path)
        datos = b"x"
        h = _hash(datos)
        almacen.guardar(h, io.BytesIO(datos), 1)
        assert (tmp_path / clave_almacen(h)).is_file()

    def test_guardar_dos_veces_es_noop(self, tmp_path: Path) -> None:
        """Inmutable + dedup: re-guardar el mismo hash no toca el blob."""
        almacen = AlmacenLocal(tmp_path)
        datos = b"original"
        h = _hash(datos)
        almacen.guardar(h, io.BytesIO(datos), len(datos))
        almacen.guardar(h, io.BytesIO(b"IMPOSTOR"), 8)  # mismo hash declarado
        with almacen.leer(h) as f:
            assert f.read() == datos

    def test_sin_temporales_huerfanos(self, tmp_path: Path) -> None:
        almacen = AlmacenLocal(tmp_path)
        datos = b"abc" * 1000
        almacen.guardar(_hash(datos), io.BytesIO(datos), len(datos))
        assert not list(tmp_path.rglob("*.tmp"))
