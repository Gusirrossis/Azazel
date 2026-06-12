"""Integración del filtro editable (config_overrides) y el explorador de cola.

Solo necesitan Postgres (los endpoints /filtro y /cola/* no tocan OpenSearch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from normalizacion.core import cola, config_overrides
from normalizacion.core.config import Config
from normalizacion.core.modelo import Estado, RutaDecision

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str, conexion: Any) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


@pytest.fixture()
def cliente_api(config: Config) -> Any:
    from fastapi.testclient import TestClient

    from normalizacion.api.main import crear_app

    return TestClient(crear_app(config))


def _sembrar(conexion: Any, nombres: list[str], disco_id: str = "d1") -> None:
    cola.upsert_disco(conexion, disco_id, "/mnt/d1")
    filas = [
        cola.FilaCatalogo(
            archivo_id=f"id-{i:05d}",
            disco_id=disco_id,
            ruta=n,
            nombre=n,
            extension=f".{n.rsplit('.', 1)[1]}" if "." in n else None,
            tamano=100,
            mtime=datetime(2023, 1, 1, tzinfo=UTC),
        )
        for i, n in enumerate(nombres)
    ]
    cola.insertar_pendientes(conexion, filas)
    conexion.commit()


class TestOverridesNucleo:
    def test_roundtrip_guardar_leer_borrar(self, conexion: Any) -> None:
        assert config_overrides.leer_overrides(conexion) == {}
        config_overrides.guardar_overrides(conexion, {"umbral_hot": 80})
        assert config_overrides.leer_overrides(conexion) == {"umbral_hot": 80}
        config_overrides.guardar_overrides(conexion, {"umbral_hot": 70, "modo_lista": "negra"})
        assert config_overrides.leer_overrides(conexion)["umbral_hot"] == 70
        config_overrides.borrar_overrides(conexion)
        assert config_overrides.leer_overrides(conexion) == {}

    def test_campo_no_editable_rechazado(self, conexion: Any) -> None:
        with pytest.raises(ValueError, match="no editables"):
            config_overrides.guardar_overrides(conexion, {"t3_profundidad_max": 1})

    def test_filtro_efectivo_mergea_y_revalida(self, config: Config) -> None:
        filtro = config_overrides.filtro_efectivo(config, {"umbral_hot": 80})
        assert filtro.umbral_hot == 80
        assert filtro.umbral_cold == config.filtro.umbral_cold  # lo no tocado, intacto
        with pytest.raises(ValueError):
            config_overrides.filtro_efectivo(config, {"umbral_hot": 999})

    def test_aplicar_sin_overrides_devuelve_config_intacta(self, config: Config) -> None:
        assert config_overrides.aplicar_overrides(config).filtro == config.filtro

    def test_aplicar_overrides_llega_a_la_config_de_corrida(
        self, config: Config, conexion: Any
    ) -> None:
        config_overrides.guardar_overrides(conexion, {"umbral_hot": 77})
        conexion.commit()
        assert config_overrides.aplicar_overrides(config).filtro.umbral_hot == 77

    def test_version_derivada_estable_y_auditada(self) -> None:
        v1 = config_overrides.derivar_version("reglas-v3", {"umbral_hot": 80})
        v2 = config_overrides.derivar_version("reglas-v3", {"umbral_hot": 80})
        v3 = config_overrides.derivar_version("reglas-v3", {"umbral_hot": 81})
        assert v1 == v2 != v3
        assert v1.startswith("reglas-v3+ov-")
        # re-derivar sobre una versión ya derivada no apila sufijos
        assert config_overrides.derivar_version(v1, {"umbral_hot": 80}) == v1


class TestApiFiltro:
    def test_get_defaults_sin_overrides(self, cliente_api: Any) -> None:
        r = cliente_api.get("/filtro")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["hay_overrides"] is False
        assert cuerpo["efectivo"]["umbral_hot"] == 65
        assert cuerpo["efectivo"]["entropia_texto_max"] == 3.5
        assert cuerpo["efectivo"]["prioridad_extensiones"][".txt"] == 140

    def test_put_mergea_y_deriva_version(self, cliente_api: Any) -> None:
        r = cliente_api.put("/filtro", json={"umbral_hot": 80})
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["hay_overrides"] is True
        assert cuerpo["efectivo"]["umbral_hot"] == 80
        assert "+ov-" in cuerpo["efectivo"]["version_filtro"]
        # segundo PUT acumula sin perder el primero
        r2 = cliente_api.put("/filtro", json={"tipos_excluidos": ["text/html", "text/css"]})
        assert r2.json()["efectivo"]["umbral_hot"] == 80
        assert sorted(r2.json()["efectivo"]["tipos_excluidos"]) == ["text/css", "text/html"]

    def test_put_campo_desconocido_es_422(self, cliente_api: Any) -> None:
        assert cliente_api.put("/filtro", json={"no_existe": 1}).status_code == 422

    def test_put_valor_fuera_de_rango_es_422(self, cliente_api: Any) -> None:
        assert cliente_api.put("/filtro", json={"umbral_hot": 999}).status_code == 422

    def test_put_vacio_es_400(self, cliente_api: Any) -> None:
        assert cliente_api.put("/filtro", json={}).status_code == 400

    def test_delete_restablece(self, cliente_api: Any) -> None:
        cliente_api.put("/filtro", json={"umbral_hot": 80})
        r = cliente_api.delete("/filtro")
        assert r.status_code == 200
        assert r.json()["hay_overrides"] is False
        assert r.json()["efectivo"]["umbral_hot"] == 65


class TestApiColaArchivos:
    def test_filtro_por_estado_y_total(self, cliente_api: Any, conexion: Any) -> None:
        _sembrar(conexion, ["a.txt", "b.csv", "c.pdf"])
        cola.marcar_error(conexion, "id-00000", Estado.PENDIENTE, "prueba:manual")
        conexion.commit()
        r = cliente_api.get("/cola/archivos", params={"estado": "ERROR"})
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["total"] == 1
        assert cuerpo["archivos"][0]["nombre"] == "a.txt"
        assert cuerpo["archivos"][0]["error_motivo"] == "prueba:manual"
        assert cuerpo["archivos"][0]["intentos"] == 1

    def test_estado_desconocido_es_400(self, cliente_api: Any) -> None:
        assert cliente_api.get("/cola/archivos", params={"estado": "NADA"}).status_code == 400

    def test_keyset_dos_paginas_sin_solape(self, cliente_api: Any, conexion: Any) -> None:
        _sembrar(conexion, [f"f{i}.txt" for i in range(7)])
        p1 = cliente_api.get("/cola/archivos", params={"limite": 4}).json()
        assert len(p1["archivos"]) == 4 and p1["cursor"] is not None
        p2 = cliente_api.get(
            "/cola/archivos", params={"limite": 4, "cursor": p1["cursor"]}
        ).json()
        assert len(p2["archivos"]) == 3 and p2["cursor"] is None
        ids1 = {a["archivo_id"] for a in p1["archivos"]}
        ids2 = {a["archivo_id"] for a in p2["archivos"]}
        assert not ids1 & ids2
        assert p1["total"] == p2["total"] == 7

    def test_senales_visibles_en_la_fila(self, cliente_api: Any, conexion: Any) -> None:
        """La vista existe para auditar el filtro: la entropía debe llegar al front."""
        _sembrar(conexion, ["a.txt"])
        cola.claim(conexion, worker_id="w", estado=Estado.PENDIENTE, lote=1, lease_segundos=60)
        cola.guardar_precalificacion(
            conexion,
            "id-00000",
            puntaje=20,
            ruta=RutaDecision.COLD,
            tipo_real="application/zip",
            senales={"entropia": 7.9, "tier": "T2"},
            motivo="comprimido",
            version_filtro="test",
        )
        conexion.commit()
        r = cliente_api.get("/cola/archivos", params={"estado": "COLD"}).json()
        assert r["archivos"][0]["senales"]["entropia"] == 7.9
        assert r["archivos"][0]["ruta_decision"] == "COLD"

    def test_resumen_por_causa_y_tipo(self, cliente_api: Any, conexion: Any) -> None:
        """El resumen agrupa por el prefijo del motivo (error_motivo manda en ERROR)."""
        _sembrar(conexion, ["a.txt", "b.txt", "c.zip"])
        cola.marcar_error(conexion, "id-00000", Estado.PENDIENTE, "agotado:almacen caido")
        cola.marcar_error(conexion, "id-00001", Estado.PENDIENTE, "agotado:indice caido")
        conexion.commit()
        r = cliente_api.get("/cola/archivos", params={"estado": "ERROR"}).json()
        causas = {g["clave"]: g["archivos"] for g in r["resumen"]["por_causa"]}
        assert causas == {"agotado": 2}
        tipos = {g["clave"]: g["archivos"] for g in r["resumen"]["por_tipo"]}
        assert tipos == {"sin_tipificar": 2}

    def test_filtro_franja_gris_por_puntaje(self, cliente_api: Any, conexion: Any) -> None:
        _sembrar(conexion, ["a.txt", "b.txt", "c.txt"])
        for archivo_id, puntaje in (("id-00000", 20), ("id-00001", 50), ("id-00002", 80)):
            cola.claim(
                conexion, worker_id="w", estado=Estado.PENDIENTE, lote=1, lease_segundos=60
            )
            cola.guardar_precalificacion(
                conexion,
                archivo_id,
                puntaje=puntaje,
                ruta=RutaDecision.HOT,
                tipo_real="text/plain",
                senales={},
                motivo="x",
                version_filtro="t",
            )
        conexion.commit()
        r = cliente_api.get(
            "/cola/archivos", params={"puntaje_min": 35, "puntaje_max": 64}
        ).json()
        assert r["total"] == 1
        assert r["archivos"][0]["puntaje"] == 50

    def test_reprocesar_errores(self, cliente_api: Any, conexion: Any) -> None:
        _sembrar(conexion, ["a.txt", "b.txt"])
        cola.marcar_error(conexion, "id-00000", Estado.PENDIENTE, "agotado:almacen")
        cola.marcar_error(conexion, "id-00001", Estado.PENDIENTE, "io_ilegible: x")
        conexion.commit()
        r = cliente_api.post("/cola/reprocesar-errores", json={"motivo_como": "agotado:%"})
        assert r.status_code == 200
        assert r.json() == {"total": 1, "destinos": {"PENDIENTE": 1}}
        restante = cliente_api.get("/cola/archivos", params={"estado": "ERROR"}).json()
        assert restante["total"] == 1

    def test_rescore_frio(self, cliente_api: Any, conexion: Any) -> None:
        _sembrar(conexion, ["a.zip"])
        cola.claim(conexion, worker_id="w", estado=Estado.PENDIENTE, lote=1, lease_segundos=60)
        cola.guardar_precalificacion(
            conexion,
            "id-00000",
            puntaje=10,
            ruta=RutaDecision.COLD,
            tipo_real=None,
            senales={},
            motivo="fuera_de_lista",
            version_filtro="test",
        )
        conexion.commit()
        r = cliente_api.post("/cola/rescore-frio")
        assert r.status_code == 200
        assert r.json() == {"re_encolados": 1}
        pendiente = cliente_api.get("/cola/archivos", params={"estado": "PENDIENTE"}).json()
        assert pendiente["total"] == 1
