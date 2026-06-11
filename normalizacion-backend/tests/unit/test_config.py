"""Tests de la configuración: defaults sanos y override por entorno (perillas ⚙)."""

from __future__ import annotations

import pytest

from normalizacion.core.config import Config


class TestDefaults:
    def test_carga_sin_entorno(self) -> None:
        config = Config(_env_file=None)
        assert config.filtro.umbral_hot == 65
        assert config.filtro.umbral_cold == 35
        assert config.filtro.t3_profundidad_max == 10
        # Decisión del usuario (2026-06-10): los 7z reales (~15 GB → 200 GB+) se
        # exploran COMPLETOS — los guards K4 son de seguridad, no de capacidad
        assert config.filtro.t3_descomprimido_max_bytes >= 500 * 1024**3
        assert config.filtro.t3_entradas_max >= 100_000
        assert config.filtro.t3_ratio_compresion_max >= 300
        assert config.worker.lote_claim == 500
        assert config.indexador.reintentos_max == 3

    def test_umbral_cold_menor_que_hot(self) -> None:
        config = Config(_env_file=None)
        assert config.filtro.umbral_cold < config.filtro.umbral_hot

    def test_bytes_t1_suficientes_para_libmagic(self) -> None:
        """python-magic recomienda >= 2048 bytes; el modelo lo exige."""
        config = Config(_env_file=None)
        assert config.filtro.bytes_t1 >= 2048


class TestOverridePorEntorno:
    def test_perilla_anidada_por_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cambiar comportamiento = cambiar config, no código: NORM_FILTRO__UMBRAL_HOT."""
        monkeypatch.setenv("NORM_FILTRO__UMBRAL_HOT", "70")
        monkeypatch.setenv("NORM_POSTGRES_DSN", "postgresql://otro:otro@db:5/x")
        config = Config(_env_file=None)
        assert config.filtro.umbral_hot == 70
        assert config.postgres_dsn == "postgresql://otro:otro@db:5/x"
