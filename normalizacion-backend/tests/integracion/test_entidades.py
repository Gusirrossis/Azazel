"""Integración de la Fase 2 (E1-E3): resolución por ancla, idempotencia, mapeo.

Verifica el invariante de oro: la misma CURP en distintas fuentes resuelve a UNA
sola entidad (sin duplicar), rellenando los campos faltantes de cada fuente.
"""

from __future__ import annotations

from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.entidades import mapeo
from normalizacion.entidades import normalizadores as N
from normalizacion.entidades.consultas import estadisticas, listar_entidades
from normalizacion.entidades.pipeline import construir_entidad, proyectar
from normalizacion.entidades.receta import PERSONA_FZ1

pytestmark = pytest.mark.integracion


def _curp(prefijo17: str) -> str:
    return prefijo17 + N.digito_verificador_curp(prefijo17)


CURP_VALERIA = _curp("MERV960314MDFNSL0")  # 1996-03-14, M, CDMX


@pytest.fixture()
def config(dsn: str, conexion: Any) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


ASIGN = {
    "curp": "curp", "primer_nombre": "nombre1", "apellido_paterno": "apellido1",
    "apellido_materno": "apellido2", "correo": "email", "telefono": "telefono",
    "municipio": "municipio",
}


class TestResolucionPorAncla:
    def test_misma_curp_dos_fuentes_una_entidad(self, config: Config) -> None:
        fuente_a = [{"curp": CURP_VALERIA, "primer_nombre": "Valeria",
                     "apellido_paterno": "Mendoza", "apellido_materno": "Rios"}]
        fuente_b = [{"curp": CURP_VALERIA, "correo": "valeria@example.com",
                     "telefono": "+52 55 2233 4455", "municipio": "Cuauhtemoc"}]

        r1 = proyectar(config, PERSONA_FZ1, ASIGN, fuente_a)
        assert (r1.entidades_nuevas, r1.entidades_fusionadas) == (1, 0)
        r2 = proyectar(config, PERSONA_FZ1, ASIGN, fuente_b)
        assert (r2.entidades_nuevas, r2.entidades_fusionadas) == (0, 1)  # FUSIÓN, no duplica

        ents = listar_entidades(config)["entidades"]
        assert len(ents) == 1
        c = ents[0]["campos"]
        # datos de AMBAS fuentes en la misma entidad
        assert c["nombre"]["nombre1"] == "Valeria"
        assert c["email"] == "valeria@example.com"
        assert c["telefono"] == "5522334455"
        # derivados de la CURP
        assert c["sexo"] == "M"
        assert c["normalizados"]["normalized_dob"] == "1996-03-14"
        assert c["normalizados"]["normalized_estado"] == "CDMX"
        assert len(ents[0]["procedencias"]) == 0  # sin procedencia pasada en este test

    def test_curps_distintas_dos_entidades(self, config: Config) -> None:
        otra = _curp("FUCD940519HDFNNG0")
        filas = [{"curp": CURP_VALERIA, "primer_nombre": "Valeria"},
                 {"curp": otra, "primer_nombre": "Diego"}]
        proyectar(config, PERSONA_FZ1, ASIGN, filas)
        assert estadisticas(config)["total"] == 2

    def test_sin_ancla_no_crea_entidad(self, config: Config) -> None:
        filas = [{"primer_nombre": "Juan", "apellido_paterno": "Perez"}]  # sin CURP/RFC/email/tel
        r = proyectar(config, PERSONA_FZ1, ASIGN, filas)
        assert (r.sin_ancla, r.entidades_nuevas) == (1, 0)
        assert estadisticas(config)["total"] == 0

    def test_idempotente_reejecutar(self, config: Config) -> None:
        filas = [{"curp": CURP_VALERIA, "primer_nombre": "Valeria"}]
        proyectar(config, PERSONA_FZ1, ASIGN, filas)
        proyectar(config, PERSONA_FZ1, ASIGN, filas)  # otra vez
        assert estadisticas(config)["total"] == 1  # no duplica

    def test_ancla_debil_email_si_no_hay_curp(self, config: Config) -> None:
        filas = [{"correo": "solo@example.com", "primer_nombre": "Ana"}]
        r = proyectar(config, PERSONA_FZ1, ASIGN, filas)
        assert r.entidades_nuevas == 1
        assert listar_entidades(config)["entidades"][0]["ancla_tipo"] == "email"


class TestMapeoPropuesto:
    def test_propone_por_nombre_y_contenido(self) -> None:
        columnas = ["CURP", "Nombre", "Apellido Paterno", "columna_rara"]
        muestras = {"CURP": [CURP_VALERIA, _curp("FUCD940519HDFNNG0")]}
        prop = mapeo.proponer_mapeo(PERSONA_FZ1, columnas, muestras)
        assert prop["CURP"]["campo"] == "curp"
        assert prop["CURP"]["confianza"] == 0.95  # por contenido validado
        assert prop["Nombre"]["campo"] == "nombre1"
        assert prop["Apellido Paterno"]["campo"] == "apellido1"
        assert prop["columna_rara"]["campo"] is None

    def test_huella_estable_por_forma(self) -> None:
        assert mapeo.huella_columnas(["CURP", "Nombre"]) == mapeo.huella_columnas(["nombre", "curp"])


class TestConstruirEntidad:
    def test_deriva_todo_de_curp(self) -> None:
        ent = construir_entidad(PERSONA_FZ1, {"curp": "curp"}, {"curp": CURP_VALERIA})
        assert ent is not None
        assert ent["ancla_tipo"].value == "curp"
        assert ent["campos"]["sexo"] == "M"
        assert ent["campos"]["normalizados"]["normalized_estado"] == "CDMX"

    def test_estado_nacimiento_no_es_estado_residencia(self) -> None:
        # CURP nace en CDMX (DF) pero RESIDE en Jalisco: no se mezclan.
        ent = construir_entidad(
            PERSONA_FZ1, {"curp": "curp", "estado": "estado"},
            {"curp": CURP_VALERIA, "estado": "Jalisco"},
        )
        assert ent["campos"]["normalizados"]["normalized_estado"] == "CDMX"  # nacimiento (CURP)
        assert ent["campos"]["direccion"]["estado"] == "Jalisco"  # residencia (input)

    def test_sin_curp_no_hay_estado_de_nacimiento(self) -> None:
        ent = construir_entidad(
            PERSONA_FZ1, {"correo": "email", "estado": "estado"},
            {"correo": "a@b.com", "estado": "Jalisco"},
        )
        assert ent["campos"]["normalizados"]["normalized_estado"] is None
        assert ent["campos"]["direccion"]["estado"] == "Jalisco"


class TestIdentidad:
    def test_entidad_id_vacio_falla(self) -> None:
        from normalizacion.entidades.modelo import AnclaTipo, calcular_entidad_id

        with pytest.raises(ValueError):
            calcular_entidad_id("persona", AnclaTipo.CURP, "  ")


class TestProyeccionDinamica:
    """La MISMA persona canónica produce DISTINTAS estructuras según la receta."""

    CANON = {
        "nombre": {"nombre1": "Valeria", "nombre2": "", "apellido1": "Mendoza",
                   "apellido2": "Rios"},
        "nombre_completo": "Valeria Mendoza Rios", "alias": "vale", "curp": "CV",
        "rfc": "RV", "sexo": "M", "edad": "30", "email": "v@example.com",
        "telefono": "5522334455", "relacion": "titular",
        "direccion": {"municipio": "Cuauhtemoc", "estado": "CDMX"},
        "normalizados": {"normalized_dob": "1996-03-14", "normalized_estado": "CDMX"},
    }

    def test_otro_sistema_otra_estructura_y_valores(self) -> None:
        from normalizacion.entidades.proyeccion import (
            RECETA_SISTEMA_PLANO, aplicar_proyeccion,
        )

        out = aplicar_proyeccion(self.CANON, RECETA_SISTEMA_PLANO["definicion"])
        assert out["full_name"] == "Valeria Mendoza Rios"
        assert out["national_id"] == "CV"
        assert out["gender"] == "female"  # mapa H/M → male/female
        assert out["birth_date"] == "1996-03-14"  # de normalizados.normalized_dob
        assert out["contact"]["email"] == "v@example.com"  # anidación distinta
        assert out["birth_state"] == "CDMX"
        assert out["source"] == "azazel"  # constante
        assert "curp" not in out and "normalizados" not in out  # otra forma

    def test_paths_anidados(self) -> None:
        from normalizacion.entidades.proyeccion import get_path, set_path

        d: dict = {}
        set_path(d, "a.b.c", 7)
        assert get_path(d, "a.b.c") == 7 and get_path(d, "a.x") is None

    def test_set_path_colision_escalar_lanza(self) -> None:
        from normalizacion.entidades.proyeccion import set_path

        d: dict = {}
        set_path(d, "contact", "x")  # escalar
        with pytest.raises(ValueError):
            set_path(d, "contact.email", "y")  # colisión

    def test_exporta_coleccion_archivo_completo(self) -> None:
        from normalizacion.entidades.proyeccion import (
            RECETA_FZ1_BUNDLE, exportar_coleccion,
        )

        otra = dict(self.CANON, nombre_completo="Roberto Mendoza", curp="CR",
                    nombre={"nombre1": "Roberto", "apellido1": "Mendoza"})
        archivo = exportar_coleccion([self.CANON, otra], RECETA_FZ1_BUNDLE["definicion"])
        assert "_metadata" in archivo and "_mapeo_normalizacion_sistema" in archivo  # sobre
        assert len(archivo["personas"]) == 2  # arreglo
        p0 = archivo["personas"][0]
        assert p0["nombre"]["nombre1"] == "Valeria"  # persona anidada
        assert p0["figura"] == "cube"  # constante
        assert "es_objetivo" not in p0  # placeholder engañoso eliminado (llega en E5)

    def test_receta_por_item_en_lote_da_arreglo_plano(self) -> None:
        from normalizacion.entidades.proyeccion import RECETA_SISTEMA_PLANO, exportar_coleccion

        arr = exportar_coleccion([self.CANON], RECETA_SISTEMA_PLANO["definicion"])
        assert isinstance(arr, list) and arr[0]["full_name"] == "Valeria Mendoza Rios"

    def test_rechaza_datos_pegados_como_receta(self) -> None:
        from normalizacion.entidades.proyeccion import validar_definicion

        # el ARCHIVO de salida (con 'personas'/'_metadata') NO es una receta
        with pytest.raises(ValueError, match="DATOS"):
            validar_definicion({"_metadata": {}, "personas": [{"curp": "X"}]})

    def test_valida_receta_coleccion(self) -> None:
        from normalizacion.entidades.proyeccion import RECETA_FZ1_BUNDLE, validar_definicion

        validar_definicion(RECETA_FZ1_BUNDLE["definicion"])  # válida, no lanza
        with pytest.raises(ValueError):  # coleccion sin item
            validar_definicion({"coleccion": "personas", "sobre": {}})
        with pytest.raises(ValueError, match="ya existe en 'sobre'"):  # colisión clave/sobre
            validar_definicion({"sobre": {"personas": {"x": 1}}, "coleccion": "personas",
                                "item": {"passthrough": True}})

    def test_validacion_rechaza_definiciones_rotas(self) -> None:
        from normalizacion.entidades.proyeccion import validar_definicion

        malas = [
            {"salida": []},  # vacía
            {"salida": [{"de": "x"}]},  # sin path
            {"salida": [{"path": "p", "constante": "H", "mapa": {"H": "M"}}]},  # mapa+constante
            {"salida": [{"path": "p", "de": "x", "constante": "y"}]},  # ambos
            {"salida": [{"path": "a..b", "de": "x"}]},  # ruta inválida
            {"salida": [{"path": "contact", "de": "a"}, {"path": "contact.email", "de": "b"}]},  # colisión
        ]
        for d in malas:
            with pytest.raises(ValueError):
                validar_definicion(d)
        validar_definicion({"passthrough": True})  # válida, no lanza


class TestBackfillExtraccion:
    """Detección de persona por CURP/RFC en un documento indexado (lógica pura)."""

    RFC_OK = "MERV960314AB1"  # física 13, fecha válida (homoclave no se verifica aún)

    def test_curp_valida_en_texto(self) -> None:
        from normalizacion.entidades.backfill import personas_de_doc

        doc = {"archivo_id": "a1", "ruta_original": "/x.pdf", "disco_id": "d1",
               "texto_indexable": f"Titular CURP {CURP_VALERIA} domicilio conocido"}
        filas, proc = personas_de_doc(doc)
        assert filas == [{"curp": CURP_VALERIA}]
        assert proc["archivo_id"] == "a1" and proc["fuente"] == "backfill_indice"

    def test_curp_invalida_se_ignora(self) -> None:
        from normalizacion.entidades.backfill import personas_de_doc

        mala = CURP_VALERIA[:-1] + ("9" if CURP_VALERIA[-1] != "9" else "8")  # rompe el dígito
        filas, _ = personas_de_doc({"texto_indexable": f"ruido {mala} ruido"})
        assert filas == []

    def test_una_curp_un_rfc_misma_persona(self) -> None:
        from normalizacion.entidades.backfill import personas_de_doc

        doc = {"texto_indexable": f"{CURP_VALERIA} y su RFC {self.RFC_OK}."}
        filas, _ = personas_de_doc(doc)
        assert filas == [{"curp": CURP_VALERIA, "rfc": self.RFC_OK}]

    def test_varias_curps_no_mezclan_rfc(self) -> None:
        from normalizacion.entidades.backfill import personas_de_doc

        otra = _curp("FUCD940519HDFNNG0")
        doc = {"texto_indexable": f"{CURP_VALERIA} ... {otra} ... RFC {self.RFC_OK}"}
        filas, _ = personas_de_doc(doc)
        assert {f["curp"] for f in filas} == {CURP_VALERIA, otra}
        assert all("rfc" not in f for f in filas)  # ambiguo: no se asigna el RFC

    def test_solo_rfc_sin_curp(self) -> None:
        from normalizacion.entidades.backfill import personas_de_doc

        filas, _ = personas_de_doc({"texto_indexable": f"Contribuyente {self.RFC_OK}"})
        assert filas == [{"rfc": self.RFC_OK}]

    def test_sin_ancla_no_es_persona(self) -> None:
        from normalizacion.entidades.backfill import personas_de_doc

        filas, _ = personas_de_doc({"texto_indexable": "factura de luz, sin identificadores"})
        assert filas == []


class TestRecetasCRUD:
    def test_seed_lista_recetas(self, config: Config) -> None:
        from normalizacion.entidades.recetas_db import listar_recetas

        claves = {r["clave"] for r in listar_recetas(config)}
        assert claves == {"fz1_bundle", "sistema_plano"}  # arranca SOLO con estas dos

    def test_crear_editar_borrar(self, config: Config) -> None:
        from normalizacion.entidades.recetas_db import (
            borrar_receta, guardar_receta, leer_receta,
        )

        nueva = {"clave": "mi_sistema", "nombre": "Mío", "descripcion": "",
                 "definicion": {"salida": [{"path": "id", "de": "curp"}]},
                 "version": "v1", "tipo": "persona", "clase": "proyeccion"}
        guardar_receta(config, nueva)
        assert leer_receta(config, "mi_sistema")["nombre"] == "Mío"
        assert borrar_receta(config, "mi_sistema") is True
        assert leer_receta(config, "mi_sistema") is None

    def test_definicion_invalida_rechazada(self, config: Config) -> None:
        from normalizacion.entidades.recetas_db import guardar_receta

        with pytest.raises(ValueError):
            guardar_receta(config, {"clave": "mala", "nombre": "x",
                                    "definicion": {"foo": 1}})

    def test_receta_no_editable_no_se_sobrescribe(self, config: Config) -> None:
        from normalizacion.entidades.recetas_db import guardar_receta

        # una receta marcada no editable queda protegida contra sobrescritura
        guardar_receta(config, {"clave": "bloqueada", "nombre": "Base",
                                "definicion": {"passthrough": True}, "editable": False})
        with pytest.raises(ValueError):
            guardar_receta(config, {"clave": "bloqueada", "nombre": "hack",
                                    "definicion": {"passthrough": True}})

    def test_semillas_editables(self, config: Config) -> None:
        from normalizacion.entidades.recetas_db import guardar_receta, leer_receta, listar_recetas

        listar_recetas(config)  # siembra fz1_bundle + sistema_plano (ambas editables)
        guardar_receta(config, {"clave": "sistema_plano", "nombre": "Ej. mod",
                                "definicion": {"salida": [{"path": "curp", "de": "curp"}]}})
        assert leer_receta(config, "sistema_plano")["nombre"] == "Ej. mod"
