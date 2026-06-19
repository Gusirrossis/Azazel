"""Mapeo entidad→cable canónico del AEB (sin BD)."""

from __future__ import annotations

import datetime as dt

from normalizacion.entidades.envio import _item_aeb


def test_item_aeb_extrae_identificadores_y_conserva_campos() -> None:
    campos = {
        "nombre": {"nombre1": "Ana"},
        "curp": "ABCD900101HDFXYZ01",
        "telefono": "5550001111",
        "email": "a@b.mx",
        "direccion": {"codigo_postal": "06500"},
        "atributos": {"placa": "XYZ123"},
    }
    row = ("eid1", "persona", "curp", "ABCD900101HDFXYZ01", campos, 0.9,
           "fz1-v1", "anclas-v1", dt.datetime(2026, 1, 1))
    item = _item_aeb(row)

    assert item["external_id"] == "eid1"          # = entidad_id de Azazel (llave puente)
    assert item["kind"] == "person"
    assert item["ancla"] == {"tipo": "curp", "valor": "ABCD900101HDFXYZ01"}
    # 'telefono' → 'phone'; cp y placas también se extraen como identificadores.
    assert item["identificadores"] == {
        "curp": "ABCD900101HDFXYZ01", "email": "a@b.mx", "phone": "5550001111",
        "cp": "06500", "placas": "XYZ123",
    }
    # La forma Fz1 completa viaja en campos (para que cada consumidor proyecte a la suya).
    assert item["campos"]["nombre"]["nombre1"] == "Ana"
    assert item["relaciones"] == [] and item["evidencias"] == []
    assert 0.0 <= item["confianza"] <= 1.0
    # Claves EXACTAS (el AEB rechaza extras con 422).
    assert set(item) == {"external_id", "kind", "confianza", "version", "ancla",
                         "campos", "identificadores", "relaciones", "evidencias"}


def test_item_aeb_sin_identificadores_no_inventa() -> None:
    row = ("eid2", "persona", "email", "x@y.mx", {"alias": "x"}, None,
           "fz1-v1", "anclas-v1", dt.datetime(2026, 1, 1))
    item = _item_aeb(row)
    assert item["identificadores"] == {}
    assert item["confianza"] == 1.0  # default cuando es None
