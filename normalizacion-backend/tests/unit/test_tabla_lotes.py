"""Trocear una base en lotes: el mecanismo que lleva del 0,06 % al 100 %.

El test que sostiene todo el módulo es `test_los_lotes_cubren_la_tabla_entera`. Si el
troceo pierde filas, el fallo no se ve: la base se indexa, los documentos aparecen, las
búsquedas responden — y una persona que SÍ está en el corpus no sale nunca. Un agujero
silencioso es peor que un error, porque nadie lo va a ir a buscar.

Por eso se comprueba la propiedad completa (unión de lotes == filas de la tabla) y no
una muestra: con rangos de `rowid` y huecos por filas borradas, los casos que fallan son
justo los bordes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from normalizacion.core.config import PerillasFiltro
from normalizacion.ingesta.precalificacion import tabla_lotes


def _base(ruta: Path, filas: int, *, huecos: bool = False, sin_rowid: bool = False) -> Path:
    con = sqlite3.connect(ruta)
    if sin_rowid:
        con.execute("CREATE TABLE t (k TEXT PRIMARY KEY, curp TEXT) WITHOUT ROWID")
        for i in range(filas):
            con.execute("INSERT INTO t VALUES (?,?)", (f"k{i:06d}", f"CURP{i:06d}"))
    else:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, curp TEXT)")
        for i in range(filas):
            con.execute("INSERT INTO t VALUES (?,?)", (i + 1, f"CURP{i:06d}"))
        if huecos:
            # Filas borradas: el rowid queda con agujeros y los lotes salen desiguales.
            con.execute("DELETE FROM t WHERE id % 3 = 0")
    con.commit()
    con.close()
    return ruta


def _todas_las_curps(ruta: Path) -> set[str]:
    con = sqlite3.connect(ruta)
    try:
        return {c for (c,) in con.execute("SELECT curp FROM t")}
    finally:
        con.close()


def _curps_de_los_lotes(ruta: Path, lotes: list[tabla_lotes.Lote]) -> list[str]:
    vistas: list[str] = []
    for lote in lotes:
        flujo = tabla_lotes.servir_lote(
            ruta, lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=1 << 24
        )
        with flujo:
            for linea in flujo.read().decode().splitlines():
                if linea.strip():
                    vistas.append(json.loads(linea)["curp"])
    return vistas


class TestCobertura:
    def test_los_lotes_cubren_la_tabla_entera(self, tmp_path: Path) -> None:
        """LA propiedad: ninguna fila se queda fuera y ninguna se cuenta dos veces."""
        ruta = _base(tmp_path / "a.db", 1750)
        lotes = tabla_lotes.planificar(ruta, filas_por_lote=100)
        vistas = _curps_de_los_lotes(ruta, lotes)
        assert len(vistas) == len(set(vistas)), "ningún lote puede repetir filas"
        assert set(vistas) == _todas_las_curps(ruta)
        assert len(vistas) == 1750

    def test_tambien_con_huecos_en_el_rowid(self, tmp_path: Path) -> None:
        """El caso real: filas borradas dejan agujeros y los lotes salen desiguales.
        Cubrir el RANGO sigue cubriendo todas las filas."""
        ruta = _base(tmp_path / "b.db", 1000, huecos=True)
        lotes = tabla_lotes.planificar(ruta, filas_por_lote=50)
        vistas = _curps_de_los_lotes(ruta, lotes)
        assert set(vistas) == _todas_las_curps(ruta)

    def test_tabla_sin_rowid_usa_offset_y_no_pierde_nada(self, tmp_path: Path) -> None:
        """WITHOUT ROWID no admite el troceo por rango: cae a OFFSET, que es más lento
        pero tiene que ser igual de completo."""
        ruta = _base(tmp_path / "c.db", 450, sin_rowid=True)
        lotes = tabla_lotes.planificar(ruta, filas_por_lote=100)
        assert all(lt.modo == "offset" for lt in lotes)
        assert set(_curps_de_los_lotes(ruta, lotes)) == _todas_las_curps(ruta)

    def test_una_tabla_pequena_da_un_solo_lote(self, tmp_path: Path) -> None:
        ruta = _base(tmp_path / "d.db", 10)
        assert len(tabla_lotes.planificar(ruta, filas_por_lote=500)) == 1


class TestFormato:
    def test_el_lote_sale_como_ndjson(self, tmp_path: Path) -> None:
        """NDJSON a propósito: lo recoge el plugin tabular que ya existe, con su perfil
        de calidad y su orden por identidad. Ni extractor nuevo ni camino paralelo."""
        ruta = _base(tmp_path / "e.db", 5)
        lote = tabla_lotes.planificar(ruta, filas_por_lote=500)[0]
        with tabla_lotes.servir_lote(
            ruta, lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=1 << 20
        ) as flujo:
            lineas = flujo.read().decode().strip().splitlines()
        assert len(lineas) == 5
        assert json.loads(lineas[0]).keys() == {"id", "curp"}

    def test_la_ruta_interna_es_estable(self, tmp_path: Path) -> None:
        """El `archivo_id` se deriva de la ruta: si el nombre del lote cambiara entre
        corridas, la misma base se duplicaría entera en la cola y en el índice."""
        ruta = _base(tmp_path / "f.db", 900)
        una = [lt.ruta_interna for lt in tabla_lotes.planificar(ruta, filas_por_lote=100)]
        otra = [lt.ruta_interna for lt in tabla_lotes.planificar(ruta, filas_por_lote=100)]
        assert una == otra
        assert all("/" in r for r in una)


class TestGuards:
    def test_respeta_el_tope_de_lotes_por_base(self, tmp_path: Path, monkeypatch) -> None:
        """Sin tope, una base de 50 M de filas genera 100.000 entradas ella sola y el
        resto del corpus no avanza."""
        monkeypatch.setattr(tabla_lotes, "MAX_LOTES_POR_BASE", 5)
        ruta = _base(tmp_path / "g.db", 1000)
        assert len(tabla_lotes.planificar(ruta, filas_por_lote=10)) <= 5

    def test_explorar_marca_corrupto_sin_lanzar(self, tmp_path: Path) -> None:
        malo = tmp_path / "roto.db"
        malo.write_bytes(b"SQLite format 3\x00" + b"basura" * 200)
        entradas, motivo = tabla_lotes.explorar(PerillasFiltro(), malo, 0)
        assert entradas == []
        assert motivo == "contenedor_corrupto"

    def test_no_modifica_la_base(self, tmp_path: Path) -> None:
        """Son datos de otro sistema: `mode=ro` + `immutable=1`."""
        ruta = _base(tmp_path / "h.db", 300)
        antes = (ruta.stat().st_mtime_ns, ruta.stat().st_size)
        lotes = tabla_lotes.planificar(ruta, filas_por_lote=50)
        _curps_de_los_lotes(ruta, lotes)
        assert (ruta.stat().st_mtime_ns, ruta.stat().st_size) == antes
        assert not (tmp_path / "h.db-wal").exists()
