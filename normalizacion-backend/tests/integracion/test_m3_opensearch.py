"""M3 ⭐ end-to-end con OpenSearch REAL: el hito crítico del plan.

Un disco entra → catálogo → precalificación (T0-T4) → worker (blobs + índice) →
mover frío → verificación → puerta VERDE → y la búsqueda por nombre FUNCIONA.

Requiere OpenSearch vivo (NORM_OPENSEARCH_URL); si no está, se omite con motivo.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.config import Config
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
from normalizacion.ingesta.workers.orquestador import procesar_hot
from normalizacion.ingesta.workers.verificador import (
    evaluar_puerta,
    mover_frio,
    verificar_indexados,
)

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
def config(dsn: str, opensearch_url: str) -> Config:
    return Config(
        _env_file=None,
        postgres_dsn=dsn,
        opensearch_url=opensearch_url,
        almacen_backend="local",
        indice_alias="archivos-test",
    )


@pytest.fixture()
def indice_limpio(config: Config) -> Any:
    from normalizacion.core.indexador.opensearch import aplicar_indice, crear_cliente

    cliente = crear_cliente(config)
    cliente.indices.delete(index=f"{config.indice_alias}*", ignore=[404])
    raiz = Path(__file__).resolve().parents[2]
    aplicar_indice(config, raiz / "deploy")
    return cliente


class TestM3EndToEnd:
    def test_el_hito_critico(
        self,
        config: Config,
        conexion: Any,
        indice_limpio: Any,
        disco_sintetico: Path,
        tmp_path: Path,
    ) -> None:
        """M3: disco → a salvo → buscable → verificado → SEGURO PARA DESECHAR."""
        from normalizacion.core.indexador.opensearch import SinkOpenSearch, buscar_por_nombre

        # 1) Catálogo + precalificación
        catalogar_disco(config, disco_sintetico, disco_id="disco-m3")
        precalificar_pendientes(config)

        # 2) Worker: blobs al almacén + docs al índice REAL
        almacen = AlmacenLocal(tmp_path / "almacen")
        resumen = procesar_hot(config, sink=SinkOpenSearch(config), almacen=almacen)
        assert resumen.procesados == 15
        assert resumen.errores == 0

        # 3) Búsqueda por nombre (la prioridad del negocio: nombre/tipo primero)
        indice_limpio.indices.refresh(index=config.indice_alias)
        time.sleep(0.5)
        hits = buscar_por_nombre(config, "ventas")
        assert any(h["nombre"] == "ventas_2023.csv" for h in hits)

        # La extensión mentirosa quedó indexada con su tipo REAL
        hits_jpg = buscar_por_nombre(config, "vacaciones")
        assert hits_jpg and hits_jpg[0]["tipo_real"] == "text/csv"

        # El CSV del fondo de las cajas anidadas es buscable
        hits_interno = buscar_por_nombre(config, "datos_internos")
        assert hits_interno and hits_interno[0]["hash_contenido"]

        # 4) Verificación + frío + puerta VERDE
        assert verificar_indexados(config, almacen=almacen).fallidos == 0
        assert mover_frio(config, almacen_frio=AlmacenLocal(tmp_path / "frio")).errores == 0
        estado = evaluar_puerta(config, "disco-m3")
        assert estado.seguro_para_desechar is True

        # 5) El índice contiene EXACTAMENTE los 15 HOT (COLD jamás se indexa — R5)
        total = indice_limpio.count(index=config.indice_alias)["count"]
        assert total == 15

    def test_reindexar_no_duplica(
        self,
        config: Config,
        conexion: Any,
        indice_limpio: Any,
        disco_sintetico: Path,
        tmp_path: Path,
    ) -> None:
        """_id = archivo_id: reprocesar sobrescribe en vez de duplicar (idempotencia)."""
        from normalizacion.core.indexador.opensearch import SinkOpenSearch

        catalogar_disco(config, disco_sintetico, disco_id="disco-m3")
        precalificar_pendientes(config)
        almacen = AlmacenLocal(tmp_path / "almacen")
        procesar_hot(config, sink=SinkOpenSearch(config), almacen=almacen)

        # Forzar re-proceso: regresar 3 filas a PRECALIFICADO y volver a correr
        conexion.execute(
            "UPDATE archivos SET estado = 'PRECALIFICADO', hash_contenido = NULL"
            " WHERE archivo_id IN (SELECT archivo_id FROM archivos"
            "  WHERE estado = 'INDEXADO' LIMIT 3)"
        )
        conexion.commit()
        procesar_hot(config, sink=SinkOpenSearch(config), almacen=almacen)

        indice_limpio.indices.refresh(index=config.indice_alias)
        total = indice_limpio.count(index=config.indice_alias)["count"]
        assert total == 15  # ni un duplicado
