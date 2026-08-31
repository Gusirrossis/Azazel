"""Fixtures de integración: esquema aplicado y base limpia por test.

⚠ ESTOS TESTS BORRAN DATOS. `conexion` hace `TRUNCATE` de la cola entera, y algunos
tests escriben en OpenSearch. Apuntarlos a producción por accidente —un
`NORM_POSTGRES_DSN` heredado del entorno, una terminal equivocada— destruye el
catálogo. Ya ocurrió una vez.

Por eso este módulo empieza con dos guardas que abortan la sesión ENTERA antes de
que corra un solo test. No son un aviso: cortan.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]

#: Una base de pruebas está vacía o casi. Producción tiene millones de filas. Este
#: número no distingue "test" de "producción" por el NOMBRE —que se puede repetir o
#: copiar— sino por lo que hay dentro, que es lo que de verdad importa proteger.
_TOPE_FILAS_SEGURO = 1_000

#: Alias de OpenSearch que los tests pueden tocar. El de producción es `archivos`, y
#: un `Config()` sin `indice_alias` explícito cae ahí por defecto.
#:
#: Hoy los tres tests que hablan con un OpenSearch real fijan su alias
#: (`archivos-api-test`, `archivos-test`) y `test_chaos` usa un cliente falso. Esta
#: guarda existe para el test que se escriba MAÑANA y se olvide de fijarlo: el coste
#: de olvidarlo es escribir documentos de prueba en el índice real.
_PREFIJOS_ALIAS_TEST = ("archivos-test", "archivos-api-test")


def _abortar(motivo: str) -> None:
    pytest.exit(
        f"\n\n{'=' * 78}\n"
        f"  TESTS DE INTEGRACIÓN ABORTADOS — parece PRODUCCIÓN\n"
        f"{'=' * 78}\n"
        f"  {motivo}\n\n"
        f"  Estos tests hacen TRUNCATE de la cola y escriben en el índice.\n"
        f"  Levanta una base de pruebas y apunta ahí:\n\n"
        f"    docker compose -f deploy/docker-compose.dev.yml --profile cola up -d\n"
        f"    NORM_POSTGRES_DSN=postgresql://norm:norm@localhost:5432/normalizacion \\\n"
        f"      pytest tests/integracion/\n"
        f"{'=' * 78}\n",
        returncode=2,
    )


@pytest.fixture(scope="session")
def dsn() -> str:
    valor = os.environ.get("NORM_POSTGRES_DSN")
    if not valor:
        pytest.skip("requiere NORM_POSTGRES_DSN (docker compose --profile cola up)")
    return valor


@pytest.fixture(scope="session", autouse=True)
def _guarda_no_es_produccion(dsn: str) -> None:
    """Aborta la sesión si la base de destino tiene datos reales.

    Se mira el CONTENIDO y no el nombre: una base llamada `normalizacion_test` que
    resultó ser una copia de producción es igual de destructiva, y el nombre de la
    de producción también es `normalizacion`.

    Una tabla `archivos` inexistente significa base recién creada: eso es seguro.
    """
    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            fila = conn.execute("SELECT count(*) FROM archivos").fetchone()
    except psycopg.errors.UndefinedTable:
        return  # base virgen: las migraciones aún no han corrido
    except Exception as exc:
        _abortar(f"No se pudo comprobar si es producción: {type(exc).__name__}: {exc}")
        return

    filas = int(fila[0]) if fila else 0
    if filas > _TOPE_FILAS_SEGURO:
        _abortar(
            f"`archivos` tiene {filas:,} filas (el tope seguro es {_TOPE_FILAS_SEGURO:,}).\n"
            f"  DSN: {_censurar(dsn)}"
        )


@pytest.fixture(scope="session", autouse=True)
def _guarda_indice_de_pruebas() -> None:
    """Aborta si el entorno apunta al alias de producción.

    `Config(_env_file=None, ...)` NO aísla de las variables `NORM_*`: pydantic-settings
    las lee igual. Así que un `NORM_INDICE_ALIAS` heredado —o simplemente el valor por
    defecto `archivos` en un test que se olvidó de fijarlo— manda los documentos de
    prueba al índice real.
    """
    alias = os.environ.get("NORM_INDICE_ALIAS")
    if alias and not alias.startswith(_PREFIJOS_ALIAS_TEST):
        _abortar(
            f"NORM_INDICE_ALIAS={alias!r} no es un alias de pruebas.\n"
            f"  Los permitidos empiezan por: {', '.join(_PREFIJOS_ALIAS_TEST)}"
        )


def _censurar(dsn: str) -> str:
    """El DSN lleva la contraseña. Se enseña el destino, nunca la credencial."""
    if "@" in dsn:
        esquema, _, resto = dsn.partition("://")
        return f"{esquema}://***@{resto.rpartition('@')[2]}"
    return dsn


@pytest.fixture(scope="session")
def esquema(dsn: str, _guarda_no_es_produccion: None) -> None:
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
