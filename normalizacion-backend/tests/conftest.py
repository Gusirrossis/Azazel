"""Fixtures compartidas."""

from __future__ import annotations

from pathlib import Path

import pytest

from normalizacion.herramientas.generador_disco import generar_disco


@pytest.fixture(scope="session")
def disco_sintetico(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Un disco sintético determinista (semilla 42), compartido por la sesión de tests."""
    destino = tmp_path_factory.mktemp("disco")
    generar_disco(destino, semilla=42)
    return destino
