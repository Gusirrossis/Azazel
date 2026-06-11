"""Tests del generador de disco sintético: determinismo y casos hostiles presentes."""

from __future__ import annotations

import zipfile
from pathlib import Path

from normalizacion.herramientas.generador_disco import generar_disco, hash_arbol


class TestDeterminismo:
    def test_misma_semilla_mismo_arbol(self, tmp_path: Path) -> None:
        """INVARIANTE de las pruebas: el disco es reproducible byte a byte."""
        a, b = tmp_path / "a", tmp_path / "b"
        generar_disco(a, semilla=42)
        generar_disco(b, semilla=42)
        assert hash_arbol(a) == hash_arbol(b)

    def test_semilla_distinta_arbol_distinto(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        generar_disco(a, semilla=42)
        generar_disco(b, semilla=43)
        assert hash_arbol(a) != hash_arbol(b)

    def test_manifiesto_estable(self, tmp_path: Path) -> None:
        manifiesto = generar_disco(tmp_path / "d", semilla=42)
        assert manifiesto["archivos"] == 20
        assert manifiesto["contenedores"] == 2


class TestCasosHostiles:
    def test_la_bomba_viola_el_guard_de_ratio(self, disco_sintetico: Path) -> None:
        """La zip-bomb de juguete debe disparar el guard K4 (ratio > 100:1)."""
        with zipfile.ZipFile(disco_sintetico / "contenedores" / "bomba.zip") as zf:
            info = zf.infolist()[0]
            ratio = info.file_size / max(info.compress_size, 1)
        assert ratio > 100.0

    def test_extension_mentirosa_es_csv(self, disco_sintetico: Path) -> None:
        """vacaciones.jpg NO es una imagen: su contenido es tabular (T1 no debe creerle)."""
        head = (disco_sintetico / "fotos" / "vacaciones.jpg").read_bytes()[:64]
        assert head.startswith(b"id,fecha,cliente,monto,moneda")
        assert not head.startswith(b"\xff\xd8")

    def test_duplicados_son_identicos(self, disco_sintetico: Path) -> None:
        a = (disco_sintetico / "duplicados" / "copia_a.csv").read_bytes()
        b = (disco_sintetico / "duplicados" / "sub" / "copia_b.csv").read_bytes()
        assert a == b

    def test_cajas_dentro_de_cajas(self, disco_sintetico: Path) -> None:
        """El contenedor anidado existe: zip → zip → csv (lo recorrerá T3 en BFS)."""
        with zipfile.ZipFile(disco_sintetico / "contenedores" / "cajas.zip") as exterior:
            assert "caja2.zip" in exterior.namelist()

    def test_archivo_de_cero_bytes_presente(self, disco_sintetico: Path) -> None:
        assert (disco_sintetico / "basura" / "vacio.dat").stat().st_size == 0
