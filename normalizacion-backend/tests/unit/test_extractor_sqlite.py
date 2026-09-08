"""Extractor de SQLite: el formato en el que está casi todo el corpus.

Los dos tests que sostienen este archivo:

  · `TestNoEscribe` — las bases son de OTRO sistema y están vivas. Si el extractor
    las toca, el fallo no es un dato mal indexado: es corromper la base de alguien.
  · `TestIdentidad` — el objetivo real no es "leer la base", es que la CURP que hay
    dentro acabe en el texto. De ahí salen las anclas, y de las anclas las personas.
    Un extractor que lee perfectamente y deja las CURP fuera del presupuesto no sirve.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from normalizacion.core.config import PerillasWorker
from normalizacion.ingesta.workers.extractores import (
    ContextoExtraccion,
    extractor_para,
    mimes_registrados,
)
from normalizacion.ingesta.workers.extractores.sqlite import extraer_sqlite


def _base(ruta: Path, filas: int = 5, *, con_curp: bool = True) -> Path:
    con = sqlite3.connect(ruta)
    con.execute(
        "CREATE TABLE padron (id INTEGER, curp TEXT, nombre TEXT, notas TEXT)"
        if con_curp
        else "CREATE TABLE padron (id INTEGER, notas TEXT)"
    )
    con.execute("CREATE TABLE config (clave TEXT, valor TEXT)")
    con.execute("INSERT INTO config VALUES ('version','1')")
    for i in range(filas):
        if con_curp:
            con.execute(
                "INSERT INTO padron VALUES (?,?,?,?)",
                (i, f"GOMC{800101 + i}HDFXXX{i:02d}", f"Persona {i}", "x" * 40),
            )
        else:
            con.execute("INSERT INTO padron VALUES (?,?)", (i, "x" * 40))
    con.commit()
    con.close()
    return ruta


def _ctx(ruta: Path, *, max_chars: int = 100_000) -> ContextoExtraccion:
    perillas = PerillasWorker(extractor_max_chars=max_chars)
    return ContextoExtraccion(
        fuente=open(ruta, "rb"),  # noqa: SIM115 — el plugin lo cierra o usa su .name
        nombre=ruta.name,
        tipo_real="application/vnd.sqlite3",
        tamano=ruta.stat().st_size,
        perillas=perillas,
    )


class TestRegistro:
    def test_el_mime_de_sqlite_tiene_extractor(self) -> None:
        """La precalificación ya detecta `SQLite format 3` por magic bytes y lo manda
        a HOT. Sin plugin registrado, esas bases se indexaban mudas."""
        assert "application/vnd.sqlite3" in mimes_registrados()
        assert extractor_para("application/vnd.sqlite3") is extraer_sqlite


class TestEsquema:
    def test_saca_tablas_y_columnas(self, tmp_path: Path) -> None:
        r = extraer_sqlite(_ctx(_base(tmp_path / "a.db")))
        assert r.campos["sqlite_tablas"] == 2
        assert set(r.campos["sqlite_tablas_nombres"]) == {"padron", "config"}
        assert "padron" in r.texto
        assert "curp" in r.texto

    def test_cuenta_las_filas_reales(self, tmp_path: Path) -> None:
        """El recuento es del total, no de lo que cupo en el texto: es lo que permite
        saber que una base de 3 M de filas entró como muestra."""
        r = extraer_sqlite(_ctx(_base(tmp_path / "b.db", filas=40)))
        assert r.campos["sqlite_filas_total"] == 41  # 40 de padron + 1 de config

    def test_ignora_las_tablas_internas_de_sqlite(self, tmp_path: Path) -> None:
        """`sqlite_stat1` no se puede crear a mano —el nombre está reservado—, así que
        se genera como en la vida real: con ANALYZE sobre una tabla indexada."""
        ruta = _base(tmp_path / "c.db")
        con = sqlite3.connect(ruta)
        con.execute("CREATE INDEX idx_curp ON padron(curp)")
        con.execute("ANALYZE")
        con.commit()
        con.close()
        interna = sqlite3.connect(ruta).execute(
            "SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0]
        assert interna == 1, "el escenario exige que la tabla interna exista"

        r = extraer_sqlite(_ctx(ruta))
        assert not any(t.startswith("sqlite_") for t in r.campos["sqlite_tablas_nombres"])


class TestIdentidad:
    def test_la_curp_del_contenido_llega_al_texto(self, tmp_path: Path) -> None:
        """El objetivo entero: sin esto no hay anclas, y sin anclas no hay personas."""
        r = extraer_sqlite(_ctx(_base(tmp_path / "d.db", filas=3)))
        assert "GOMC800101HDFXXX00" in r.texto
        assert "Persona 1" in r.texto

    def test_con_presupuesto_corto_gana_la_identidad(self, tmp_path: Path) -> None:
        """La prueba de que priorizar sirve: con el texto muy topado, lo que sobrevive
        son las CURP y no el relleno de la columna `notas`."""
        ruta = _base(tmp_path / "e.db", filas=30)
        r = extraer_sqlite(_ctx(ruta, max_chars=1200))
        assert len(r.texto) <= 1200
        assert "GOMC" in r.texto, "las CURP deben entrar antes que el relleno"

    def test_marca_que_es_una_muestra(self, tmp_path: Path) -> None:
        """Una base grande entra parcial. Decirlo importa: la ausencia de un nombre en
        el índice no puede leerse como que la persona no está en la base."""
        r = extraer_sqlite(_ctx(_base(tmp_path / "f.db", filas=900), max_chars=2000))
        assert "sqlite_muestra" in r.flags or "sqlite_texto_truncado" in r.flags


class TestNoEscribe:
    def test_no_modifica_la_base(self, tmp_path: Path) -> None:
        """`mode=ro` + `immutable=1`. Sin `immutable`, SQLite intenta recuperar el WAL
        —es decir, ESCRIBIR— sobre una base viva de otro sistema."""
        ruta = _base(tmp_path / "g.db")
        antes = (ruta.stat().st_mtime_ns, ruta.stat().st_size)
        extraer_sqlite(_ctx(ruta))
        assert (ruta.stat().st_mtime_ns, ruta.stat().st_size) == antes

    def test_no_deja_wal_ni_shm(self, tmp_path: Path) -> None:
        """Los rastros de que alguien abrió la base para escribir."""
        ruta = _base(tmp_path / "h.db")
        extraer_sqlite(_ctx(ruta))
        assert not (tmp_path / "h.db-wal").exists()
        assert not (tmp_path / "h.db-shm").exists()

    def test_funciona_sobre_un_archivo_sin_permiso_de_escritura(self, tmp_path: Path) -> None:
        """Como estará montado de verdad: `:ro`. Si el plugin necesitara escribir,
        aquí es donde se vería."""
        ruta = _base(tmp_path / "i.db")
        os.chmod(ruta, 0o444)
        try:
            r = extraer_sqlite(_ctx(ruta))
            assert r.campos["sqlite_tablas"] == 2
        finally:
            os.chmod(ruta, 0o644)


class TestNoRevienta:
    def test_un_archivo_corrupto_da_flag_y_no_excepcion(self, tmp_path: Path) -> None:
        """Regla del registro de extractores: `extraer` JAMÁS lanza."""
        malo = tmp_path / "roto.db"
        malo.write_bytes(b"SQLite format 3\x00" + b"basura" * 100)
        r = extraer_sqlite(_ctx(malo))
        assert any(f.startswith("sqlite_ilegible") for f in r.flags)

    def test_una_base_vacia_no_rompe(self, tmp_path: Path) -> None:
        vacia = tmp_path / "vacia.db"
        sqlite3.connect(vacia).close()
        r = extraer_sqlite(_ctx(vacia))
        assert r.campos["sqlite_tablas"] == 0
