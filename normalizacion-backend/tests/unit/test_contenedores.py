"""Tests de T3 (puros, sin BD): listar sin extraer, guards anti zip-bomb, path specs."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from normalizacion.core.config import PerillasFiltro
from normalizacion.ingesta.precalificacion.contenedores import (
    ContenedorInseguro,
    abrir_entrada,
    explorar,
)

PERILLAS = PerillasFiltro()


def _zip(destino: Path, entradas: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        for nombre, datos in entradas.items():
            zf.writestr(nombre, datos)
    return destino


class TestExplorar:
    def test_lista_sin_extraer(self, tmp_path: Path) -> None:
        ruta = _zip(tmp_path / "c.zip", {"a.csv": b"x,y\n1,2\n", "sub/b.txt": b"hola"})
        r = explorar(PERILLAS, ruta, "application/zip")
        assert r.ok
        assert {e.ruta_interna for e in r.entradas} == {"a.csv", "sub/b.txt"}
        assert {e.nombre for e in r.entradas} == {"a.csv", "b.txt"}
        assert all(e.tamano > 0 for e in r.entradas)

    def test_zip_bomb_dispara_guard_ratio(self, tmp_path: Path) -> None:
        """INVARIANTE (DoD F1.5): la bomba produce un FLAG, nunca un crash."""
        ruta = _zip(tmp_path / "bomba.zip", {"relleno.bin": b"\x00" * (4 * 1024 * 1024)})
        r = explorar(PERILLAS, ruta, "application/zip")
        assert not r.ok
        assert r.motivo == "guard_ratio"
        assert r.entradas == ()

    def test_guard_de_entradas_maximas(self, tmp_path: Path) -> None:
        perillas = PerillasFiltro(t3_entradas_max=3)
        ruta = _zip(tmp_path / "m.zip", {f"f{i}.txt": b"abcdefgh" for i in range(4)})
        r = explorar(perillas, ruta, "application/zip")
        assert not r.ok and r.motivo == "guard_entradas"

    def test_guard_de_descomprimido_total(self, tmp_path: Path) -> None:
        perillas = PerillasFiltro(t3_descomprimido_max_bytes=100)
        ruta = _zip(tmp_path / "g.zip", {"a.txt": b"un texto cualquiera mayor a cien bytes " * 5})
        r = explorar(perillas, ruta, "application/zip")
        assert not r.ok and r.motivo == "guard_descomprimido"

    def test_corrupto_es_flag_no_excepcion(self, tmp_path: Path) -> None:
        ruta = tmp_path / "roto.zip"
        ruta.write_bytes(b"PK\x03\x04" + b"\xde\xad\xbe\xef" * 10)
        r = explorar(PERILLAS, ruta, "application/zip")
        assert not r.ok and r.motivo == "contenedor_corrupto"

    def test_formato_no_soportado_se_preserva(self, tmp_path: Path) -> None:
        """Imágenes de disco: aún sin exploración interna → se preservan íntegras."""
        ruta = tmp_path / "maquina.vhdx"
        ruta.write_bytes(b"vhdxfile" + b"\x00" * 1024)
        r = explorar(PERILLAS, ruta, "application/x-vhdx")
        assert not r.ok and r.motivo == "formato_no_soportado"


class TestSieteZip:
    """Decisión del usuario: los comprimidos son prioritarios → 7z explorable."""

    def _crear_7z(self, destino: Path, entradas: dict[str, bytes]) -> Path:
        import py7zr

        with py7zr.SevenZipFile(destino, "w") as sz:
            for nombre, datos in entradas.items():
                sz.writestr(datos, nombre)
        return destino

    def test_lista_entradas_de_7z(self, tmp_path: Path) -> None:
        ruta = self._crear_7z(
            tmp_path / "c.7z", {"a.csv": b"x,y\n1,2\n", "docs/b.txt": b"hola mundo"}
        )
        r = explorar(PERILLAS, ruta, "application/x-7z-compressed")
        assert r.ok
        assert {e.ruta_interna for e in r.entradas} == {"a.csv", "docs/b.txt"}
        assert r.formato == "7z"

    def test_abrir_entrada_de_7z(self, tmp_path: Path) -> None:
        contenido = b"col1,col2\n7,8\n"
        self._crear_7z(tmp_path / "c.7z", {"dato.csv": contenido})
        with abrir_entrada(
            tmp_path, ["c.7z", "dato.csv"], umbral_memoria=65_536, limite_bytes=1_000_000
        ) as f:
            assert f.read() == contenido

    def test_cadena_mixta_7z_dentro_de_zip(self, tmp_path: Path) -> None:
        """Multi-formato: un CSV dentro de un 7z dentro de un ZIP se resuelve paso a paso."""
        import io
        import zipfile as zf_mod

        contenido = b"a,b\n9,9\n"
        siete = tmp_path / "interno.7z"
        self._crear_7z(siete, {"dato.csv": contenido})
        with zf_mod.ZipFile(tmp_path / "caja.zip", "w") as zf:
            zf.writestr("interno.7z", siete.read_bytes())
        with abrir_entrada(
            tmp_path,
            ["caja.zip", "interno.7z", "dato.csv"],
            umbral_memoria=65_536,
            limite_bytes=1_000_000,
        ) as f:
            assert f.read() == contenido
        del io

    def test_entrada_7z_que_excede_limite(self, tmp_path: Path) -> None:
        import pytest as pt

        self._crear_7z(tmp_path / "g.7z", {"grande.bin": b"\x07" * 50_000})
        with pt.raises(ContenedorInseguro):
            abrir_entrada(
                tmp_path, ["g.7z", "grande.bin"], umbral_memoria=1024, limite_bytes=10_000
            )

    def test_guard_descomprimido_en_7z(self, tmp_path: Path) -> None:
        from normalizacion.core.config import PerillasFiltro

        self._crear_7z(tmp_path / "g.7z", {"a.bin": b"\x01" * 5000})
        r = explorar(
            PerillasFiltro(t3_descomprimido_max_bytes=100),
            tmp_path / "g.7z",
            "application/x-7z-compressed",
        )
        assert not r.ok and r.motivo == "guard_descomprimido"


class TestRar:
    """RAR se LISTA con 7-Zip (`7zz`) y se EXTRAE con `unar` — ambos binarios
    notarizados. Un RAR ilegible (truncado/hostil) se preserva íntegro vía flag,
    jamás se pierde ni revienta el proceso."""

    def test_rar5_truncado_se_marca_sin_reventar(self, tmp_path: Path) -> None:
        ruta = tmp_path / "x.rar"
        ruta.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64)  # rar5 truncado (no real)
        r = explorar(PERILLAS, ruta, "application/x-rar-compressed")
        # 7zz no lo acepta como archivo válido → flag, sin entradas, sin excepción.
        assert not r.ok
        assert r.entradas == ()
        assert r.formato == "rar"

    def test_rar_basura_jamas_revienta(self, tmp_path: Path) -> None:
        """INVARIANTE: archivo hostil → flag o listado vacío, NUNCA una excepción."""
        ruta = tmp_path / "roto.rar"
        ruta.write_bytes(b"Rar!\x1a\x07\x00" + b"\xde\xad\xbe\xef" * 16)  # rar4 basura
        r = explorar(PERILLAS, ruta, "application/x-rar-compressed")
        assert r.entradas == ()  # 7zz lo marca corrupto; nunca lanza


class TestTarYFlujos:
    """tar/gz/bz2/xz explorables con librería estándar (decisión del usuario:
    los comprimidos del servidor — tar.gz incluidos — se exploran por completo)."""

    def _crear_tar(self, destino: Path, entradas: dict[str, bytes], modo: str = "w") -> Path:
        import io
        import tarfile as tar_mod

        with tar_mod.open(destino, modo) as tf:  # type: ignore[call-overload]
            for nombre, datos in entradas.items():
                info = tar_mod.TarInfo(nombre)
                info.size = len(datos)
                tf.addfile(info, io.BytesIO(datos))
        return destino

    def test_tar_plano_se_lista(self, tmp_path: Path) -> None:
        ruta = self._crear_tar(tmp_path / "c.tar", {"a.csv": b"x,y\n1,2\n", "docs/b.txt": b"hola"})
        r = explorar(PERILLAS, ruta, "application/x-tar")
        assert r.ok
        assert {e.ruta_interna for e in r.entradas} == {"a.csv", "docs/b.txt"}
        assert r.formato == "tar"

    def test_tar_gz_se_lista_como_tar(self, tmp_path: Path) -> None:
        """Un .tar.gz llega tipado como gzip; el explorador descubre el tar adentro."""
        ruta = self._crear_tar(tmp_path / "c.tgz", {"datos/d.csv": b"a,b\n3,4\n"}, modo="w:gz")
        r = explorar(PERILLAS, ruta, "application/gzip")
        assert r.ok
        assert r.formato == "tar"
        assert r.entradas[0].ruta_interna == "datos/d.csv"

    def test_gz_simple_mide_el_contenido_exacto(self, tmp_path: Path) -> None:
        import gzip as gz_mod

        contenido = b"col1,col2\n" + b"9,9\n" * 500
        (tmp_path / "d.csv.gz").write_bytes(gz_mod.compress(contenido))
        r = explorar(PERILLAS, tmp_path / "d.csv.gz", "application/gzip")
        assert r.ok
        assert r.formato == "gz"
        assert r.entradas == (r.entradas[0],)
        assert r.entradas[0].ruta_interna == "contenido"
        assert r.entradas[0].tamano == len(contenido)  # medido, no ISIZE

    def test_abrir_entrada_de_tar_gz(self, tmp_path: Path) -> None:
        contenido = b"a,b\n5,6\n"
        self._crear_tar(tmp_path / "c.tgz", {"datos/d.csv": contenido}, modo="w:gz")
        with abrir_entrada(
            tmp_path, ["c.tgz", "datos/d.csv"], umbral_memoria=65_536, limite_bytes=1_000_000
        ) as f:
            assert f.read() == contenido

    def test_abrir_entrada_de_gz_simple(self, tmp_path: Path) -> None:
        import gzip as gz_mod

        contenido = b"INSERT INTO t VALUES (1);\n" * 20
        (tmp_path / "dump.sql.gz").write_bytes(gz_mod.compress(contenido))
        with abrir_entrada(
            tmp_path, ["dump.sql.gz", "contenido"], umbral_memoria=65_536, limite_bytes=1_000_000
        ) as f:
            assert f.read() == contenido

    def test_cadena_tar_dentro_de_zip(self, tmp_path: Path) -> None:
        contenido = b"x,y\n7,7\n"
        tar_interno = self._crear_tar(tmp_path / "interno.tar", {"d.csv": contenido})
        _zip(tmp_path / "caja.zip", {"interno.tar": tar_interno.read_bytes()})
        with abrir_entrada(
            tmp_path,
            ["caja.zip", "interno.tar", "d.csv"],
            umbral_memoria=65_536,
            limite_bytes=1_000_000,
        ) as f:
            assert f.read() == contenido

    def test_tar_vacio_es_ok_sin_entradas(self, tmp_path: Path) -> None:
        ruta = tmp_path / "vacio.tar"
        ruta.write_bytes(b"\x00" * 1024)  # dos bloques de cierre = tar vacío válido
        r = explorar(PERILLAS, ruta, "application/x-tar")
        assert r.ok and r.entradas == ()

    def test_guard_descomprimido_corta_el_flujo(self, tmp_path: Path) -> None:
        import gzip as gz_mod

        from normalizacion.core.config import PerillasFiltro

        (tmp_path / "g.gz").write_bytes(gz_mod.compress(b"\x00" * 50_000))
        r = explorar(
            PerillasFiltro(t3_descomprimido_max_bytes=1000), tmp_path / "g.gz", "application/gzip"
        )
        assert not r.ok and r.motivo == "guard_descomprimido"


class TestAbrirEntrada:
    def test_cadena_anidada_dos_niveles(self, tmp_path: Path) -> None:
        """Path spec estilo plaso: caja.zip → interna.zip → dato.csv."""
        contenido = b"col1,col2\n7,8\n"
        interna = io.BytesIO()
        with zipfile.ZipFile(interna, "w") as zf:
            zf.writestr("dato.csv", contenido)
        _zip(tmp_path / "caja.zip", {"interna.zip": interna.getvalue()})

        with abrir_entrada(
            tmp_path,
            ["caja.zip", "interna.zip", "dato.csv"],
            umbral_memoria=65_536,
            limite_bytes=10_000_000,
        ) as f:
            assert f.read() == contenido

    def test_limite_duro_corta_la_materializacion(self, tmp_path: Path) -> None:
        """Defensa en profundidad: aunque el listado mienta, el copiado tiene tope."""
        _zip(tmp_path / "caja.zip", {"grande.bin": b"\x07" * 50_000})
        with pytest.raises(ContenedorInseguro):
            abrir_entrada(
                tmp_path,
                ["caja.zip", "grande.bin"],
                umbral_memoria=1024,
                limite_bytes=10_000,
            )

    def test_cadena_irresoluble_es_oserror(self, tmp_path: Path) -> None:
        _zip(tmp_path / "caja.zip", {"a.txt": b"x"})
        with pytest.raises(OSError):
            abrir_entrada(
                tmp_path,
                ["caja.zip", "no_existe.txt"],
                umbral_memoria=1024,
                limite_bytes=10_000,
            )
