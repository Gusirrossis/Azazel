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


class TestPrioridadPorExtension:
    """Orden de procesamiento por extensión (decisión 2026-06-11): .txt → .7z → .rar
    → .zip → resto. Valores >100 ganan a prioridad=puntaje y a contenedores (90)."""

    def test_orden_pedido(self) -> None:
        filtro = Config(_env_file=None).filtro
        p = filtro.prioridad_extensiones
        assert p[".txt"] > p[".7z"] > p[".rar"] > p[".zip"] > 100

    def test_extension_listada_gana_al_hint_de_contenedor(self) -> None:
        filtro = Config(_env_file=None).filtro
        assert filtro.prioridad_para_extension(".7z") == filtro.prioridad_extensiones[".7z"]
        assert filtro.prioridad_para_extension(".zip") == filtro.prioridad_extensiones[".zip"]

    def test_contenedor_no_listado_conserva_hint(self) -> None:
        filtro = Config(_env_file=None).filtro
        assert filtro.prioridad_para_extension(".gz") == filtro.prioridad_inicial_contenedores
        assert filtro.prioridad_para_extension(".iso") == filtro.prioridad_inicial_contenedores

    def test_resto_y_sin_extension_a_cero(self) -> None:
        filtro = Config(_env_file=None).filtro
        assert filtro.prioridad_para_extension(".csv") == 0
        assert filtro.prioridad_para_extension(None) == 0

    def test_mayusculas_normalizadas(self) -> None:
        filtro = Config(_env_file=None).filtro
        assert filtro.prioridad_para_extension(".TXT") == filtro.prioridad_extensiones[".txt"]

    def test_editable_por_entorno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "NORM_FILTRO__PRIORIDAD_EXTENSIONES", '{".pdf": 150, ".txt": 140}'
        )
        filtro = Config(_env_file=None).filtro
        assert filtro.prioridad_para_extension(".pdf") == 150
        # .zip ya no listada → cae al hint de contenedor (50)
        assert filtro.prioridad_para_extension(".zip") == filtro.prioridad_inicial_contenedores


class TestOverridePorEntorno:
    def test_perilla_anidada_por_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cambiar comportamiento = cambiar config, no código: NORM_FILTRO__UMBRAL_HOT."""
        monkeypatch.setenv("NORM_FILTRO__UMBRAL_HOT", "70")
        monkeypatch.setenv("NORM_POSTGRES_DSN", "postgresql://otro:otro@db:5/x")
        config = Config(_env_file=None)
        assert config.filtro.umbral_hot == 70
        assert config.postgres_dsn == "postgresql://otro:otro@db:5/x"
