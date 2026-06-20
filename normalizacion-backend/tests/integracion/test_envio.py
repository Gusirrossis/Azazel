"""Worker de envío al AEB contra Postgres real, con el POST mockeado."""

from __future__ import annotations

from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.entidades import normalizadores as N
from normalizacion.entidades.destino import guardar_destino
from normalizacion.entidades.envio import enviar_a_destino, estado_envio
from normalizacion.entidades.pipeline import proyectar
from normalizacion.entidades.receta import PERSONA_FZ1

pytestmark = pytest.mark.integracion

ASIGN = {"curp": "curp", "primer_nombre": "nombre1", "apellido_paterno": "apellido1"}


def _curp(prefijo17: str) -> str:
    return prefijo17 + N.digito_verificador_curp(prefijo17)


@pytest.fixture()
def config(dsn: str, conexion: Any) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


def _habilitar(config: Config, lote: int = 2, intervalo_seg: int = 0) -> None:
    guardar_destino(config, {
        "habilitado": True, "url": "http://aeb.local", "auth_token": "secreto",
        "lote": lote, "intervalo_seg": intervalo_seg,
    })


class _FakeAEB:
    """Captura los lotes POSTeados y simula la respuesta del AEB (todo creado)."""

    def __init__(self) -> None:
        self.lotes: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], cuerpo: dict[str, Any]) -> Any:
        self.lotes.append(cuerpo)
        self.headers.append(headers)
        n = len(cuerpo["entidades"])
        return 200, {"recibidas": n, "creadas": n, "actualizadas": 0,
                     "sin_cambio": 0, "fallidas": 0}


def _sembrar(config: Config, cuantas: int) -> None:
    for i in range(cuantas):
        curp = _curp(f"MERV96031{i}MDFNSL0")
        proyectar(config, PERSONA_FZ1, ASIGN, [{"curp": curp, "primer_nombre": f"P{i}"}])


def test_deshabilitado_no_envia(config: Config) -> None:
    r = enviar_a_destino(config)
    assert r.detuvo_en == "destino deshabilitado" and r.entidades == 0


def test_envia_en_lotes_con_cable_correcto(config: Config, monkeypatch: Any) -> None:
    _habilitar(config, lote=2)
    _sembrar(config, 3)
    fake = _FakeAEB()
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", fake)

    r = enviar_a_destino(config)
    assert r.entidades == 3 and r.creadas == 3 and r.lotes == 2  # 2 + 1
    # Sobre canónico (no fz1_bundle) con la fuente y el header de auth correctos.
    assert fake.lotes[0]["fuente"] == "azazel_resolucion"
    assert fake.lotes[0]["productor"] == "azazel"
    assert fake.lotes[0]["modo_merge"] == "reemplazar"  # Azazel propaga cambios (last-write-wins)
    assert fake.headers[0]["X-API-Key"] == "secreto"
    item = fake.lotes[0]["entidades"][0]
    assert item["external_id"] and item["kind"] == "person"
    assert "personas" not in fake.lotes[0] and "_metadata" not in fake.lotes[0]


def test_reanudable_e_idempotente(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    _sembrar(config, 3)
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", _FakeAEB())
    enviar_a_destino(config)
    # Segunda corrida: nada nuevo que enviar (cursor drenado).
    fake2 = _FakeAEB()
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", fake2)
    r2 = enviar_a_destino(config)
    assert r2.entidades == 0 and r2.lotes == 0 and not fake2.lotes
    # reiniciar reenvía TODO desde cero.
    fake3 = _FakeAEB()
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", fake3)
    r3 = enviar_a_destino(config, reiniciar=True)
    assert r3.entidades == 3


def test_entidad_modificada_se_reenvia(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    curp = _curp("GOMC900101HDFXYZ0")
    proyectar(config, PERSONA_FZ1, ASIGN, [{"curp": curp, "primer_nombre": "Carlos"}])
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", _FakeAEB())
    enviar_a_destino(config)
    # Fusión que rellena un campo nuevo → bumpea actualizado_en → el cursor la vuelve a tomar.
    proyectar(config, PERSONA_FZ1, ASIGN, [{"curp": curp, "apellido_paterno": "Gómez"}])
    fake = _FakeAEB()
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", fake)
    r = enviar_a_destino(config)
    assert r.entidades == 1


def test_falla_de_red_da_mensaje_claro(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    _sembrar(config, 1)
    monkeypatch.setattr(
        "normalizacion.entidades.envio._post_json",
        lambda u, h, c: (0, {"detail": "no se pudo conectar: [Errno 111] Connection refused"}),
    )
    r = enviar_a_destino(config)
    assert r.lotes == 0 and r.detuvo_en and "No se pudo conectar" in r.detuvo_en
    assert r.errores


def test_rechazo_de_clave_da_mensaje_claro(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    _sembrar(config, 1)
    monkeypatch.setattr(
        "normalizacion.entidades.envio._post_json",
        lambda u, h, c: (401, {"detail": "X-API-Key inválida"}),
    )
    r = enviar_a_destino(config)
    assert "clave de ingesta" in (r.detuvo_en or "")


def test_sin_token_no_intenta_enviar(config: Config) -> None:
    guardar_destino(config, {"habilitado": True, "url": "http://aeb.local",
                             "auth_token": "", "lote": 2, "intervalo_seg": 0})
    r = enviar_a_destino(config)
    assert r.lotes == 0 and "clave de ingesta" in (r.detuvo_en or "")


def test_fallos_por_item_se_anotan_con_codigos(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    _sembrar(config, 2)

    def _con_fallos(url: Any, headers: Any, cuerpo: dict[str, Any]) -> Any:
        n = len(cuerpo["entidades"])
        return 207, {
            "recibidas": n, "creadas": n - 1, "actualizadas": 0, "sin_cambio": 0, "fallidas": 1,
            "resultados": [
                {"estado": "creada"},
                {"estado": "error", "codigo": "tipo_relacion_invalido"},
            ],
        }

    monkeypatch.setattr("normalizacion.entidades.envio._post_json", _con_fallos)
    r = enviar_a_destino(config)
    assert r.fallidas == 1
    assert any("tipo_relacion_invalido" in e for e in r.errores)


def test_estado_incluye_ultimo_intento_ok(config: Config, monkeypatch: Any) -> None:
    _habilitar(config, intervalo_seg=300)
    _sembrar(config, 1)
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", _FakeAEB())
    enviar_a_destino(config)
    est = estado_envio(config)
    assert est["intervalo_seg"] == 300
    assert est["ultimo"] and est["ultimo"]["ok"] is True and est["ultimo"]["entidades"] == 1


def test_ultimo_intento_registra_el_fallo(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    _sembrar(config, 1)
    monkeypatch.setattr(
        "normalizacion.entidades.envio._post_json", lambda u, h, c: (0, {"detail": "caído"}),
    )
    enviar_a_destino(config)
    ultimo = estado_envio(config)["ultimo"]
    assert ultimo and ultimo["ok"] is False and ultimo["detuvo_en"]


def test_pasada_automatica_envia_y_devuelve_intervalo(config: Config, monkeypatch: Any) -> None:
    from normalizacion.entidades.envio import _pasada

    _habilitar(config, intervalo_seg=30)
    _sembrar(config, 1)
    fake = _FakeAEB()
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", fake)
    espera = _pasada(config)
    assert espera == 30 and fake.lotes  # envió y pide esperar el intervalo configurado


def test_pasada_manual_no_envia(config: Config, monkeypatch: Any) -> None:
    from normalizacion.entidades.envio import _pasada

    _habilitar(config, intervalo_seg=0)  # 0 = solo manual
    _sembrar(config, 1)
    fake = _FakeAEB()
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", fake)
    espera = _pasada(config)
    assert espera == 15 and not fake.lotes  # no envía en automático; re-checa pronto


def test_estado_envio_cuenta_pendientes(config: Config, monkeypatch: Any) -> None:
    _habilitar(config)
    _sembrar(config, 2)
    est = estado_envio(config)
    assert est["habilitado"] is True and est["pendientes"] == 2 and est["cursor"] is None
    monkeypatch.setattr("normalizacion.entidades.envio._post_json", _FakeAEB())
    enviar_a_destino(config)
    est2 = estado_envio(config)
    assert est2["pendientes"] == 0 and est2["cursor"] is not None
