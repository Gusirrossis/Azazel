"""Tests de los normalizadores de la Fase 2 (anclas y campos de persona).

La CURP se prueba por ROUND-TRIP: se construye una CURP válida calculando su
propio dígito verificador, así el test no depende de conocer una CURP real (ni
filtra PII)."""

from __future__ import annotations

from datetime import date

from normalizacion.entidades import normalizadores as N


def _curp_valida(prefijo17: str) -> str:
    """prefijo de 17 chars → CURP de 18 con dígito verificador correcto."""
    return prefijo17 + N.digito_verificador_curp(prefijo17)


class TestCurp:
    def test_roundtrip_valida_y_deriva(self) -> None:
        curp = _curp_valida("MERV960314MDFNSL0")  # 1996-03-14, M, DF→CDMX
        r = N.validar_curp(curp)
        assert r.valido is True
        assert r.valor == curp
        assert r.derivados == {"dob": "1996-03-14", "sexo": "M", "estado": "CDMX"}

    def test_digito_incorrecto_invalida(self) -> None:
        curp = _curp_valida("MERV960314MDFNSL0")
        malo = curp[:17] + ("1" if curp[17] != "1" else "2")
        assert N.validar_curp(malo).valido is False

    def test_siglo_por_homoclave(self) -> None:
        # 17º char alfabético ('A') → nacido en los 2000
        curp = _curp_valida("FUCD050519HVZNNGA")  # 2005-05-19, H, VZ→Veracruz
        r = N.validar_curp(curp)
        assert r.valido is True
        assert r.derivados["dob"] == "2005-05-19"
        assert r.derivados["sexo"] == "H"
        assert r.derivados["estado"] == "Veracruz"

    def test_fecha_imposible_invalida(self) -> None:
        curp = _curp_valida("XEXX991331MDFXXX0")  # mes 13, día 31
        assert N.validar_curp(curp).valido is False

    def test_estado_desconocido_invalida(self) -> None:
        curp = _curp_valida("MERV960314MZZNSL0")  # 'ZZ' no es estado
        assert N.validar_curp(curp).valido is False

    def test_formato_basura_invalida(self) -> None:
        for basura in ["", "no-es-curp", "MERV960314MDFNSL", "1234567890123456789"]:
            assert N.validar_curp(basura).valido is False

    def test_conserva_crudo_y_normaliza_espacios(self) -> None:
        curp = _curp_valida("MERV960314MDFNSL0")
        r = N.validar_curp(f"  {curp.lower()}  ")
        assert r.valido is True and r.valor == curp and r.crudo == f"  {curp.lower()}  "


class TestRfc:
    def test_fisica_valida_deriva_fecha(self) -> None:
        r = N.validar_rfc("MERV9603143A2")
        assert r.valido is True
        assert r.derivados["dob"] == "1996-03-14"

    def test_basura_invalida(self) -> None:
        for b in ["", "MERV", "MERV9613993A2"]:  # mes 13
            assert N.validar_rfc(b).valido is False


class TestTelefono:
    def test_formatos_a_10_digitos(self) -> None:
        assert N.normalizar_telefono_mx("+52 55 2233 4455").valor == "5522334455"
        assert N.normalizar_telefono_mx("(55) 2233-4455").valor == "5522334455"
        assert N.normalizar_telefono_mx("5212223334455").valor == "2223334455"
        assert N.normalizar_telefono_mx("01 55 2233 4455").valor == "5522334455"  # lada antigua

    def test_invalido(self) -> None:
        assert N.normalizar_telefono_mx("123").valido is False


class TestEmailNombre:
    def test_email(self) -> None:
        assert N.normalizar_email("Valeria.Mendoza@Example.com").valor == "valeria.mendoza@example.com"
        assert N.normalizar_email("sin-arroba").valido is False

    def test_nombre_plegado(self) -> None:
        r = N.normalizar_nombre("José   MUÑOZ")
        assert r.valor == "José MUÑOZ"
        assert r.derivados["plegado"] == "jose munoz"


class TestEdad:
    def test_edad_con_fecha_fija(self) -> None:
        assert N.calcular_edad("1996-03-14", hoy=date(2026, 3, 13)) == 29
        assert N.calcular_edad("1996-03-14", hoy=date(2026, 3, 14)) == 30
