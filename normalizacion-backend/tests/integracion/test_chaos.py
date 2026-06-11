"""CHAOS (DoD Fase 3 → M4): se tumba cada dependencia a media corrida y el sistema
se recupera sin perder ni duplicar NADA al restaurarla.

Las caídas se inyectan con dobles del almacén/sink (misma interfaz); Postgres caído
se cubre por diseño (system-of-record: el proceso muere y reanuda por leases —
ver TestKillAMitad, que es el mismo mecanismo).
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Any

import pytest

from normalizacion.core import cola
from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.config import Config, PerillasWorker
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
from normalizacion.ingesta.workers.orquestador import procesar_hot
from normalizacion.ingesta.workers.verificador import (
    evaluar_puerta,
    mover_frio,
    verificar_indexados,
)

pytestmark = pytest.mark.integracion


class KillSimulado(BaseException):
    """Muerte abrupta del proceso (no la atrapa ningún except Exception)."""


class AlmacenCaido:
    """MinIO caído: toda operación falla con error de conexión."""

    def existe(self, hash_contenido: str) -> bool:
        raise ConnectionError("simulado: almacén caído")

    def guardar(self, hash_contenido: str, fuente: IO[bytes], tamano: int) -> None:
        raise ConnectionError("simulado: almacén caído")

    def leer(self, hash_contenido: str) -> IO[bytes]:
        raise ConnectionError("simulado: almacén caído")


class AlmacenQueMata:
    """Sano hasta el archivo N; ahí el proceso 'muere' (una sola vez)."""

    def __init__(self, real: AlmacenLocal, morir_en: int) -> None:
        self._real = real
        self._restantes = morir_en
        self.ya_mato = False

    def existe(self, h: str) -> bool:
        return self._real.existe(h)

    def guardar(self, h: str, fuente: IO[bytes], tamano: int) -> None:
        if not self.ya_mato:
            self._restantes -= 1
            if self._restantes <= 0:
                self.ya_mato = True
                raise KillSimulado()
        self._real.guardar(h, fuente, tamano)

    def leer(self, h: str) -> IO[bytes]:
        return self._real.leer(h)


def _config(dsn: str, **worker: Any) -> Config:
    base: dict[str, Any] = {"backoff_transitorio_base_s": 30.0}  # backoff visible
    base.update(worker)
    return Config(
        _env_file=None,
        postgres_dsn=dsn,
        almacen_backend="local",
        worker=PerillasWorker(**base),
    )


def _vencer_leases(conexion: Any) -> None:
    """Simula el paso del tiempo: todos los backoffs/leases vencen ya."""
    conexion.execute(
        "UPDATE archivos SET lease_hasta = clock_timestamp() - interval '1 second'"
        " WHERE lease_hasta IS NOT NULL"
    )
    conexion.commit()


@pytest.fixture()
def preparado(dsn: str, conexion: Any, disco_sintetico: Path) -> Any:
    config = _config(dsn)
    catalogar_disco(config, disco_sintetico, disco_id="disco-test")
    precalificar_pendientes(config)
    return conexion


class TestAlmacenCaido:
    def test_caida_total_no_pierde_ni_un_archivo(
        self, dsn: str, preparado: Any, tmp_path: Path
    ) -> None:
        """MinIO caído durante TODA la corrida: 0 procesados, 0 dead-letter,
        las 15 filas quedan en backoff esperando que vuelva."""
        config = _config(dsn)
        resumen = procesar_hot(config, almacen=AlmacenCaido())  # type: ignore[arg-type]
        assert resumen.procesados == 0
        assert resumen.transitorios == 15
        assert resumen.errores == 0  # NADA fue a dead-letter: la culpa era del almacén

        estados = dict(
            preparado.execute("SELECT estado, COUNT(*) FROM archivos GROUP BY estado").fetchall()
        )
        assert estados.get("ERROR") is None
        assert estados["PRECALIFICADO"] == 15  # devueltas, con intentos+1

    def test_al_restaurar_se_completa_identico(
        self, dsn: str, preparado: Any, tmp_path: Path
    ) -> None:
        """Caída → restauración → el resultado final es el de una corrida limpia."""
        config = _config(dsn)
        procesar_hot(config, almacen=AlmacenCaido())  # type: ignore[arg-type]
        _vencer_leases(preparado)  # "pasó" el backoff

        sano = AlmacenLocal(tmp_path / "almacen")
        resumen = procesar_hot(config, almacen=sano)
        assert resumen.procesados == 15
        assert resumen.errores == 0
        indexados = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'INDEXADO'"
        ).fetchone()[0]
        assert indexados == 15

    def test_backoff_real_no_reclama_de_inmediato(
        self, dsn: str, preparado: Any, tmp_path: Path
    ) -> None:
        """Tras el fallo, la fila NO es reclamable hasta vencer el backoff (lease)."""
        config = _config(dsn)
        procesar_hot(config, almacen=AlmacenCaido())  # type: ignore[arg-type]
        # SIN vencer leases: un worker sano no encuentra nada que reclamar
        resumen = procesar_hot(config, almacen=AlmacenLocal(tmp_path / "a"))
        assert resumen.procesados == 0


class ClienteOpenSearchFalso:
    """Simula OpenSearch: opcionalmente caído (toda llamada bulk falla)."""

    def __init__(self, caido: bool = False) -> None:
        self._caido = caido

    def bulk(self, body: str) -> dict[str, Any]:
        if self._caido:
            raise ConnectionError("simulado: OpenSearch caído")
        import json

        items = [
            {"index": {"_id": json.loads(linea)["index"]["_id"], "status": 201}}
            for linea in body.splitlines()
            if '"index"' in linea and "_id" in linea
        ]
        return {"errors": False, "items": items}


class TestOpenSearchCaido:
    def test_indice_caido_devuelve_filas_no_dead_letter(
        self, dsn: str, preparado: Any, tmp_path: Path
    ) -> None:
        """OpenSearch caído: blobs persistidos, pero filas devueltas con backoff
        (el doc NO está confirmado en el índice → no pueden ser INDEXADO)."""
        from normalizacion.core.config import PerillasIndexador
        from normalizacion.core.indexador.opensearch import SinkOpenSearch

        config = _config(dsn)
        config_sink = Config(
            _env_file=None,
            postgres_dsn=dsn,
            indexador=PerillasIndexador(backoff_base_s=0.0, reintentos_max=1),
        )
        sink_roto = SinkOpenSearch(config_sink, cliente=ClienteOpenSearchFalso(caido=True))

        almacen = AlmacenLocal(tmp_path / "almacen")
        resumen = procesar_hot(config, sink=sink_roto, almacen=almacen)
        assert resumen.procesados == 0
        assert resumen.transitorios == 15
        assert resumen.errores == 0

        _vencer_leases(preparado)
        sink_sano = SinkOpenSearch(config_sink, cliente=ClienteOpenSearchFalso())
        resumen2 = procesar_hot(config, sink=sink_sano, almacen=almacen)
        assert resumen2.procesados == 15
        # Los blobs del primer intento se DEDUPLICARON (no se re-copiaron)
        assert resumen2.deduplicados == 15


class TestDiscoIlegible:
    def test_archivo_desaparecido_agota_y_va_a_dead_letter(
        self, dsn: str, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """Disco origen ilegible para UN archivo: reintenta con tope, ERROR clasificado,
        y el RESTO del disco termina completo."""
        import shutil

        copia = tmp_path / "disco"
        shutil.copytree(disco_sintetico, copia)
        config = _config(dsn, backoff_transitorio_base_s=0.0, intentos_max=2)
        catalogar_disco(config, copia, disco_id="disco-test")
        precalificar_pendientes(config)
        (copia / "datos" / "ventas_2023.csv").unlink()  # el disco "se daña"

        almacen = AlmacenLocal(tmp_path / "almacen")
        resumen = procesar_hot(config, almacen=almacen)
        assert resumen.procesados == 14
        assert resumen.errores == 1  # agotado → dead-letter

        fila = conexion.execute(
            "SELECT estado, error_motivo FROM archivos WHERE nombre = 'ventas_2023.csv'"
        ).fetchone()
        assert fila[0] == "ERROR"
        assert fila[1].startswith("agotado:")
        # Y la puerta queda BLOQUEADA por ese único archivo
        verificar_indexados(config, almacen=almacen)
        mover_frio(config, almacen_frio=AlmacenLocal(tmp_path / "frio"))
        assert evaluar_puerta(config, "disco-test").seguro_para_desechar is False


class TestKillAMitad:
    def test_matar_y_reanudar_es_identico_a_corrida_limpia(
        self, dsn: str, preparado: Any, tmp_path: Path
    ) -> None:
        """DoD Fase 3: kill -9 a mitad del disco → reanudar → estado final idéntico."""
        config = _config(dsn)
        real = AlmacenLocal(tmp_path / "almacen")
        asesino = AlmacenQueMata(real, morir_en=6)

        with pytest.raises(KillSimulado):
            procesar_hot(config, almacen=asesino)  # type: ignore[arg-type]
        assert asesino.ya_mato

        # El proceso murió: quedan huérfanos EN_PROCESO con lease. Pasa el tiempo…
        _vencer_leases(preparado)

        # Worker nuevo: rescata huérfanos y termina TODO
        resumen = procesar_hot(config, worker_id="w-relevo", almacen=real)
        assert resumen.errores == 0

        finales = dict(
            preparado.execute(
                "SELECT estado, COUNT(*) FROM archivos GROUP BY estado ORDER BY estado"
            ).fetchall()
        )
        assert finales == {"INDEXADO": 15, "COLD": 8}  # idéntico a una corrida limpia
        con_hash = preparado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'INDEXADO' AND hash_contenido IS NOT NULL"
        ).fetchone()[0]
        assert con_hash == 15
        # Sin blobs duplicados ni faltantes (14 únicos: 15 HOT - 1 dedup)
        blobs = len([p for p in (tmp_path / "almacen").rglob("*") if p.is_file()])
        assert blobs == 14


class TestHeartbeat:
    def test_renovar_lease_extiende_el_plazo(self, dsn: str, preparado: Any) -> None:
        """Un worker vivo procesando algo enorme no pierde su trabajo por lease."""
        import psycopg

        with psycopg.connect(dsn) as conn:
            from normalizacion.core.modelo import Estado

            cola.claim(
                conn,
                worker_id="w-lento",
                estado=Estado.PRECALIFICADO,
                lote=5,
                lease_segundos=10,
            )
            antes = conn.execute(
                "SELECT MIN(lease_hasta) FROM archivos WHERE worker_id = 'w-lento'"
            ).fetchone()[0]
            renovadas = cola.renovar_lease(conn, "w-lento", 600)
            despues = conn.execute(
                "SELECT MIN(lease_hasta) FROM archivos WHERE worker_id = 'w-lento'"
            ).fetchone()[0]
        assert renovadas == 5
        assert despues > antes


class TestProcesoDeLote:
    def test_io_intermitente_en_precalificacion_reintenta(
        self, dsn: str, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """Archivo que falla en la precalificación → backoff (PENDIENTE), no ERROR."""
        import shutil

        copia = tmp_path / "disco"
        shutil.copytree(disco_sintetico, copia)
        config = _config(dsn)
        catalogar_disco(config, copia, disco_id="disco-test")
        (copia / "datos" / "notas.txt").unlink()

        resumen = precalificar_pendientes(config)
        assert resumen.transitorios == 1
        fila = conexion.execute(
            "SELECT estado, intentos FROM archivos WHERE nombre = 'notas.txt'"
        ).fetchone()
        assert fila == ("PENDIENTE", 1)  # sigue en la cola, con backoff
