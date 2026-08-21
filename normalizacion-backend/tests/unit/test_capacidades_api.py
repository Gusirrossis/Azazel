"""⚙K16 — contrato de capacidades en los bordes de la API.

La tabla perfil x ruta vive como DATO, no como un `if` repetido en cada test: si
mañana se añade un perfil, se añade una fila y el test dice qué falta cablear.

Estos tests construyen la app SIN tocar Postgres ni OpenSearch: sólo comprueban
qué se rechaza con 409 antes de llegar a la capa de datos.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from normalizacion.core.config import Config, PerillasDespliegue

# (ruta, método, capacidad requerida, cuerpo VÁLIDO)
#
# El cuerpo tiene que validar: FastAPI corre la validación del esquema ANTES del
# handler, así que con un body malformado saldría 422 y el corte por capacidad ni
# se ejecutaría — el test estaría comprobando otra cosa.
ACCIONES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("/entidades/backfill", "post", "entidades", {}),
    ("/entidades/enviar", "post", "entidades", {}),
    (
        "/entidades/proyectar",
        "post",
        "entidades",
        {"tipo": "persona", "asignacion": {"curp": "curp"}, "filas": []},
    ),
    ("/pipeline/ejecutar", "post", "ingesta", {"ruta": "/tmp/x"}),
)

# perfil → capacidades esperadas
CAPACIDADES: dict[str, dict[str, bool]] = {
    "local": {"ingesta": True, "entidades": True},
    "hibrido-ingesta": {"ingesta": True, "entidades": False},
    "hibrido-servicio": {"ingesta": True, "entidades": True},
}


@pytest.fixture(autouse=True)
def _sin_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """La autorización consulta Postgres (claves dinámicas con nombre del panel).

    Estos tests comprueban SÓLO el corte por capacidad, que ocurre antes de tocar
    datos, así que la auth se cortocircuita en vez de levantar una base."""
    monkeypatch.setattr("normalizacion.api.claves_busqueda.autorizada", lambda cfg, k: True)


def _app(perfil: str, nodo_id: str) -> Any:
    from normalizacion.api.main import crear_app

    config = Config(
        _env_file=None,
        despliegue=PerillasDespliegue(perfil=perfil, nodo_id=nodo_id),  # type: ignore[arg-type]
    )
    return crear_app(config)


@pytest.fixture(params=sorted(CAPACIDADES))
def perfil(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class TestCapacidadesPorPerfil:
    def test_la_topologia_se_expone(self, perfil: str) -> None:
        """El front necesita saber qué OCULTAR: una sección vacía se lee como
        'no hay datos', y eso sería mentira si la capacidad vive en otro nodo."""
        cliente = TestClient(_app(perfil, "n1"))
        cuerpo = cliente.get("/sistema/topologia").json()
        assert cuerpo["perfil"] == perfil
        for capacidad, esperado in CAPACIDADES[perfil].items():
            assert cuerpo["capacidades"][capacidad] is esperado

    @pytest.mark.parametrize(("ruta", "metodo", "capacidad", "cuerpo"), ACCIONES)
    def test_accion_sin_capacidad_responde_409(
        self, perfil: str, ruta: str, metodo: str, capacidad: str, cuerpo: dict[str, Any]
    ) -> None:
        """Nunca un resultado inventado: si este nodo no resuelve entidades, la
        respuesta correcta es 409, no 'cero entidades'."""
        if CAPACIDADES[perfil][capacidad]:
            pytest.skip(f"{perfil} sí tiene {capacidad}")
        cliente = TestClient(_app(perfil, "n1"))
        respuesta = getattr(cliente, metodo)(ruta, json=cuerpo)
        assert respuesta.status_code == 409, respuesta.text
        assert "K16" in respuesta.json()["detail"]


class TestDemonioDeEnvio:
    """P5: `iniciar_bucle` arrancaba en TODA instancia de la API. Con dos nodos eso
    son dos procesos empujando al AEB con `modo_merge: reemplazar` (last-write-wins),
    cada uno con su versión PARCIAL de la misma persona, pisándose en bucle."""

    @pytest.mark.parametrize("perfil", sorted(CAPACIDADES))
    def test_solo_arranca_donde_hay_capacidad(
        self, perfil: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        arrancados: list[str] = []
        monkeypatch.setattr(
            "normalizacion.entidades.envio.iniciar_bucle",
            lambda cfg: arrancados.append(cfg.despliegue.perfil),
        )
        _app(perfil, "n1")
        assert bool(arrancados) is CAPACIDADES[perfil]["entidades"]


class TestDestinoEligible:
    """P2: el nodo que replica blobs hacia el maestro necesita un almacén ÚNICO.
    Con el selector de destino, cada corrida puede dejar el almacén en una carpeta
    distinta (`config_con_destino` conmuta a backend `local`) y no hay qué replicar."""

    def test_el_vps_no_ofrece_selector_de_destino(self) -> None:
        cliente = TestClient(_app("hibrido-servicio", "vps-01"))
        caps = cliente.get("/sistema/topologia").json()["capacidades"]
        assert caps["destino_eligible"] is False
        assert caps["archivo_maestro"] is False

    def test_el_maestro_si_lo_ofrece(self) -> None:
        for perfil in ("local", "hibrido-ingesta"):
            cliente = TestClient(_app(perfil, "mac-01"))
            caps = cliente.get("/sistema/topologia").json()["capacidades"]
            assert caps["destino_eligible"] is True, perfil
            assert caps["archivo_maestro"] is True, perfil
