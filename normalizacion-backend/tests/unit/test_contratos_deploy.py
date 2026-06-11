"""Tests del contrato de despliegue: mapping de OpenSearch y política ISM válidos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[2]


def _cargar(relativo: str) -> dict[str, Any]:
    datos: dict[str, Any] = json.loads((RAIZ / relativo).read_text(encoding="utf-8"))
    return datos


class TestMappingArchivos:
    def test_es_json_valido_con_template(self) -> None:
        mapping = _cargar("deploy/mappings/archivos.json")
        assert mapping["index_patterns"] == ["archivos-*"]
        assert "mappings" in mapping["template"]

    def test_nombre_usa_wildcard_no_ngram(self) -> None:
        """Decisión de arquitectura: wildcard (~4 TB) y NO ngram (~15-25 TB)."""
        props = _cargar("deploy/mappings/archivos.json")["template"]["mappings"]["properties"]
        assert props["nombre"]["type"] == "wildcard"
        assert props["ruta_original"]["type"] == "wildcard"

    def test_campos_clave_del_contrato(self) -> None:
        props = _cargar("deploy/mappings/archivos.json")["template"]["mappings"]["properties"]
        for campo in ("archivo_id", "disco_id", "tipo_real", "puntaje", "hash_contenido"):
            assert campo in props, f"falta {campo} en el mapping"
        assert props["puntaje"]["type"] == "short"

    def test_dynamic_templates_para_campos_extraidos(self) -> None:
        mappings = _cargar("deploy/mappings/archivos.json")["template"]["mappings"]
        assert any("campos_extraidos" in str(t) for t in mappings["dynamic_templates"])


class TestPoliticaIsm:
    def test_estados_hot_warm_cold(self) -> None:
        politica = _cargar("deploy/ism/politica_archivos.json")["policy"]
        nombres = [estado["name"] for estado in politica["states"]]
        assert nombres == ["hot", "warm", "cold"]
        assert politica["default_state"] == "hot"

    def test_hot_tiene_rollover(self) -> None:
        politica = _cargar("deploy/ism/politica_archivos.json")["policy"]
        hot = politica["states"][0]
        assert any("rollover" in accion for accion in hot["actions"])
