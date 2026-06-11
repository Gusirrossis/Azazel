"""Tests unitarios del walker: construcción de filas T0 (sin base de datos)."""

from __future__ import annotations

from normalizacion.ingesta.catalogo.walker import construir_fila


class TestConstruirFila:
    def test_extension_en_minusculas_con_punto(self) -> None:
        fila = construir_fila(
            disco_id="d1",
            ruta_relativa="Datos/VENTAS.CSV",
            nombre="VENTAS.CSV",
            tamano=10,
            mtime_ns=1,
        )
        assert fila.extension == ".csv"

    def test_sin_extension_es_none(self) -> None:
        fila = construir_fila(
            disco_id="d1",
            ruta_relativa="bin/Makefile",
            nombre="Makefile",
            tamano=10,
            mtime_ns=1,
        )
        assert fila.extension is None

    def test_id_estable_entre_separadores(self) -> None:
        """Mismo disco montado en Windows o Linux → mismo archivo_id."""
        a = construir_fila(
            disco_id="d1",
            ruta_relativa=r"datos\ventas.csv",
            nombre="ventas.csv",
            tamano=10,
            mtime_ns=1,
        )
        b = construir_fila(
            disco_id="d1",
            ruta_relativa="datos/ventas.csv",
            nombre="ventas.csv",
            tamano=10,
            mtime_ns=1,
        )
        assert a.archivo_id == b.archivo_id

    def test_discos_distintos_no_colisionan(self) -> None:
        """La misma ruta relativa en dos discos distintos son dos filas distintas."""
        a = construir_fila(
            disco_id="d1", ruta_relativa="x.csv", nombre="x.csv", tamano=10, mtime_ns=1
        )
        b = construir_fila(
            disco_id="d2", ruta_relativa="x.csv", nombre="x.csv", tamano=10, mtime_ns=1
        )
        assert a.archivo_id != b.archivo_id
