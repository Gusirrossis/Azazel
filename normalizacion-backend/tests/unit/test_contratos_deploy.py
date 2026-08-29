"""Tests del contrato de despliegue: mapping de OpenSearch y política ISM válidos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

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


def _compose(nombre: str) -> dict[str, Any]:
    import yaml

    datos: dict[str, Any] = yaml.safe_load(
        (RAIZ / "deploy" / nombre).read_text(encoding="utf-8")
    )
    return datos


class TestComposeProduccion:
    """El compose `.dev` publica Postgres, OpenSearch (SIN seguridad), MinIO y
    Grafana a 0.0.0.0 con credenciales `norm/norm`. En una Mac eso es localhost;
    en un VPS con IP pública es el índice y la base enteros abiertos a internet.
    Estos tests impiden que ese archivo, o uno parecido, acabe en producción."""

    PUBLICOS_PERMITIDOS: ClassVar[set[str]] = {"caddy"}

    def test_solo_caddy_publica_puertos(self) -> None:
        servicios = _compose("docker-compose.prod.yml")["services"]
        publican = {n for n, s in servicios.items() if s.get("ports")}
        assert publican <= self.PUBLICOS_PERMITIDOS, (
            f"{publican - self.PUBLICOS_PERMITIDOS} exponen puertos al host;"
            " en producción la única superficie pública es Caddy"
        )

    def test_opensearch_conserva_su_seguridad(self) -> None:
        os_env = _compose("docker-compose.prod.yml")["services"]["opensearch"]["environment"]
        assert "DISABLE_SECURITY_PLUGIN" not in os_env
        assert "OPENSEARCH_INITIAL_ADMIN_PASSWORD" in os_env

    def test_sin_credenciales_por_defecto(self) -> None:
        """Todo secreto viene del entorno y el arranque FALLA si falta (`${VAR:?…}`).
        Un default como `norm/norm` se queda puesto para siempre."""
        crudo = (RAIZ / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
        for secreto in (
            "NORM_PG_PASSWORD",
            "NORM_OS_ADMIN_PASSWORD",
            "NORM_MINIO_ROOT_PASSWORD",
            "NORM_GRAFANA_PASSWORD",
        ):
            assert f"${{{secreto}:?" in crudo, f"{secreto} debe ser obligatorio, sin default"
        assert "norm-secreto" not in crudo
        assert "POSTGRES_PASSWORD: norm" not in crudo

    def test_la_api_exige_llaves_y_perfil(self) -> None:
        """Sin usuarios NI `api_keys` la API queda abierta a cualquiera; en el nodo
        público la llave estática es la red de seguridad que no depende de que
        alguien se acuerde de crear el primer usuario. Y sin perfil, un VPS
        arrancaría como `local`: archivo maestro y selector de destino, que son
        justo lo que no debe ser."""
        crudo = (RAIZ / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
        assert "${NORM_API_KEYS:?" in crudo
        assert "${NORM_DESPLIEGUE__PERFIL:?" in crudo
        assert "${NORM_DESPLIEGUE__NODO_ID:?" in crudo

    def test_el_compose_dev_sigue_siendo_de_dev(self) -> None:
        """Documenta la diferencia: si alguien 'arregla' el .dev quitándole puertos,
        este test recuerda que el de producción es otro archivo."""
        dev = _compose("docker-compose.dev.yml")["services"]
        assert dev["postgres"].get("ports"), "el .dev publica puertos a propósito (localhost)"
