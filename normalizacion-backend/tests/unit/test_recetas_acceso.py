"""Motor de resolución multi-tipo: la receta ACCESO (credenciales) y que persona siga intacta.

El motor pasó a ser data-driven por receta: elige el ancla de los campos `es_ancla` (en orden de
declaración) y arma `campos` planos para tipos no-persona. Persona conserva su forma anidada.
"""

from __future__ import annotations

from normalizacion.entidades.modelo import AnclaTipo
from normalizacion.entidades.pipeline import construir_entidad
from normalizacion.entidades.receta import ACCESO, PERSONA_FZ1
from normalizacion.entidades.normalizadores import digito_verificador_curp


def _curp(prefijo17: str) -> str:
    return prefijo17 + digito_verificador_curp(prefijo17)


def test_receta_acceso_kind_y_ancla() -> None:
    assert ACCESO.kind == "acceso"
    anclas = [c.nombre for c in ACCESO.campos if c.es_ancla]
    assert anclas == ["email"]  # el correo es el ancla


def test_construir_acceso_campos_planos_ancla_email() -> None:
    asignacion = {"correo": "email", "user": "usuario", "pass": "contrasena", "breach": "fuente"}
    fila = {"correo": "Alice@Example.com", "user": "alice", "pass": "hunter2", "breach": "ColeccionX"}
    ent = construir_entidad(ACCESO, asignacion, fila)
    assert ent is not None
    assert ent["ancla_tipo"] == AnclaTipo.EMAIL
    assert ent["ancla_valor"] == "alice@example.com"  # normalizado a minúsculas
    c = ent["campos"]
    assert c["email"] == "alice@example.com" and c["usuario"] == "alice"
    assert c["contrasena"] == "hunter2" and c["fuente"] == "ColeccionX"
    # forma PLANA: sin las claves anidadas de persona
    assert "nombre" not in c and "direccion" not in c and "normalizados" not in c


def test_acceso_sin_correo_no_resuelve() -> None:
    # sin email (el ancla) → va a resolución difusa (None), aunque tenga usuario/contraseña
    ent = construir_entidad(ACCESO, {"u": "usuario", "p": "contrasena"}, {"u": "bob", "p": "x"})
    assert ent is None


def test_persona_sigue_intacta() -> None:
    """Regresión: persona conserva ancla CURP y su forma anidada AL LEERLA.

    Lo GUARDADO ya no es la forma completa: las claves vacías y lo derivable de las
    anclas dejaron de escribirse (una ficha del backfill tenía 20 de 27 claves
    vacías). `direccion` sin ni un dato ya no se almacena, y `normalizados` se
    recalcula al leer. El contrato de lectura no cambia: `derivados.enriquecer` lo
    reconstruye, que es por donde pasan la proyección al AEB y el panel.
    """
    from normalizacion.entidades import derivados

    curp = _curp("MERV960314MDFNSL0")
    ent = construir_entidad(PERSONA_FZ1, {"curp": "curp", "nombre": "nombre1"},
                            {"curp": curp, "nombre": "Valeria"})
    assert ent is not None
    assert ent["ancla_tipo"] == AnclaTipo.CURP and ent["ancla_valor"] == curp

    guardado = ent["campos"]
    assert guardado["nombre"]["nombre1"] == "Valeria"   # lo capturado SÍ se guarda
    assert "direccion" not in guardado                  # sin ni un dato: no se escribe
    assert "normalizados" not in guardado               # derivable de la CURP

    leido = derivados.enriquecer(guardado)
    assert "normalizados" in leido and leido["normalizados"]["normalized_curp"] == curp
    assert leido["nombre"]["nombre1"] == "Valeria"


def test_envio_mapea_kind_acceso() -> None:
    from normalizacion.entidades.envio import _item_aeb
    row = ("eid1", "acceso", "email", "alice@example.com",
           {"email": "alice@example.com", "usuario": "alice", "contrasena": "x"},
           1.0, "acceso-v1", "anclas-v1", None)
    item = _item_aeb(row)
    assert item["kind"] == "acceso"
    assert item["identificadores"]["email"] == "alice@example.com"
    assert item["ancla"] == {"tipo": "email", "valor": "alice@example.com"}
