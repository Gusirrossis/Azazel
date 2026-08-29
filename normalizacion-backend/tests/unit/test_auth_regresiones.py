"""Regresiones de seguridad encontradas en la auditoría del login.

Cada test de aquí corresponde a un defecto REAL que llegó a estar en la rama. Son
todos del mismo tipo: fallos que no rompen nada visible, no aparecen en ningún log y
solo se notan cuando alguien los aprovecha. Sin un test que los fije, el próximo
refactor los reintroduce sin que nadie se entere.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from normalizacion.api import claves_busqueda, usuarios
from normalizacion.api.seguridad import FrenoDeIntentos
from normalizacion.core.config import Config


@pytest.fixture(autouse=True)
def _sin_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construir la app toca Postgres tres veces (overrides, higiene, recetas) y cada
    intento gasta su `connect_timeout` contra un puerto cerrado. Aquí solo se prueba
    la DECISIÓN de autorización, así que se cortan esos caminos: el test debe medir
    la lógica, no la latencia de un socket que nunca contesta."""
    monkeypatch.setattr(
        "normalizacion.core.config_overrides.aplicar_recursos", lambda cfg: cfg
    )
    monkeypatch.setattr("normalizacion.api.sesiones.barrer", lambda cfg: 0)
    monkeypatch.setattr("normalizacion.entidades.envio.iniciar_bucle", lambda cfg: None)
    monkeypatch.setattr("normalizacion.entidades.recetas_db.seed_recetas", lambda conn: None)


def _app(monkeypatch: pytest.MonkeyPatch, *, hay_usuarios: bool, claves: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    """App con la BD simulada: sin tocar Postgres, se fija si hay usuarios y claves."""
    from normalizacion.api.main import crear_app

    monkeypatch.setattr(usuarios, "hay_alguno_cacheado", lambda cfg: hay_usuarios)
    monkeypatch.setattr(usuarios, "hay_alguno", lambda cfg: hay_usuarios)
    monkeypatch.setattr(claves_busqueda, "hay_alguna", lambda cfg: bool(claves))
    monkeypatch.setattr(claves_busqueda, "coincide", lambda cfg, k: k in claves)
    # `sesiones.validar` solo se llama si llega cookie, y aquí nunca llega.
    return crear_app(Config(_env_file=None, api_keys=()))


class TestClaveInventada:
    """El defecto más grave de la rama: `claves_busqueda.autorizada` devuelve True
    cuando NO hay ninguna clave configurada (su modo abierto de dev), y `_autorizar`
    lo usaba como comprobación de identidad. Resultado: cualquier cabecera
    `X-API-Key: loquesea` entraba como `lector` aunque ya existieran usuarios — el
    índice entero y la descarga de todos los originales, saltándose el login."""

    def test_una_clave_inventada_no_entra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = TestClient(_app(monkeypatch, hay_usuarios=True))
        r = c.get("/auth/yo", headers={"X-API-Key": "inventada"})
        assert r.status_code == 401

    def test_tampoco_con_claves_configuradas_si_no_coincide(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = TestClient(_app(monkeypatch, hay_usuarios=True, claves=("bus_buena",)))
        assert c.get("/auth/yo", headers={"X-API-Key": "bus_mala"}).status_code == 401

    def test_la_clave_buena_si_entra_como_lector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        c = TestClient(_app(monkeypatch, hay_usuarios=True, claves=("bus_buena",)))
        r = c.get("/auth/yo", headers={"X-API-Key": "bus_buena"})
        assert r.status_code == 200
        assert r.json()["rol"] == "lector"

    def test_sin_credencial_no_entra_si_hay_usuarios(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = TestClient(_app(monkeypatch, hay_usuarios=True))
        assert c.get("/auth/yo").status_code == 401

    def test_una_instalacion_virgen_si_abre(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin usuarios NI claves hay que poder crear el primer admin."""
        c = TestClient(_app(monkeypatch, hay_usuarios=False))
        r = c.get("/auth/yo")
        assert r.status_code == 200
        assert r.json()["rol"] == "admin"

    def test_una_clave_cierra_la_puerta_aunque_no_haya_usuarios(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La puerta abierta miraba solo `cfg.api_keys` y se saltaba las claves CON
        NOMBRE: una instalación protegida solo con ellas quedaba abierta de par en par."""
        c = TestClient(_app(monkeypatch, hay_usuarios=False, claves=("bus_x",)))
        assert c.get("/auth/yo").status_code == 401


class TestCacheDeUsuariosFallaCerrado:
    """`hay_alguno_cacheado` sellaba el TTL ANTES de consultar. Cuando la consulta
    fallaba devolvía True solo en ESA llamada, y las siguientes leían del caché un
    False que la base de datos nunca dio — o sea, rol admin anónimo durante 5 s cada
    vez que Postgres parpadeara."""

    @pytest.fixture(autouse=True)
    def _limpio(self) -> Any:
        usuarios._reiniciar_cache_usuarios()
        yield
        usuarios._reiniciar_cache_usuarios()

    def test_un_fallo_de_bd_no_abre_la_ventana(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def revienta(cfg: Any) -> bool:
            raise RuntimeError("postgres no responde")

        monkeypatch.setattr(usuarios, "hay_alguno", revienta)
        cfg = Config(_env_file=None)
        # Las tres seguidas, dentro del TTL: TODAS deben decir "cerrado".
        assert [usuarios.hay_alguno_cacheado(cfg) for _ in range(3)] == [True, True, True]

    def test_el_true_confirmado_se_engancha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llamadas: list[int] = []

        def con_usuarios(cfg: Any) -> bool:
            llamadas.append(1)
            return True

        monkeypatch.setattr(usuarios, "hay_alguno", con_usuarios)
        cfg = Config(_env_file=None)
        assert usuarios.hay_alguno_cacheado(cfg) is True
        assert usuarios.hay_alguno_cacheado(cfg) is True
        assert len(llamadas) == 1, "el latch debe evitar la segunda consulta"

    def test_el_false_confirmado_si_se_respeta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una instalación realmente vacía sí tiene que poder arrancar abierta."""
        monkeypatch.setattr(usuarios, "hay_alguno", lambda cfg: False)
        assert usuarios.hay_alguno_cacheado(Config(_env_file=None)) is False


class TestIpDeConfianza:
    """`_ip_cliente` tomaba la entrada IZQUIERDA de X-Forwarded-For, que la escribe el
    cliente (`$proxy_add_x_forwarded_for` añade la real por detrás). Con eso, el freno
    del login por IP se esquiva mandando una cabecera distinta en cada intento."""

    def _ip(self, cabeceras: dict[str, str]) -> str:
        from normalizacion.api.main import _ip_cliente

        class _Req:
            headers = cabeceras
            client = type("C", (), {"host": "10.0.0.9"})()

        return _ip_cliente(_Req())  # type: ignore[arg-type]

    def test_no_se_cree_la_izquierda_de_forwarded_for(self) -> None:
        ip = self._ip({"x-forwarded-for": "1.2.3.4, 203.0.113.7"})
        assert ip != "1.2.3.4", "esa entrada la escribe el cliente"
        assert ip == "203.0.113.7", "la de la derecha la añadió nuestro propio proxy"

    def test_prefiere_x_real_ip(self) -> None:
        assert self._ip({"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"}) == "203.0.113.7"

    def test_sin_cabeceras_usa_el_socket(self) -> None:
        assert self._ip({}) == "10.0.0.9"


class TestFrenoAcotado:
    """El login es ANÓNIMO: cualquiera inventa un usuario distinto en cada intento.
    Sin tope, cada uno dejaba una entrada que nunca se borraba."""

    def test_la_memoria_no_crece_sin_limite(self) -> None:
        freno = FrenoDeIntentos(max_intentos=5, bloqueo_seg=300)
        for i in range(FrenoDeIntentos._MAX_CLAVES + 500):
            freno.registrar_fallo(f"u:inventado{i}")
        assert len(freno._fallos) <= FrenoDeIntentos._MAX_CLAVES + 1

    def test_un_bloqueo_vigente_no_se_desaloja(self) -> None:
        """Si no, llenar la tabla de basura sería la forma de limpiarse el castigo."""
        freno = FrenoDeIntentos(max_intentos=2, bloqueo_seg=300)
        freno.registrar_fallo("u:victima")
        freno.registrar_fallo("u:victima")
        assert freno.bloqueado("u:victima") > 0
        for i in range(FrenoDeIntentos._MAX_CLAVES + 100):
            freno.registrar_fallo(f"u:ruido{i}")
        assert freno.bloqueado("u:victima") > 0, "el bloqueo debe sobrevivir a la poda"


class TestCorsSinComodin:
    """`allow_credentials` + `*` convierte la cookie de sesión en credencial
    cross-site: Starlette refleja el Origin de quien pregunte."""

    def test_el_comodin_se_descarta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from normalizacion.api.main import crear_app

        monkeypatch.setattr(usuarios, "hay_alguno", lambda cfg: False)
        app = crear_app(Config(_env_file=None, api_cors_origenes=("*", "https://panel.example")))
        cors = next(m for m in app.user_middleware if "CORS" in str(m))
        assert "*" not in cors.kwargs["allow_origins"]
        assert "https://panel.example" in cors.kwargs["allow_origins"]
