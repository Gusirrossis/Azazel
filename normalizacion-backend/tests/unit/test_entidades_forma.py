"""La forma de la entidad: qué se guarda y qué se recalcula.

Medido antes del cambio: una entidad resuelta desde una CURP encontrada en texto —el
único caso que produce el backfill— guardaba 27 claves de las que 20 estaban vacías,
y de las 7 con valor, tres eran copias (`curp` ≡ `normalized_curp`, `sexo` ≡
`normalized_sex`). 597 bytes para transportar 18 caracteres de información.

Estos tests fijan las dos mitades del trato: se guarda poco, y al leer se ve todo.
"""

from __future__ import annotations

import json

from normalizacion.entidades import derivados
from normalizacion.entidades.pipeline import construir_entidad
from normalizacion.entidades.receta import obtener_receta

CURP = "MAAJ800101HDFRRN09"


def _entidad_del_backfill() -> dict:
    r = obtener_receta("persona")
    ent = construir_entidad(r, {"curp": "curp", "rfc": "rfc"}, {"curp": CURP})
    assert ent is not None
    return ent["campos"]


class TestSeGuardaPoco:
    def test_las_claves_vacias_no_se_escriben(self) -> None:
        c = _entidad_del_backfill()
        vacias = [k for k, v in c.items() if v in (None, "", {}, [])]
        assert not vacias, f"no deberían guardarse claves vacías: {vacias}"

    def test_los_subobjetos_vacios_desaparecen(self) -> None:
        """`direccion` con sus ocho claves nulas era el mayor desperdicio."""
        c = _entidad_del_backfill()
        assert "direccion" not in c
        assert "nombre" not in c

    def test_no_se_guarda_lo_derivable(self) -> None:
        """`normalizados` es una copia de las anclas más dos derivaciones que se
        recalculan desde la CURP en microsegundos."""
        c = _entidad_del_backfill()
        assert "normalizados" not in c
        assert "edad" not in c, "la edad envejece: guardarla es garantizar que se pudra"

    def test_la_ficha_encoge_de_verdad(self) -> None:
        c = _entidad_del_backfill()
        assert len(json.dumps(c, ensure_ascii=False)) < 200, "antes eran 597 B"

    def test_pero_conserva_lo_que_importa(self) -> None:
        assert _entidad_del_backfill()["curp"] == CURP


class TestAlLeerSeVeTodo:
    """El contrato no cambia: `proyeccion.py` lee `normalizados.normalized_dob` por
    ruta, y el panel espera la ficha completa."""

    def test_enriquecer_devuelve_el_bloque_normalizados(self) -> None:
        lleno = derivados.enriquecer(_entidad_del_backfill())
        n = lleno["normalizados"]
        assert n["normalized_curp"] == CURP
        assert n["normalized_dob"] == "1980-01-01"
        assert n["normalized_sex"] == "H"
        assert n["normalized_estado"]

    def test_la_edad_se_calcula_al_leer(self) -> None:
        assert derivados.enriquecer(_entidad_del_backfill())["edad"]

    def test_no_muta_la_entrada(self) -> None:
        c = _entidad_del_backfill()
        antes = json.dumps(c, sort_keys=True)
        derivados.enriquecer(c)
        assert json.dumps(c, sort_keys=True) == antes

    def test_una_entidad_sin_ancla_valida_no_revienta(self) -> None:
        """Basura en la columna no puede tumbar una lectura."""
        lleno = derivados.enriquecer({"curp": "NO-ES-UNA-CURP"})
        assert lleno["normalizados"]["normalized_dob"] is None


class TestFichaBreve:
    """Lo que viaja a un consumidor externo."""

    def test_no_lleva_procedencias(self) -> None:
        """Pueden ser cientos de rutas de archivo por entidad."""
        b = derivados.ficha_breve({"curp": CURP, "procedencias": [{"ruta": "x"}] * 300})
        assert "procedencias" not in b

    def test_no_lleva_claves_vacias(self) -> None:
        b = derivados.ficha_breve({"curp": CURP})
        assert all(v for v in b.values())

    def test_lleva_lo_derivado(self) -> None:
        b = derivados.ficha_breve({"curp": CURP})
        assert b["curp"] == CURP
        assert b["sexo"] == "H"
        assert b["edad"]
