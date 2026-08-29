"""Integración: el ciclo completo de login contra un Postgres real.

Requiere NORM_POSTGRES_DSN apuntando a un Postgres vivo (perfil `cola` del compose)
con las migraciones aplicadas (`alembic upgrade head`).

Va por la API entera con TestClient, no por los módulos sueltos: lo que hay que
comprobar es que la cookie se pone, viaja de vuelta y abre las puertas que le
corresponden — y eso solo se ve de extremo a extremo.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

pytestmark = [
    pytest.mark.integracion,
    pytest.mark.skipif(
        not os.environ.get("NORM_POSTGRES_DSN"),
        reason="requiere NORM_POSTGRES_DSN (docker compose --profile cola up)",
    ),
]

CLAVE_BUENA = "una contraseña larga y buena"


@pytest.fixture
def config():  # type: ignore[no-untyped-def]
    from normalizacion.core.config import cargar_config

    cfg = cargar_config()
    # En los tests el cliente habla HTTP: con `Secure` el navegador simulado
    # descartaría la cookie y todo fallaría por un motivo que no es el del test.
    return cfg.model_copy(update={"sesion_cookie_secure": False, "api_keys": ()})


@pytest.fixture
def cliente(config) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    from normalizacion.api.main import crear_app

    with TestClient(crear_app(config)) as c:
        yield c


@pytest.fixture
def usuario_admin(config):  # type: ignore[no-untyped-def]
    """Un admin recién creado, con nombre único para no chocar entre corridas."""
    from normalizacion.api import usuarios

    nombre = f"prueba_{uuid.uuid4().hex[:10]}"
    creado = usuarios.crear(config, nombre, CLAVE_BUENA, rol="admin")
    yield creado
    _borrar(config, creado.id)


@pytest.fixture
def usuario_lector(config):  # type: ignore[no-untyped-def]
    from normalizacion.api import usuarios

    nombre = f"prueba_{uuid.uuid4().hex[:10]}"
    creado = usuarios.crear(config, nombre, CLAVE_BUENA, rol="lector")
    yield creado
    _borrar(config, creado.id)


def _borrar(config, usuario_id: int) -> None:  # type: ignore[no-untyped-def]
    import psycopg

    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        conn.execute("DELETE FROM usuarios WHERE id = %s", (usuario_id,))
        conn.commit()


def _entrar(cliente: TestClient, usuario: str, clave: str = CLAVE_BUENA):  # type: ignore[no-untyped-def]
    return cliente.post("/auth/login", json={"usuario": usuario, "contrasena": clave})


class TestLogin:
    def test_credenciales_buenas_abren_sesion(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        r = _entrar(cliente, usuario_admin.usuario)
        assert r.status_code == 200
        assert r.json()["rol"] == "admin"
        assert "norm_sesion" in r.cookies

    def test_la_cookie_es_httponly(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        """El motivo de todo este cambio: un XSS no puede leerla. Si alguien quita
        el flag, el panel vuelve a ser tan vulnerable como con localStorage."""
        r = _entrar(cliente, usuario_admin.usuario)
        cabecera = r.headers["set-cookie"].lower()
        assert "httponly" in cabecera
        assert "samesite=strict" in cabecera

    def test_la_contrasena_mala_no_abre_nada(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        r = _entrar(cliente, usuario_admin.usuario, "no es la buena, pero es larga")
        assert r.status_code == 401
        assert "norm_sesion" not in r.cookies

    def test_el_error_no_distingue_usuario_inexistente(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        """Mismo código y mismo mensaje: si difirieran, se podría averiguar qué
        cuentas existen probando nombres."""
        malo = _entrar(cliente, usuario_admin.usuario, "clave equivocada larga")
        fantasma = _entrar(cliente, f"nadie_{uuid.uuid4().hex[:8]}", CLAVE_BUENA)
        assert malo.status_code == fantasma.status_code == 401
        assert malo.json()["detail"] == fantasma.json()["detail"]

    def test_el_usuario_desactivado_no_entra(self, cliente, config, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        from normalizacion.api import usuarios

        usuarios.actualizar(config, usuario_admin.id, activo=False)
        assert _entrar(cliente, usuario_admin.usuario).status_code == 401

    def test_el_usuario_se_normaliza(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        """`ANA` y `ana` son la misma cuenta al entrar."""
        assert _entrar(cliente, usuario_admin.usuario.upper()).status_code == 200


class TestSesion:
    def test_yo_devuelve_la_identidad(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_admin.usuario)
        r = cliente.get("/auth/yo")
        assert r.status_code == 200
        assert r.json()["usuario"] == usuario_admin.usuario

    def test_sin_cookie_no_hay_identidad(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        assert cliente.get("/auth/yo").status_code == 401

    def test_logout_invalida_la_sesion(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_admin.usuario)
        assert cliente.post("/auth/logout").status_code == 200
        assert cliente.get("/auth/yo").status_code == 401

    def test_revocar_corta_una_sesion_ya_abierta(self, cliente, config, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        """Lo que un JWT no puede hacer sin lista negra: echar a alguien YA."""
        from normalizacion.api import sesiones

        _entrar(cliente, usuario_admin.usuario)
        assert cliente.get("/auth/yo").status_code == 200
        sesiones.revocar_todas(config, usuario_admin.id)
        assert cliente.get("/auth/yo").status_code == 401

    def test_desactivar_al_usuario_corta_sus_sesiones(self, cliente, config, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        from normalizacion.api import usuarios

        _entrar(cliente, usuario_admin.usuario)
        usuarios.actualizar(config, usuario_admin.id, activo=False)
        assert cliente.get("/auth/yo").status_code == 401

    def test_cambiar_contrasena_exige_la_actual(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_admin.usuario)
        r = cliente.post(
            "/auth/contrasena",
            json={"actual": "no es la mía pero es larga", "nueva": "otra contraseña larga"},
        )
        assert r.status_code == 403

    def test_cambiar_contrasena_rechaza_las_debiles(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_admin.usuario)
        r = cliente.post("/auth/contrasena", json={"actual": CLAVE_BUENA, "nueva": "corta"})
        assert r.status_code == 400


class TestRoles:
    def test_un_lector_no_administra(self, cliente, usuario_lector) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_lector.usuario)
        r = cliente.get("/auth/usuarios")
        assert r.status_code == 403, "un lector no debe poder listar usuarios"

    def test_un_lector_no_edita_el_filtro(self, cliente, usuario_lector) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_lector.usuario)
        assert cliente.delete("/filtro").status_code == 403

    def test_un_lector_no_ve_las_claves(self, cliente, usuario_lector) -> None:  # type: ignore[no-untyped-def]
        """Antes, cualquiera con la API key podía revocar las claves de todos."""
        _entrar(cliente, usuario_lector.usuario)
        assert cliente.get("/seguridad/claves-busqueda").status_code == 403

    def test_un_admin_si_administra(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        _entrar(cliente, usuario_admin.usuario)
        assert cliente.get("/auth/usuarios").status_code == 200

    def test_403_y_no_401_cuando_falta_rol(self, cliente, usuario_lector) -> None:  # type: ignore[no-untyped-def]
        """401 mandaría al front a pedir login otra vez, en bucle: la sesión ya es
        válida, lo que falta es permiso."""
        _entrar(cliente, usuario_lector.usuario)
        assert cliente.get("/auth/usuarios").status_code == 403


class TestClaveDeMaquina:
    def test_una_clave_con_nombre_entra_como_lector(self, config, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        """Las claves de consumidor sirven para buscar, no para administrar."""
        from normalizacion.api import claves_busqueda
        from normalizacion.api.main import crear_app

        nombre = f"consumidor_{uuid.uuid4().hex[:8]}"
        clave = claves_busqueda.generar_clave(config, nombre)
        try:
            with TestClient(crear_app(config)) as c:
                cabecera = {"X-API-Key": clave}
                assert c.get("/auth/yo", headers=cabecera).status_code == 200
                assert c.get("/auth/usuarios", headers=cabecera).status_code == 403
        finally:
            claves_busqueda.revocar_clave(config, nombre)

    def test_una_clave_invalida_se_rechaza(self, cliente, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        r = cliente.get("/auth/yo", headers={"X-API-Key": "bus_inventada"})
        assert r.status_code == 401


class TestFuerzaBruta:
    def test_bloquea_tras_varios_fallos(self, cliente, config, usuario_admin) -> None:  # type: ignore[no-untyped-def]
        for _ in range(config.login_max_intentos):
            _entrar(cliente, usuario_admin.usuario, "clave equivocada larga")
        r = _entrar(cliente, usuario_admin.usuario)
        assert r.status_code == 429, "la contraseña buena tampoco pasa durante el bloqueo"
        assert "Retry-After" in r.headers
