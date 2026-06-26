"""Tests del gobernador de recursos (⚙ K15): dimensionado y presión por memoria.

Se inyecta un `psutil` FALSO en sys.modules para simular distintos niveles de RAM
sin depender de la máquina que corre los tests (ni de tener psutil instalado)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from normalizacion.core import recursos
from normalizacion.core.config import Config


def _config(**recursos_kwargs: object) -> Config:
    cfg = Config(_env_file=None)
    for clave, valor in recursos_kwargs.items():
        setattr(cfg.recursos, clave, valor)
    return cfg


@pytest.fixture
def psutil_falso(monkeypatch: pytest.MonkeyPatch):
    """Devuelve un setter: `fijar(total_mb, disponible_mb)` controla la 'RAM'."""
    estado = {"total": 16384, "disponible": 16384}

    def vm() -> SimpleNamespace:
        usado = estado["total"] - estado["disponible"]
        pct = 100.0 * usado / estado["total"] if estado["total"] else 0.0
        return SimpleNamespace(
            total=estado["total"] * 1024 * 1024,
            available=estado["disponible"] * 1024 * 1024,
            percent=pct,
        )

    modulo = SimpleNamespace(virtual_memory=vm)
    monkeypatch.setitem(sys.modules, "psutil", modulo)

    def fijar(total_mb: int, disponible_mb: int) -> None:
        estado["total"] = total_mb
        estado["disponible"] = disponible_mb

    return fijar


class TestPolitica:
    def test_reserva_por_politica(self) -> None:
        assert _config(politica="conservador").recursos.fraccion_reserva() == 0.40
        assert _config(politica="balanceado").recursos.fraccion_reserva() == 0.30
        assert _config(politica="maximo").recursos.fraccion_reserva() == 0.20

    def test_override_explicito_manda_sobre_politica(self) -> None:
        cfg = _config(politica="maximo", reserva_ram_pct=0.5)
        assert cfg.recursos.fraccion_reserva() == 0.5


class TestPresupuestoWorkers:
    def test_modo_fijo_respeta_lo_solicitado(self, psutil_falso) -> None:
        psutil_falso(16384, 256)  # casi sin RAM: en fijo NO debe importar
        cfg = _config(modo="fijo")
        assert recursos.presupuesto_workers(cfg, solicitado=8) == 8

    def test_adaptativo_acota_por_memoria(self, psutil_falso) -> None:
        # 16 GB total, 8 GB libres, reserva 40% = 6.5 GB → utilizable ≈ 1.5 GB.
        # mem_por_worker 700 MB → ~2 workers, aunque se pidan 8.
        psutil_falso(16384, 8192)
        cfg = _config(modo="adaptativo", politica="conservador", mem_por_worker_mb=700)
        n = recursos.presupuesto_workers(cfg, solicitado=8)
        assert 1 <= n <= 3

    def test_adaptativo_mucha_ram_libre_sube_workers(self, psutil_falso) -> None:
        psutil_falso(65536, 60000)  # 64 GB, casi toda libre
        cfg = _config(modo="adaptativo", politica="maximo", mem_por_worker_mb=700)
        # No debe pasar del tope por núcleos (no podemos predecir os.cpu_count()).
        n = recursos.presupuesto_workers(cfg)
        assert n >= 1

    def test_adaptativo_solicitado_es_techo_no_orden(self, psutil_falso) -> None:
        psutil_falso(65536, 60000)
        cfg = _config(modo="adaptativo", mem_por_worker_mb=100)
        assert recursos.presupuesto_workers(cfg, solicitado=2) <= 2

    def test_workers_max_acota(self, psutil_falso) -> None:
        psutil_falso(65536, 60000)
        cfg = _config(modo="adaptativo", mem_por_worker_mb=100, workers_max=1)
        assert recursos.presupuesto_workers(cfg) == 1

    def test_sin_psutil_cae_a_nucleos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Sin psutil instalado, medir() devuelve None y adaptativo no revienta.
        monkeypatch.setitem(sys.modules, "psutil", None)
        cfg = _config(modo="adaptativo")
        assert recursos.presupuesto_workers(cfg) >= 1


class TestPresion:
    def test_bajo_presion_cuando_falta_ram(self, psutil_falso) -> None:
        psutil_falso(16384, 1024)  # 1 GB libre, reserva 40% → presión
        assert recursos.bajo_presion(_config(modo="adaptativo")) is True

    def test_sin_presion_con_ram_holgada(self, psutil_falso) -> None:
        psutil_falso(16384, 14000)
        assert recursos.bajo_presion(_config(modo="adaptativo")) is False

    def test_modo_fijo_nunca_reporta_presion(self, psutil_falso) -> None:
        psutil_falso(16384, 128)
        assert recursos.bajo_presion(_config(modo="fijo")) is False

    def test_esperar_sin_presion_no_bloquea(self, psutil_falso) -> None:
        psutil_falso(16384, 14000)
        assert recursos.esperar_si_presion(_config(modo="adaptativo")) == 0.0

    def test_esperar_respeta_tope_y_no_cuelga(self, psutil_falso) -> None:
        psutil_falso(16384, 128)  # presión permanente
        cfg = _config(modo="adaptativo", espera_max_presion_s=0.0)
        # espera_max=0 → devuelve de inmediato (jamás cuelga un lote por memoria).
        assert recursos.esperar_si_presion(cfg) == 0.0


class TestCabeTarea:
    def test_no_cabe_bajo_presion(self, psutil_falso) -> None:
        psutil_falso(16384, 1024)
        assert recursos.cabe_tarea(_config(modo="adaptativo")) is False

    def test_cabe_con_holgura(self, psutil_falso) -> None:
        psutil_falso(16384, 14000)
        assert recursos.cabe_tarea(_config(modo="adaptativo"), costo_mb=256) is True

    def test_modo_fijo_siempre_cabe(self, psutil_falso) -> None:
        psutil_falso(16384, 128)
        assert recursos.cabe_tarea(_config(modo="fijo")) is True
