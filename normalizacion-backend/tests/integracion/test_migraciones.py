"""Integración: las migraciones suben y bajan contra un Postgres real.

Requiere NORM_POSTGRES_DSN apuntando a un Postgres vivo (perfil `cola` del compose).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integracion,
    pytest.mark.skipif(
        not os.environ.get("NORM_POSTGRES_DSN"),
        reason="requiere NORM_POSTGRES_DSN (docker compose --profile cola up)",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]


def _config_alembic() -> object:
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "alembic"))
    return cfg


def test_upgrade_y_downgrade_completos() -> None:
    """DoD Fase 0: `alembic upgrade head` crea el esquema y `downgrade base` lo revierte."""
    import psycopg
    from alembic import command

    cfg = _config_alembic()
    dsn = os.environ["NORM_POSTGRES_DSN"]

    command.upgrade(cfg, "head")  # type: ignore[arg-type]
    with psycopg.connect(dsn) as conn:
        tablas = {
            fila[0]
            for fila in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    assert {"archivos", "discos"} <= tablas

    # La máquina de estados vive también en la BD (CHECK constraint)
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE archivos, discos")
        conn.commit()
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO discos (disco_id) VALUES ('d1')")
        conn.execute(
            "INSERT INTO archivos (archivo_id, disco_id, ruta, nombre, tamano, mtime, estado)"
            " VALUES ('x', 'd1', '/x', 'x', 0, now(), 'ESTADO_INVENTADO')"
        )

    command.downgrade(cfg, "base")  # type: ignore[arg-type]
    with psycopg.connect(dsn) as conn:
        tablas = {
            fila[0]
            for fila in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    assert "archivos" not in tablas

    # Restaurar el esquema: otros tests (y el entorno dev) lo necesitan vivo
    command.upgrade(cfg, "head")  # type: ignore[arg-type]
