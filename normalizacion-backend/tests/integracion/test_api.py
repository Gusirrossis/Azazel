"""Integración Fase 5 (DoD → M5): la API completa contra OpenSearch + almacén reales.

Requiere OpenSearch vivo; sin él, se omite con motivo (igual que test_m3).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.config import Config

pytestmark = pytest.mark.integracion


@pytest.fixture(scope="module")
def opensearch_url() -> str:
    url = os.environ.get("NORM_OPENSEARCH_URL", "http://localhost:9200")
    try:
        from opensearchpy import OpenSearch

        if not OpenSearch(hosts=[url], timeout=5).ping():
            pytest.skip(f"OpenSearch no responde en {url}")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"OpenSearch no disponible: {exc}")
    return url


@pytest.fixture()
def entorno(
    dsn: str, opensearch_url: str, conexion: Any, disco_sintetico: Path, tmp_path: Path
) -> Config:
    """Pipeline completo indexado en un alias de prueba + almacén local en tmp."""
    from normalizacion.core.indexador.opensearch import (
        SinkOpenSearch,
        aplicar_indice,
        crear_cliente,
    )
    from normalizacion.ingesta.catalogo.walker import catalogar_disco
    from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
    from normalizacion.ingesta.workers.orquestador import procesar_hot

    config = Config(
        _env_file=None,
        postgres_dsn=dsn,
        opensearch_url=opensearch_url,
        almacen_backend="local",
        almacen_local_raiz=str(tmp_path / "almacen"),
        indice_alias="archivos-api-test",
    )
    cliente = crear_cliente(config)
    cliente.indices.delete(index=f"{config.indice_alias}*", ignore=[404])
    aplicar_indice(config, Path(__file__).resolve().parents[2] / "deploy")

    catalogar_disco(config, disco_sintetico, disco_id="disco-api")
    precalificar_pendientes(config)
    procesar_hot(config, sink=SinkOpenSearch(config), almacen=AlmacenLocal(tmp_path / "almacen"))
    cliente.indices.refresh(index=config.indice_alias)
    return config


@pytest.fixture()
def cliente_api(entorno: Config) -> Any:
    from fastapi.testclient import TestClient

    from normalizacion.api.main import crear_app

    return TestClient(crear_app(entorno))


class TestCarpetaDestino:
    """UX: el front elige origen (ámbito `datos`) Y destino (ámbito `destino`),
    cada uno confinado a su propia raíz."""

    def _cliente_confinado(self, entorno: Config, datos: Path, destino: Path) -> Any:
        from fastapi.testclient import TestClient

        from normalizacion.api.main import crear_app

        cfg = entorno.model_copy(
            update={"api_carpeta_raiz": str(datos), "api_carpeta_destino_raiz": str(destino)}
        )
        return TestClient(crear_app(cfg))

    def test_ambitos_navegan_raices_distintas(self, entorno: Config, tmp_path: Path) -> None:
        datos, destino = tmp_path / "datos", tmp_path / "destino"
        (datos / "origen-a").mkdir(parents=True)
        (destino / "salida-b").mkdir(parents=True)
        cliente = self._cliente_confinado(entorno, datos, destino)

        r = cliente.get("/sistema/carpetas")
        assert r.status_code == 200 and r.json()["carpetas"] == ["origen-a"]
        r = cliente.get("/sistema/carpetas", params={"ambito": "destino"})
        assert r.status_code == 200 and r.json()["carpetas"] == ["salida-b"]

    def test_crear_carpeta_de_destino(self, entorno: Config, tmp_path: Path) -> None:
        datos, destino = tmp_path / "datos", tmp_path / "destino"
        datos.mkdir()
        destino.mkdir()
        cliente = self._cliente_confinado(entorno, datos, destino)

        r = cliente.post("/sistema/carpetas", json={"ruta": str(destino), "nombre": "junio"})
        assert r.status_code == 200
        assert (destino / "junio").is_dir()
        # confinada: no se puede crear fuera de la raíz de destino
        r = cliente.post("/sistema/carpetas", json={"ruta": str(datos), "nombre": "x"})
        assert r.status_code == 400

    def test_pipeline_rechaza_destino_fuera_de_raiz(self, entorno: Config, tmp_path: Path) -> None:
        datos, destino = tmp_path / "datos", tmp_path / "destino"
        (datos / "origen").mkdir(parents=True)
        destino.mkdir()
        cliente = self._cliente_confinado(entorno, datos, destino)

        r = cliente.post(
            "/pipeline/ejecutar",
            json={"ruta": str(datos / "origen"), "destino": str(tmp_path / "escape")},
        )
        assert r.status_code == 400
        assert "fuera de la carpeta permitida" in r.json()["detail"]

    def test_preservados_sin_explorar_visibles(self, cliente_api: Any) -> None:
        """La bomba del disco sintético aparece en el inventario de preservados."""
        r = cliente_api.get("/pipeline/preservados")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["total"] >= 1
        assert any(a["nombre"] == "bomba.zip" for a in cuerpo["archivos"])

    def test_estado_reporta_si_el_destino_es_eligible(
        self, entorno: Config, tmp_path: Path
    ) -> None:
        # nativo (sin confinamiento): eligible
        from fastapi.testclient import TestClient

        from normalizacion.api.main import crear_app

        r = TestClient(crear_app(entorno)).get("/pipeline/estado")
        assert r.status_code == 200 and r.json()["destino_eligible"] is True
        # Docker confinado SIN volumen de destino: NO eligible (sería efímero)
        cfg = entorno.model_copy(update={"api_carpeta_raiz": str(tmp_path)})
        r = TestClient(crear_app(cfg)).get("/pipeline/estado")
        assert r.status_code == 200 and r.json()["destino_eligible"] is False


class TestBusqueda:
    def test_busqueda_por_texto(self, cliente_api: Any) -> None:
        r = cliente_api.post("/buscar", json={"texto": "ventas"})
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["total"] >= 1
        assert any(d["nombre"] == "ventas_2023.csv" for d in cuerpo["documentos"])

    def test_filtro_por_tipo_real(self, cliente_api: Any) -> None:
        r = cliente_api.post("/buscar", json={"tipo_real": "text/csv"})
        docs = r.json()["documentos"]
        assert docs and all(d["tipo_real"] == "text/csv" for d in docs)
        # la extensión mentirosa aparece aquí: es un CSV aunque diga .jpg
        assert any(d["nombre"] == "vacaciones.jpg" for d in docs)

    def test_facetas(self, cliente_api: Any) -> None:
        r = cliente_api.post("/buscar", json={"facetas": True})
        facetas = r.json()["facetas"]
        assert facetas["por_tipo"]["text/csv"] >= 3
        assert "por_extension" in facetas and "por_disco" in facetas

    def test_paginacion_profunda_sin_huecos_ni_duplicados(self, cliente_api: Any) -> None:
        """DoD: search_after recorre TODO exactamente una vez."""
        vistos: list[str] = []
        cursor = None
        for _ in range(10):
            cuerpo: dict[str, Any] = {"tamano_pagina": 4}
            if cursor:
                cuerpo["cursor"] = cursor
            r = cliente_api.post("/buscar", json=cuerpo)
            docs = r.json()["documentos"]
            if not docs:
                break
            vistos.extend(d["archivo_id"] for d in docs)
            cursor = r.json()["cursor"]
        assert len(vistos) == 15
        assert len(set(vistos)) == 15

    def test_pit_da_vista_estable(self, cliente_api: Any) -> None:
        r = cliente_api.post("/buscar", json={"abrir_pit": True, "tamano_pagina": 5})
        cuerpo = r.json()
        assert cuerpo["pit_id"], "OpenSearch 2.x debe soportar PIT"
        r2 = cliente_api.post(
            "/buscar",
            json={"pit_id": cuerpo["pit_id"], "cursor": cuerpo["cursor"], "tamano_pagina": 5},
        )
        assert r2.status_code == 200
        assert len(r2.json()["documentos"]) == 5

    def test_busqueda_por_contenido_con_resaltado(self, cliente_api: Any) -> None:
        """Buscar una palabra que SOLO existe DENTRO de un archivo (no en su nombre)
        la encuentra y devuelve el fragmento resaltado — el caso 'nombre de persona'."""
        r = cliente_api.post("/buscar", json={"texto": "reanudar"})
        docs = r.json()["documentos"]
        assert any(d["nombre"] == "notas.txt" for d in docs)  # 'reanudar' está en el texto
        con_fragmento = next(d for d in docs if d["nombre"] == "notas.txt")
        assert any("⟪" in f for f in con_fragmento["_resaltado"])

    def test_autocompletar(self, cliente_api: Any) -> None:
        r = cliente_api.get("/autocompletar", params={"q": "vaca"})
        assert "vacaciones.jpg" in r.json()["sugerencias"]


class TestArchivo:
    def test_detalle_por_id(self, cliente_api: Any) -> None:
        archivo_id = cliente_api.post("/buscar", json={"texto": "ventas"}).json()["documentos"][0][
            "archivo_id"
        ]
        r = cliente_api.get(f"/archivo/{archivo_id}")
        assert r.status_code == 200
        assert r.json()["nombre"] == "ventas_2023.csv"

    def test_descarga_del_original_byte_a_byte(
        self, cliente_api: Any, disco_sintetico: Path
    ) -> None:
        """DoD: el original baja del ALMACÉN — el disco físico ya no existe."""
        archivo_id = cliente_api.post("/buscar", json={"texto": "ventas"}).json()["documentos"][0][
            "archivo_id"
        ]
        r = cliente_api.get(f"/archivo/{archivo_id}/contenido")
        assert r.status_code == 200
        original = (disco_sintetico / "datos" / "ventas_2023.csv").read_bytes()
        assert r.content == original
        assert "ventas_2023.csv" in r.headers["content-disposition"]

    def test_inexistente_es_404(self, cliente_api: Any) -> None:
        assert cliente_api.get("/archivo/no-existe").status_code == 404


class TestEstadisticas:
    def test_totales(self, cliente_api: Any) -> None:
        r = cliente_api.get("/estadisticas")
        cuerpo = r.json()
        assert cuerpo["total_documentos"] == 15
        assert cuerpo["bytes_totales"] > 0
        assert cuerpo["por_disco"]["disco-api"] == 15


class TestSeguridad:
    def test_sin_llave_es_401(self, entorno: Config) -> None:
        from fastapi.testclient import TestClient

        from normalizacion.api.main import crear_app

        protegida = entorno.model_copy(update={"api_keys": ("llave-correcta",)})
        cliente = TestClient(crear_app(protegida))
        assert cliente.get("/estadisticas").status_code == 401
        assert cliente.get("/estadisticas", headers={"X-API-Key": "llave-mala"}).status_code == 401
        assert (
            cliente.get("/estadisticas", headers={"X-API-Key": "llave-correcta"}).status_code == 200
        )

    def test_rate_limit_429(self, entorno: Config) -> None:
        from fastapi.testclient import TestClient

        from normalizacion.api.main import crear_app

        limitada = entorno.model_copy(update={"api_solicitudes_por_minuto": 3})
        cliente = TestClient(crear_app(limitada))
        for _ in range(3):
            assert cliente.get("/estadisticas").status_code == 200
        assert cliente.get("/estadisticas").status_code == 429

    def test_cors_para_el_front(self, cliente_api: Any) -> None:
        """El front (origen permitido) puede llamar a la API desde el navegador."""
        r = cliente_api.get("/estadisticas", headers={"Origin": "http://localhost:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_openapi_publicado(self, cliente_api: Any) -> None:
        """DoD: el contrato existe y tiene los 5 endpoints del plan."""
        spec = cliente_api.get("/openapi.json").json()
        for ruta in (
            "/buscar",
            "/autocompletar",
            "/archivo/{archivo_id}",
            "/archivo/{archivo_id}/contenido",
            "/estadisticas",
        ):
            assert ruta in spec["paths"], f"falta {ruta} en el contrato"
