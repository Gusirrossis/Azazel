"""Fixtures de integración: esquema aplicado y base limpia por test."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def dsn() -> str:
    valor = os.environ.get("NORM_POSTGRES_DSN")
    if not valor:
        pytest.skip("requiere NORM_POSTGRES_DSN (docker compose --profile cola up)")
    return valor


@pytest.fixture(scope="session")
def esquema(dsn: str) -> None:
    """Aplica las migraciones una vez por sesión (idempotente si ya está en head)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(RAIZ / "alembic.ini"))
    cfg.set_main_option("script_location", str(RAIZ / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture()
def conexion(dsn: str, esquema: None) -> Iterator[Any]:
    """Conexión con la base LIMPIA (truncada) — cada test parte de cero."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute(
            # `usuarios`, `sesiones` y `extracciones` van aquí desde que existen: sin
            # truncarlas, un test que crea un admin deja la instalación "con usuarios"
            # para el siguiente, y una extracción cacheada hace que el de al lado
            # reuse un resultado en vez de producirlo. CASCADE por la FK de sesiones.
            "TRUNCATE archivos, discos, control, corridas, config_overrides,"
            " entidades, mapeos_aprobados, recetas, usuarios, sesiones, extracciones"
            " CASCADE"
        )
        conn.commit()
        yield conn
