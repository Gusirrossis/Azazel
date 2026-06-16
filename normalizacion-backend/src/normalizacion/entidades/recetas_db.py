"""Almacén de recetas en Postgres (CRUD + semilla). Las recetas son DATOS
editables: el sistema arranca con fz1 + un ejemplo, y el usuario crea/edita más
desde la UI sin tocar código."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.config import Config

from .proyeccion import SEMILLAS, validar_definicion

_COLS = ("clave, clase, tipo, nombre, descripcion, definicion, version, activa,"
         " editable, creado_en, actualizado_en")


def _a_dict(f: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "clave": f[0], "clase": f[1], "tipo": f[2], "nombre": f[3], "descripcion": f[4],
        "definicion": f[5], "version": f[6], "activa": f[7], "editable": f[8],
        "creado_en": f[9], "actualizado_en": f[10],
    }


def seed_recetas(conn: psycopg.Connection[Any]) -> None:
    """Inserta las recetas semilla si aún no existen (idempotente)."""
    for r in SEMILLAS:
        conn.execute(
            "INSERT INTO recetas (clave, clase, tipo, nombre, descripcion, definicion,"
            " version, editable) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (clave) DO NOTHING",
            (r["clave"], r["clase"], r["tipo"], r["nombre"], r["descripcion"],
             Jsonb(r["definicion"]), r["version"], r.get("editable", True)),
        )
    conn.commit()


def listar_recetas(config: Config, clase: str | None = None) -> list[dict[str, Any]]:
    cond = " WHERE clase = %s" if clase else ""
    with psycopg.connect(config.postgres_dsn) as conn:
        seed_recetas(conn)
        filas = conn.execute(
            f"SELECT {_COLS} FROM recetas{cond} ORDER BY clase, clave",
            (clase,) if clase else (),
        ).fetchall()
    return [_a_dict(f) for f in filas]


def leer_receta(config: Config, clave: str) -> dict[str, Any] | None:
    with psycopg.connect(config.postgres_dsn) as conn:
        f = conn.execute(f"SELECT {_COLS} FROM recetas WHERE clave = %s", (clave,)).fetchone()
    return _a_dict(f) if f else None


def guardar_receta(config: Config, r: dict[str, Any]) -> dict[str, Any]:
    """Crea o edita una receta de proyección. Valida la definición antes de persistir.
    Las recetas no editables (la base fz1) no se pueden sobrescribir."""
    validar_definicion(r["definicion"])
    with psycopg.connect(config.postgres_dsn) as conn:
        existente = conn.execute(
            "SELECT editable FROM recetas WHERE clave = %s", (r["clave"],)
        ).fetchone()
        if existente is not None and existente[0] is False:
            raise ValueError(f"la receta '{r['clave']}' es base y no es editable (clónala)")
        conn.execute(
            "INSERT INTO recetas (clave, clase, tipo, nombre, descripcion, definicion,"
            " version, activa, editable) VALUES (%s,%s,%s,%s,%s,%s,%s,true,%s)"
            " ON CONFLICT (clave) DO UPDATE SET nombre = EXCLUDED.nombre,"
            " descripcion = EXCLUDED.descripcion, definicion = EXCLUDED.definicion,"
            " version = EXCLUDED.version, actualizado_en = now()",
            (r["clave"], r.get("clase", "proyeccion"), r.get("tipo", "persona"),
             r["nombre"], r.get("descripcion", ""), Jsonb(r["definicion"]),
             r.get("version", "v1"), r.get("editable", True)),
        )
        conn.commit()
    out = leer_receta(config, r["clave"])
    assert out is not None
    return out


def borrar_receta(config: Config, clave: str) -> bool:
    with psycopg.connect(config.postgres_dsn) as conn:
        ed = conn.execute("SELECT editable FROM recetas WHERE clave = %s", (clave,)).fetchone()
        if ed is None:
            return False
        if ed[0] is False:
            raise ValueError("la receta base no se puede borrar")
        conn.execute("DELETE FROM recetas WHERE clave = %s", (clave,))
        conn.commit()
    return True
