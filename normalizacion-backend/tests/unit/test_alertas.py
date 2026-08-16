"""Las alertas y dashboards de deploy/ son contratos: deben ser válidos y coherentes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[2]

def _metricas_publicadas() -> set[str]:
    """Las que el Exportador publica DE VERDAD, leídas de su registry.

    Antes era una lista a mano con la nota "si cambian, cambia aquí Y en deploy/".
    Una lista así se desincroniza sola: al añadir `norm_replica_lag_segundos` el
    test falló por la copia, no por un problema real. Derivarla del código elimina
    la doble contabilidad y el test sigue haciendo su trabajo — impedir alertas y
    paneles sobre métricas fantasma, que jamás disparan (operación a ciegas)."""
    from normalizacion.core.observabilidad.metricas import Exportador

    return {
        m.name for m in Exportador().registry.collect() if m.name.startswith("norm_")
    }


METRICAS_PUBLICADAS = _metricas_publicadas()


def _metricas_norm(texto: str) -> set[str]:
    return set(re.findall(r"\bnorm_[a-z_]+", texto))


class TestAlertas:
    def test_reglas_son_yaml_valido_con_runbooks(self) -> None:
        reglas = yaml.safe_load((RAIZ / "deploy" / "prometheus-alertas.yml").read_text("utf-8"))
        alertas = reglas["groups"][0]["rules"]
        assert len(alertas) >= 4
        for alerta in alertas:
            assert alerta["alert"], "alerta sin nombre"
            assert "runbook" in alerta["annotations"], f"{alerta['alert']} sin runbook"

    def test_cada_runbook_referenciado_existe(self) -> None:
        reglas = (RAIZ / "deploy" / "prometheus-alertas.yml").read_text("utf-8")
        runbooks = (RAIZ / "deploy" / "RUNBOOKS.md").read_text("utf-8").lower().replace(" ", "")
        for ancla in re.findall(r"RUNBOOKS\.md#([a-z]+)", reglas):
            assert f"##{ancla}" in runbooks, f"runbook #{ancla} no existe"

    def test_alertas_usan_metricas_que_existen(self) -> None:
        """Una alerta sobre una métrica fantasma jamás dispara (operación a ciegas)."""
        reglas = (RAIZ / "deploy" / "prometheus-alertas.yml").read_text("utf-8")
        assert _metricas_norm(reglas) <= METRICAS_PUBLICADAS


class TestDashboard:
    def test_dashboard_es_json_valido(self) -> None:
        dash = json.loads(
            (RAIZ / "deploy" / "grafana" / "dashboards" / "normalizacion.json").read_text("utf-8")
        )
        assert dash["title"]
        assert len(dash["panels"]) >= 6

    def test_paneles_usan_metricas_que_existen(self) -> None:
        crudo = (RAIZ / "deploy" / "grafana" / "dashboards" / "normalizacion.json").read_text(
            "utf-8"
        )
        assert _metricas_norm(crudo) <= METRICAS_PUBLICADAS
