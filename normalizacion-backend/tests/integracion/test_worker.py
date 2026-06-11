"""Integración Iteración 4 (DoD): lectura única, dedup real y reanudación del worker.

Pipeline completo sobre el disco sintético: catálogo → precalificación → worker.
El almacén es el backend local (misma interfaz que MinIO) en un tmp_path.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from normalizacion.core import cola
from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.config import Config
from normalizacion.core.indexador import SinkNulo
from normalizacion.core.modelo import Estado
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
from normalizacion.ingesta.workers.orquestador import procesar_hot

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn, almacen_backend="local")


@pytest.fixture()
def preparado(config: Config, conexion: Any, disco_sintetico: Path) -> Any:
    """Disco catalogado y precalificado: 15 filas HOT esperando al worker."""
    catalogar_disco(config, disco_sintetico, disco_id="disco-test")
    precalificar_pendientes(config)
    return conexion


class TestWorkerHot:
    def test_flujo_completo_hot(self, config: Config, preparado: Any, tmp_path: Path) -> None:
        """DoD: las 15 HOT quedan INDEXADO con hash; los COLD no se tocan."""
        almacen = AlmacenLocal(tmp_path / "almacen")
        sink = SinkNulo()
        resumen = procesar_hot(config, sink=sink, almacen=almacen)

        assert resumen.procesados == 15
        assert resumen.errores == 0
        assert len(sink.documentos) == 15

        indexados = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'INDEXADO' AND hash_contenido IS NOT NULL"
        ).fetchone()[0]
        assert indexados == 15
        cold_intactos = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'COLD' AND hash_contenido IS NULL"
        ).fetchone()[0]
        assert cold_intactos == 8

    def test_archivo_envenenado_no_tumba_la_corrida(
        self, config: Config, preparado: Any, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """CORRECCIÓN REAL: un archivo que revienta con una excepción inesperada
        (decode raro, lib de 7z/rar, bytes hostiles) va a dead-letter y el worker
        SIGUE con los demás. Jamás un solo archivo detiene el proceso automático."""
        from normalizacion.ingesta.workers import orquestador

        real_persistir = orquestador._persistir
        envenenado = {"n": 0}

        def persistir_con_veneno(*args: Any, **kwargs: Any) -> Any:
            envenenado["n"] += 1
            if envenenado["n"] == 3:  # el 3er archivo está "envenenado"
                raise ValueError("'utf-8' codec can't decode byte 0xed: bytes hostiles")
            return real_persistir(*args, **kwargs)

        monkeypatch.setattr(orquestador, "_persistir", persistir_con_veneno)
        resumen = procesar_hot(config, sink=SinkNulo(), almacen=AlmacenLocal(tmp_path / "almacen"))

        assert resumen.errores == 1  # SOLO el envenenado
        assert resumen.procesados == 14  # los otros 14 HOT, intactos
        envenenados = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'ERROR'"
            " AND error_motivo LIKE 'worker_fallido:%'"
        ).fetchone()[0]
        assert envenenados == 1
        # y es reprocesable: nada se perdió, está en la cola como ERROR

    def test_dedup_un_solo_blob_dos_procedencias(
        self, config: Config, preparado: Any, tmp_path: Path
    ) -> None:
        """DoD (R2): mismo contenido en dos rutas → UN blob, dos filas con el mismo hash."""
        almacen = AlmacenLocal(tmp_path / "almacen")
        resumen = procesar_hot(config, almacen=almacen)
        assert resumen.deduplicados == 1
        assert resumen.blobs_nuevos == 14  # 15 HOT - 1 duplicado

        hashes = preparado.execute(
            "SELECT hash_contenido FROM archivos WHERE nombre IN ('copia_a.csv', 'copia_b.csv')"
        ).fetchall()
        assert len(hashes) == 2
        assert hashes[0][0] == hashes[1][0]
        blobs_en_disco = len([p for p in (tmp_path / "almacen").rglob("*") if p.is_file()])
        assert blobs_en_disco == 14

    def test_integridad_round_trip(
        self, config: Config, preparado: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """El blob del almacén es BYTE A BYTE el original (la base de la puerta de It5)."""
        almacen = AlmacenLocal(tmp_path / "almacen")
        procesar_hot(config, almacen=almacen)

        original = (disco_sintetico / "datos" / "ventas_2023.csv").read_bytes()
        fila = preparado.execute(
            "SELECT hash_contenido FROM archivos WHERE nombre = 'ventas_2023.csv'"
        ).fetchone()
        with almacen.leer(fila[0]) as f:
            blob = f.read()
        assert blob == original
        assert hashlib.sha256(blob).hexdigest() == fila[0]

    def test_entrada_interna_tambien_se_persiste(
        self, config: Config, preparado: Any, tmp_path: Path
    ) -> None:
        """El CSV del fondo de las cajas (path spec) también queda a salvo como blob."""
        almacen = AlmacenLocal(tmp_path / "almacen")
        procesar_hot(config, almacen=almacen)
        fila = preparado.execute(
            "SELECT estado, hash_contenido FROM archivos WHERE nombre = 'datos_internos.csv'"
        ).fetchone()
        assert fila[0] == "INDEXADO"
        assert almacen.existe(fila[1])

    def test_re_ejecutar_es_noop(self, config: Config, preparado: Any, tmp_path: Path) -> None:
        almacen = AlmacenLocal(tmp_path / "almacen")
        procesar_hot(config, almacen=almacen)
        segunda = procesar_hot(config, almacen=almacen)
        assert segunda.procesados == 0


class TestReanudacion:
    def test_huerfano_de_worker_muerto_se_rescata(
        self, config: Config, preparado: Any, tmp_path: Path
    ) -> None:
        """DoD: matar el worker a media corrida no pierde NADA (lease + huérfanos)."""
        import psycopg

        # Simular un worker que murió: claim + EN_PROCESO con lease ya vencido
        with psycopg.connect(config.postgres_dsn) as conn:
            filas = cola.claim(
                conn,
                worker_id="w-muerto",
                estado=Estado.PRECALIFICADO,
                lote=3,
                lease_segundos=0,
            )
            for fila in filas:
                cola.transicionar(
                    conn,
                    fila.archivo_id,
                    Estado.PRECALIFICADO,
                    Estado.EN_PROCESO,
                    conservar_lease=True,
                )
            conn.commit()
        en_proceso = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'EN_PROCESO'"
        ).fetchone()[0]
        assert en_proceso == 3

        # Un worker nuevo arranca: rescata huérfanos y termina TODO el trabajo
        almacen = AlmacenLocal(tmp_path / "almacen")
        resumen = procesar_hot(config, worker_id="w-vivo", almacen=almacen)
        assert resumen.huerfanos_rescatados == 3
        assert resumen.procesados == 15
        atorados = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado IN ('EN_PROCESO', 'PRECALIFICADO')"
        ).fetchone()[0]
        assert atorados == 0
