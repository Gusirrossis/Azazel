"""Integración de la cola: claim atómico concurrente, leases y máquina de estados.

El test de concurrencia es DoD de la Fase 1: N trabajadores reclamando en paralelo
→ cero duplicados, cero items perdidos.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from normalizacion.core import cola
from normalizacion.core.modelo import Estado

pytestmark = pytest.mark.integracion


def _sembrar(conexion: Any, cuantos: int, disco_id: str = "d1") -> None:
    cola.upsert_disco(conexion, disco_id, "/mnt/d1")
    filas = [
        cola.FilaCatalogo(
            archivo_id=f"id-{i:05d}",
            disco_id=disco_id,
            ruta=f"carpeta/archivo_{i}.csv",
            nombre=f"archivo_{i}.csv",
            extension=".csv",
            tamano=100 + i,
            mtime=datetime(2023, 1, 1, tzinfo=UTC),
        )
        for i in range(cuantos)
    ]
    cola.insertar_pendientes(conexion, filas)
    conexion.commit()


class TestClaimConcurrente:
    def test_n_workers_cero_duplicados_cero_perdidos(self, conexion: Any, dsn: str) -> None:
        """INVARIANTE (DoD Fase 1): el claim jamás entrega la misma fila a dos workers."""
        total = 300
        _sembrar(conexion, total)

        reclamados: list[str] = []
        candado = threading.Lock()

        def trabajador(nombre: str) -> None:
            with psycopg.connect(dsn) as conn:
                while True:
                    filas = cola.claim(
                        conn,
                        worker_id=nombre,
                        estado=Estado.PENDIENTE,
                        lote=20,
                        lease_segundos=60,
                    )
                    conn.commit()
                    if not filas:
                        return
                    with candado:
                        reclamados.extend(f.archivo_id for f in filas)

        hilos = [threading.Thread(target=trabajador, args=(f"w{i}",)) for i in range(8)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        assert len(reclamados) == total, "se perdieron items"
        assert len(set(reclamados)) == total, "¡claim duplicado entre workers!"


class TestLeases:
    def test_fila_con_lease_vigente_no_es_reclamable(self, conexion: Any) -> None:
        _sembrar(conexion, 1)
        primero = cola.claim(
            conexion, worker_id="w1", estado=Estado.PENDIENTE, lote=10, lease_segundos=60
        )
        assert len(primero) == 1
        segundo = cola.claim(
            conexion, worker_id="w2", estado=Estado.PENDIENTE, lote=10, lease_segundos=60
        )
        assert segundo == []

    def test_lease_vencido_vuelve_a_ser_reclamable(self, conexion: Any) -> None:
        """Worker muerto → su trabajo NO se pierde: al vencer el lease, otro lo toma."""
        _sembrar(conexion, 1)
        cola.claim(
            conexion, worker_id="w-muerto", estado=Estado.PENDIENTE, lote=10, lease_segundos=0
        )
        rescate = cola.claim(
            conexion, worker_id="w-vivo", estado=Estado.PENDIENTE, lote=10, lease_segundos=60
        )
        assert len(rescate) == 1
        assert rescate[0].archivo_id == "id-00000"


class TestPrioridadPorExtension:
    def _sembrar_extensiones(self, conexion: Any) -> None:
        cola.upsert_disco(conexion, "d1", "/mnt/d1")
        nombres = ["resto.csv", "d.zip", "c.rar", "b.7z", "a.txt"]
        prioridades = {".txt": 140, ".7z": 130, ".rar": 120, ".zip": 110}
        filas = [
            cola.FilaCatalogo(
                archivo_id=f"id-{n}",
                disco_id="d1",
                ruta=n,
                nombre=n,
                extension=f".{n.rsplit('.', 1)[1]}",
                tamano=100,
                mtime=datetime(2023, 1, 1, tzinfo=UTC),
                prioridad=prioridades.get(f".{n.rsplit('.', 1)[1]}", 0),
            )
            for n in nombres
        ]
        cola.insertar_pendientes(conexion, filas)
        conexion.commit()

    def test_claim_respeta_orden_txt_7z_rar_zip_resto(self, conexion: Any) -> None:
        """Decisión 2026-06-11: .txt primero, luego .7z, .rar, .zip, después el resto."""
        self._sembrar_extensiones(conexion)
        filas = cola.claim(
            conexion, worker_id="w1", estado=Estado.PENDIENTE, lote=10, lease_segundos=60
        )
        assert [f.nombre for f in filas] == ["a.txt", "b.7z", "c.rar", "d.zip", "resto.csv"]

    def test_guardar_precalificacion_conserva_prioridad_explicita(self, conexion: Any) -> None:
        """El orden por extensión (>100) sobrevive PENDIENTE→PRECALIFICADO."""
        _sembrar(conexion, 1)
        cola.claim(conexion, worker_id="w1", estado=Estado.PENDIENTE, lote=1, lease_segundos=60)
        from normalizacion.core.modelo import RutaDecision

        ok = cola.guardar_precalificacion(
            conexion,
            "id-00000",
            puntaje=70,
            ruta=RutaDecision.HOT,
            tipo_real="text/plain",
            senales={},
            motivo="ok",
            version_filtro="test",
            prioridad=140,
        )
        assert ok
        fila = conexion.execute(
            "SELECT prioridad, puntaje FROM archivos WHERE archivo_id = 'id-00000'"
        ).fetchone()
        assert fila == (140, 70)

    def test_guardar_precalificacion_sin_prioridad_usa_puntaje(self, conexion: Any) -> None:
        """Compatibilidad: sin el kwarg, prioridad = puntaje (comportamiento previo)."""
        _sembrar(conexion, 1)
        cola.claim(conexion, worker_id="w1", estado=Estado.PENDIENTE, lote=1, lease_segundos=60)
        from normalizacion.core.modelo import RutaDecision

        cola.guardar_precalificacion(
            conexion,
            "id-00000",
            puntaje=42,
            ruta=RutaDecision.HOT,
            tipo_real="text/plain",
            senales={},
            motivo="ok",
            version_filtro="test",
        )
        fila = conexion.execute(
            "SELECT prioridad FROM archivos WHERE archivo_id = 'id-00000'"
        ).fetchone()
        assert fila == (42,)


class TestTransiciones:
    def test_transicion_valida_libera_el_lease(self, conexion: Any) -> None:
        _sembrar(conexion, 1)
        cola.claim(conexion, worker_id="w1", estado=Estado.PENDIENTE, lote=1, lease_segundos=60)
        ok = cola.transicionar(conexion, "id-00000", Estado.PENDIENTE, Estado.PRECALIFICADO)
        assert ok
        fila = conexion.execute(
            "SELECT estado, worker_id, lease_hasta FROM archivos WHERE archivo_id = 'id-00000'"
        ).fetchone()
        assert fila == ("PRECALIFICADO", None, None)

    def test_transicion_invalida_lanza(self, conexion: Any) -> None:
        """INVARIANTE: nada llega a HECHO sin verificarse (la máquina lo impide)."""
        _sembrar(conexion, 1)
        with pytest.raises(cola.TransicionInvalida):
            cola.transicionar(conexion, "id-00000", Estado.PENDIENTE, Estado.HECHO)

    def test_transicion_perdida_devuelve_false(self, conexion: Any) -> None:
        """Si otro proceso ya movió la fila, transicionar devuelve False (no corrompe)."""
        _sembrar(conexion, 1)
        cola.transicionar(conexion, "id-00000", Estado.PENDIENTE, Estado.PRECALIFICADO)
        assert cola.transicionar(conexion, "id-00000", Estado.PENDIENTE, Estado.ERROR) is False

    def test_dead_letter_clasifica_motivo(self, conexion: Any) -> None:
        _sembrar(conexion, 1)
        ok = cola.marcar_error(conexion, "id-00000", Estado.PENDIENTE, "io_disco_ilegible")
        assert ok
        fila = conexion.execute(
            "SELECT estado, error_motivo, intentos FROM archivos WHERE archivo_id = 'id-00000'"
        ).fetchone()
        assert fila == ("ERROR", "io_disco_ilegible", 1)
