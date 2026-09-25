"""Integración: los ciclos de bloqueo de la corrida 7 (Matrix.rar) contra Postgres real.

Cada escenario reproduce, sentencia a sentencia, un ciclo que con el código anterior
Postgres resolvía matando a una de las dos transacciones (DeadlockDetected, 3 de 3
veces por escenario, medido en un Postgres 16 desechable). `lock_timeout` convierte
cualquier espera larga en un fallo del test en vez de colgar la suite.
"""

from __future__ import annotations

import inspect
import io
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from normalizacion.core import cola
from normalizacion.core.config import Config, PerillasWorker
from normalizacion.core.indexador import SinkNulo
from normalizacion.core.modelo import Estado
from normalizacion.ingesta.workers import extractores, orquestador

pytestmark = pytest.mark.integracion


@pytest.fixture()
def abrir(dsn: str) -> Iterator[Callable[[], psycopg.Connection[Any]]]:
    """Conexiones extra (una por 'worker'), con lock_timeout para no colgarse nunca."""
    abiertas: list[psycopg.Connection[Any]] = []

    def _abrir() -> psycopg.Connection[Any]:
        c = psycopg.connect(dsn)
        c.execute("SET lock_timeout = '5s'")
        c.commit()
        abiertas.append(c)
        return c

    yield _abrir
    for c in abiertas:
        c.rollback()
        c.close()


def _sembrar_precalificado(conexion: Any, ids: list[str]) -> None:
    cola.upsert_disco(conexion, "d1", "/mnt/d1")
    cola.insertar_pendientes(
        conexion,
        [
            cola.FilaCatalogo(
                archivo_id=i,
                disco_id="d1",
                ruta=f"{i}.txt",
                nombre=f"{i}.txt",
                extension=".txt",
                tamano=1,
                mtime=datetime(2024, 1, 1, tzinfo=UTC),
            )
            for i in ids
        ],
    )
    conexion.execute(
        "UPDATE archivos SET estado = 'PRECALIFICADO', puntaje = 50, ruta_decision = 'HOT',"
        " tipo_real = 'text/plain', motivo = 'ok', version_filtro = 't', senales = '{}'"
    )
    conexion.commit()


def _claim(conn: psycopg.Connection[Any], worker_id: str, lote: int, lease: int) -> list[str]:
    filas = cola.claim(
        conn, worker_id=worker_id, estado=Estado.PRECALIFICADO, lote=lote, lease_segundos=lease
    )
    conn.commit()
    return [f.archivo_id for f in filas]


def _a_en_proceso(conn: psycopg.Connection[Any], archivo_id: str, worker_id: str) -> bool:
    return cola.transicionar(
        conn,
        archivo_id,
        Estado.PRECALIFICADO,
        Estado.EN_PROCESO,
        conservar_lease=True,
        worker_id=worker_id,
    )


def _en_hilo(fn: Callable[[], Any]) -> tuple[threading.Thread, list[BaseException]]:
    errores: list[BaseException] = []

    def cuerpo() -> None:
        try:
            fn()
        except BaseException as exc:  # el hilo reporta; el test decide
            errores.append(exc)

    h = threading.Thread(target=cuerpo)
    h.start()
    return h, errores


class TestCiclosDeLaCorrida7:
    def test_heartbeats_cruzados_con_worker_id_compartido(
        self, conexion: Any, abrir: Callable[[], psycopg.Connection[Any]]
    ) -> None:
        """Los 6 extras eran todos 'worker-1'. Cada uno con su fila en curso sin
        confirmar; el heartbeat de A esperaba la de B y el de B la de A. Aun con el id
        compartido, un heartbeat acotado al lote y sin esperas no puede cruzarse."""
        _sembrar_precalificado(conexion, ["a", "b"])
        ca, cb = abrir(), abrir()
        lote_a = _claim(ca, "worker-1", 1, 300)
        lote_b = _claim(cb, "worker-1", 1, 300)
        assert _a_en_proceso(ca, lote_a[0], "worker-1")
        assert _a_en_proceso(cb, lote_b[0], "worker-1")

        h, errores = _en_hilo(lambda: cola.renovar_lease(ca, "worker-1", 300, archivo_ids=lote_a))
        time.sleep(0.3)
        renovadas_b = cola.renovar_lease(cb, "worker-1", 300, archivo_ids=lote_b)
        h.join(timeout=10)
        assert errores == [], f"el heartbeat de A falló: {errores!r}"
        assert renovadas_b == 1
        ca.commit()
        cb.commit()

    def test_lease_vencido_el_dueno_anterior_no_pisa_filas_ajenas(
        self, conexion: Any, abrir: Callable[[], psycopg.Connection[Any]]
    ) -> None:
        """Ids únicos (pipeline-wN). Y se atasca en un archivo largo, su lease vence y X
        re-reclama el resto del lote. Antes Y transicionaba igual una fila ya de X, y el
        heartbeat de X contra el transicionar de Y cerraba el ciclo que mató a w3."""
        _sembrar_precalificado(conexion, ["a", "b", "c"])
        cy, cx = abrir(), abrir()
        _claim(cy, "Y", 3, 1)  # lease de 1 s
        assert _a_en_proceso(cy, "a", "Y")  # Y retiene 'a': su archivo largo
        time.sleep(1.5)
        lote_x = _claim(cx, "X", 3, 300)
        assert lote_x == ["b", "c"], "'a' está bloqueada por Y: el claim la salta"
        assert _a_en_proceso(cx, "c", "X")

        assert _a_en_proceso(cy, "b", "Y") is False, "Y ya no es dueño de 'b'"
        h, errores = _en_hilo(lambda: cola.renovar_lease(cx, "X", 300, archivo_ids=lote_x))
        time.sleep(0.3)
        assert _a_en_proceso(cy, "c", "Y") is False
        h.join(timeout=10)
        assert errores == [], f"el heartbeat de X falló: {errores!r}"
        cx.commit()
        cy.commit()

        duenos = dict(conexion.execute("SELECT archivo_id, worker_id FROM archivos").fetchall())
        assert duenos == {"a": "Y", "b": "X", "c": "X"}

    def test_recuperar_huerfanos_no_espera_a_un_worker_vivo(
        self, conexion: Any, abrir: Callable[[], psycopg.Connection[Any]]
    ) -> None:
        """Una fila EN_PROCESO con el lease vencido pero bloqueada la está tocando un
        proceso vivo. Antes, el worker que arrancaba se quedaba esperándola (y cerraba
        ciclos con los heartbeats); ahora rescata solo las huérfanas de verdad."""
        _sembrar_precalificado(conexion, ["a", "b"])
        vivo, nuevo = abrir(), abrir()
        _claim(vivo, "W", 2, 1)
        assert _a_en_proceso(vivo, "a", "W")
        assert _a_en_proceso(vivo, "b", "W")
        vivo.commit()
        time.sleep(1.5)
        vivo.execute("UPDATE archivos SET actualizado_en = now() WHERE archivo_id = 'a'")

        inicio = time.monotonic()
        rescatadas = cola.recuperar_huerfanos(nuevo)
        nuevo.commit()
        assert rescatadas == 1
        assert time.monotonic() - inicio < 2, "recuperar_huerfanos esperó una fila bloqueada"
        vivo.rollback()

    def test_renovar_salta_la_fila_que_otro_tiene_bloqueada(
        self, conexion: Any, abrir: Callable[[], psycopg.Connection[Any]]
    ) -> None:
        _sembrar_precalificado(conexion, ["a", "b"])
        w, otro = abrir(), abrir()
        lote = _claim(w, "W", 2, 300)
        otro.execute("SELECT 1 FROM archivos WHERE archivo_id = 'a' FOR UPDATE")

        inicio = time.monotonic()
        assert cola.renovar_lease(w, "W", 300, archivo_ids=lote) == 1
        w.commit()
        assert time.monotonic() - inicio < 2, "el heartbeat esperó una fila bloqueada"
        otro.rollback()

    def test_el_latido_renueva_el_lote_desde_su_hilo(
        self, conexion: Any, dsn: str, abrir: Callable[[], psycopg.Connection[Any]]
    ) -> None:
        """Mientras el hilo principal está parado en un archivo largo, el lease avanza."""
        _sembrar_precalificado(conexion, ["a", "b"])
        w = abrir()
        lote = _claim(w, "W", 2, 60)
        antes = dict(conexion.execute("SELECT archivo_id, lease_hasta FROM archivos").fetchall())
        conexion.commit()
        with cola.Latido(
            lambda: psycopg.connect(dsn, autocommit=True), "W", 60, intervalo_s=0.2
        ) as latido:
            latido.vigilar(lote)
            time.sleep(1.0)  # "el archivo largo"
        despues = dict(conexion.execute("SELECT archivo_id, lease_hasta FROM archivos").fetchall())
        assert all(despues[i] > antes[i] for i in lote), "el latido no renovó el lease"


# ------------------------------------------------------------------ el worker entero


class _Almacen:
    def existe(self, _h: str) -> bool:
        return False

    def guardar(self, _h: str, _f: Any, _n: int) -> None:
        return None


@pytest.fixture()
def worker_hot(
    conexion: Any, dsn: str, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., orquestador.ResumenWorker]:
    """`procesar_hot` real contra Postgres real; solo se neutraliza lo que no es cola."""
    monkeypatch.setattr(orquestador.recursos, "esperar_si_presion", lambda *a, **k: None)
    monkeypatch.setattr(
        orquestador, "_abrir_fuente", lambda _c, _r, f: io.BytesIO(f.archivo_id.encode())
    )
    monkeypatch.setattr(
        orquestador,
        "_extraer_o_reusar",
        lambda *_a, **_k: (extractores.ResultadoExtraccion(), False),
    )
    config = Config(
        _env_file=None,
        postgres_dsn=dsn,
        worker=PerillasWorker(lote_claim=25, lease_segundos=300),
    )

    def correr(worker_id: str = "W", **kw: Any) -> orquestador.ResumenWorker:
        return orquestador.procesar_hot(
            config, worker_id=worker_id, sink=SinkNulo(), almacen=_Almacen(), **kw
        )

    return correr


def _deadlock_en(monkeypatch: pytest.MonkeyPatch, funcion: str, llamada: int) -> None:
    """La `llamada`-ésima vez que el worker llama a `cola.<funcion>`, Postgres lo elige
    víctima. La excepción se lanza desde Python; el rollback que sigue es el real."""
    real = getattr(cola, funcion)
    llamadas = [0]

    def con_deadlock(*a: Any, **kw: Any) -> Any:
        llamadas[0] += 1
        if llamadas[0] == llamada:
            raise psycopg.errors.DeadlockDetected("deadlock detected (inyectado)")
        return real(*a, **kw)

    monkeypatch.setattr(cola, funcion, con_deadlock)


class TestUnDeadlockNoDejaElLoteAMedias:
    @pytest.mark.parametrize(
        ("funcion", "llamada"), [("registrar_persistencia", 1), ("transicionar", 3)]
    )
    def test_el_lote_termina_entero_en_la_misma_pasada(
        self,
        conexion: Any,
        worker_hot: Callable[..., orquestador.ResumenWorker],
        monkeypatch: pytest.MonkeyPatch,
        funcion: str,
        llamada: int,
    ) -> None:
        """El rollback de un deadlock deshace la transacción ENTERA del lote. Repitiendo
        solo la sentencia que falló, con el deadlock en el cierre quedaban 0 de 5
        procesadas y las 5 filas en PRECALIFICADO, a nombre del worker y con 300 s de
        lease vivo; en el tercer transicionar, las dos primeras igual."""
        ids = [f"r{i}" for i in range(5)]
        _sembrar_precalificado(conexion, ids)
        _deadlock_en(monkeypatch, funcion, llamada)

        resumen = worker_hot()

        assert resumen.procesados == 5
        filas = conexion.execute(
            "SELECT estado, worker_id, lease_hasta FROM archivos ORDER BY archivo_id"
        ).fetchall()
        assert filas == [("INDEXADO", None, None)] * 5


class TestUnWorkerColgadoNoRetieneSuLote:
    def test_suelta_lo_que_no_ha_tocado(
        self,
        conexion: Any,
        dsn: str,
        worker_hot: Callable[..., orquestador.ResumenWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """El primer archivo del lote se cuelga (proceso vivo). Antes, el latido renovaba
        las otras 4 filas mientras durase el cuelgue y ningún otro worker podía tomarlas;
        ahora, pasado el umbral, vuelven a la cola y el colgado, al volver, no las pisa."""
        ids = [f"r{i}" for i in range(5)]
        _sembrar_precalificado(conexion, ids)

        # Sin el arreglo el Latido no conoce el umbral: se descarta y el test falla por lo
        # que hace el worker, no por la firma.
        admitidas = inspect.signature(cola.Latido).parameters
        opciones = {"intervalo_s": 0.1, "umbral_retenido_s": 0.5}
        opciones = {c: v for c, v in opciones.items() if c in admitidas}

        class _LatidoRapido(cola.Latido):  # type: ignore[misc, valid-type]
            def __init__(self, *a: Any, **k: Any) -> None:
                super().__init__(*a, **opciones, **k)

        monkeypatch.setattr(cola, "Latido", _LatidoRapido)
        otro_reclamo: list[str] = []
        fin_del_cuelgue = threading.Event()

        def abrir(_c: Any, _r: str, fila: Any) -> Any:
            if fila.archivo_id == "r0":
                fin_del_cuelgue.wait(timeout=10)
            return io.BytesIO(fila.archivo_id.encode())

        monkeypatch.setattr(orquestador, "_abrir_fuente", abrir)
        resumenes: list[orquestador.ResumenWorker] = []
        h, errores = _en_hilo(lambda: resumenes.append(worker_hot("COLGADO")))
        with psycopg.connect(dsn) as otro:
            limite = time.monotonic() + 6
            while not otro_reclamo and time.monotonic() < limite:
                time.sleep(0.2)
                otro_reclamo = _claim(otro, "OTRO", 25, 300)
        fin_del_cuelgue.set()
        h.join(timeout=20)

        assert errores == []
        assert otro_reclamo == ["r1", "r2", "r3", "r4"], "el lote siguió retenido"
        assert resumenes[0].procesados == 1
        filas = dict(
            conexion.execute(
                "SELECT archivo_id, estado || ':' || coalesce(worker_id, '-') FROM archivos"
            ).fetchall()
        )
        assert filas == {"r0": "INDEXADO:-", **dict.fromkeys(ids[1:], "PRECALIFICADO:OTRO")}
