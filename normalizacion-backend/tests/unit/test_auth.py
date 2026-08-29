"""Login, sesiones y roles — las piezas que se pueden probar sin Postgres.

Lo que toca la BD (`usuarios`, `sesiones`) vive en `tests/integracion/test_login.py`.
Aquí quedan las decisiones puras, que son justo donde un error pasa desapercibido:
la jerarquía de roles, la política de contraseñas y el freno de fuerza bruta.
"""

from __future__ import annotations

import pytest

from normalizacion.api import roles
from normalizacion.api.contrasena import (
    LONGITUD_MINIMA,
    ContrasenaDebil,
    cifrar,
    exigir_politica,
    verificar,
)
from normalizacion.api.seguridad import FrenoDeIntentos


class TestRoles:
    """La jerarquía es acumulativa: quien puede más, puede lo de menos."""

    @pytest.mark.parametrize(
        ("rol", "minimo", "espera"),
        [
            ("lector", "lector", True),
            ("lector", "operador", False),
            ("lector", "admin", False),
            ("operador", "lector", True),
            ("operador", "operador", True),
            ("operador", "admin", False),
            ("admin", "lector", True),
            ("admin", "operador", True),
            ("admin", "admin", True),
        ],
    )
    def test_alcanza_respeta_la_jerarquia(self, rol: str, minimo: str, espera: bool) -> None:
        assert roles.alcanza(rol, minimo) is espera  # type: ignore[arg-type]

    def test_un_rol_desconocido_no_alcanza_nada(self) -> None:
        """Si un valor corrupto llegara a la columna `rol`, el sistema debe NEGAR.
        Un `.get(rol, 99)` mal puesto aquí convertiría basura en superusuario."""
        for minimo in ("lector", "operador", "admin"):
            assert roles.alcanza("superadmin", minimo) is False  # type: ignore[arg-type]
            assert roles.alcanza("", minimo) is False  # type: ignore[arg-type]

    def test_la_migracion_y_el_codigo_conocen_los_mismos_roles(self) -> None:
        """El CHECK de la 0008 y `ROLES` tienen que decir lo mismo: si divergen, se
        puede asignar por API un rol que la BD rechaza (o al revés)."""
        from pathlib import Path

        raiz = Path(__file__).resolve().parents[2]
        migracion = raiz / "alembic" / "versions" / "0008_usuarios.py"
        crudo = migracion.read_text(encoding="utf-8")
        for rol in roles.ROLES:
            assert f"'{rol}'" in crudo, f"{rol} falta en el CHECK de la migración"


class TestContrasena:
    def test_rechaza_las_cortas(self) -> None:
        with pytest.raises(ContrasenaDebil):
            exigir_politica("x" * (LONGITUD_MINIMA - 1))

    def test_acepta_la_longitud_justa(self) -> None:
        exigir_politica("x" * LONGITUD_MINIMA)  # no levanta

    def test_rechaza_solo_espacios(self) -> None:
        """Larga pero vacía: cumple la longitud y no es un secreto."""
        with pytest.raises(ContrasenaDebil):
            exigir_politica(" " * (LONGITUD_MINIMA + 4))

    def test_no_pide_composicion(self) -> None:
        """Doce caracteres cualesquiera valen. Exigir símbolos empuja a `Password1!`,
        que es lo primero que prueba cualquier diccionario."""
        exigir_politica("caballo correcto grapa")

    def test_el_hash_no_contiene_la_contrasena(self) -> None:
        secreto = "una contraseña larguísima"
        assert secreto not in cifrar(secreto)

    def test_dos_hashes_de_lo_mismo_difieren(self) -> None:
        """Sal distinta por hash: si no, dos usuarios con la misma contraseña se
        delatan mutuamente con solo mirar la tabla."""
        assert cifrar("misma contraseña") != cifrar("misma contraseña")

    def test_verificar_acepta_la_buena_y_rechaza_la_mala(self) -> None:
        h = cifrar("la contraseña buena")
        assert verificar(h, "la contraseña buena") is True
        assert verificar(h, "la contraseña mala") is False

    def test_verificar_no_revienta_con_un_hash_corrupto(self) -> None:
        """Una fila estropeada no debe tumbar el login de todo el mundo."""
        assert verificar("no soy un hash argon2", "lo que sea") is False


class TestFrenoDeIntentos:
    def test_bloquea_al_llegar_al_maximo(self) -> None:
        freno = FrenoDeIntentos(max_intentos=3, bloqueo_seg=60)
        assert freno.bloqueado("u:ana") == 0
        for _ in range(2):
            freno.registrar_fallo("u:ana")
        assert freno.bloqueado("u:ana") == 0, "dos fallos aún no bloquean"
        freno.registrar_fallo("u:ana")
        assert freno.bloqueado("u:ana") > 0

    def test_un_acierto_limpia_la_racha(self) -> None:
        freno = FrenoDeIntentos(max_intentos=3, bloqueo_seg=60)
        freno.registrar_fallo("u:ana")
        freno.registrar_fallo("u:ana")
        freno.registrar_exito("u:ana")
        freno.registrar_fallo("u:ana")
        assert freno.bloqueado("u:ana") == 0, "la racha debía empezar de cero"

    def test_las_cuentas_no_se_bloquean_entre_si(self) -> None:
        freno = FrenoDeIntentos(max_intentos=2, bloqueo_seg=60)
        freno.registrar_fallo("u:ana")
        freno.registrar_fallo("u:ana")
        assert freno.bloqueado("u:ana") > 0
        assert freno.bloqueado("u:beto") == 0

    def test_bloquear_por_ip_frena_el_barrido_de_cuentas(self) -> None:
        """Un atacante que prueba UNA contraseña en muchas cuentas no gasta el cupo
        de ninguna. Por eso el login registra el fallo con las dos llaves a la vez."""
        freno = FrenoDeIntentos(max_intentos=3, bloqueo_seg=60)
        for victima in ("ana", "beto", "carla"):
            freno.registrar_fallo(f"u:{victima}", "ip:10.0.0.1")
        assert freno.bloqueado("u:ana") == 0, "cada cuenta solo acumuló un fallo"
        assert freno.bloqueado("ip:10.0.0.1") > 0, "pero la IP sí queda frenada"

    def test_bloqueado_devuelve_el_mayor_de_las_llaves(self) -> None:
        freno = FrenoDeIntentos(max_intentos=1, bloqueo_seg=60)
        freno.registrar_fallo("ip:10.0.0.1")
        assert freno.bloqueado("u:ana", "ip:10.0.0.1") > 0
