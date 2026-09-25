"""Deadlocks de la cola en la corrida de Matrix.rar (matriz, 2026-09-24).

187 deadlocks en 30 h; uno mató a pipeline-w3 y dejó la corrida 7 FALLIDA sin mover
a frío, verificar ni evaluar la puerta. Tres causas, cada una con su guarda aquí:

- 6 `norm worker` sin id compartían el worker_id por defecto 'worker-1';
- renovar_lease bloqueaba TODAS las filas del worker, sin orden y esperando;
- transicionar y compañía no miraban el dueño: tras un lease vencido dos workers
  trabajaban las mismas filas.

Más tres defensas: un deadlock ya no mata al worker ni deshace el trabajo del lote
(reintento que rehace lo pendiente), el latido corre en un hilo, así que un archivo
largo (la extracción de Matrix.rar a la caché tardó 23 min 36 s) no deja vencer el
lote, y un worker colgado suelta lo que no ha tocado.

Postgres simulado: `_BD` contesta por tipo de sentencia, anota qué conexión mandó qué
y lleva el estado de cada fila con transacciones de verdad: lo que una conexión no
confirma solo lo ve ella, y un rollback lo descarta. Los ciclos reales, con Postgres,
están en tests/integracion/test_cola_deadlocks.py.
"""

from __future__ import annotations

import inspect
import io
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from normalizacion.core import cola
from normalizacion.core.config import Config, PerillasWorker
from normalizacion.core.indexador import SinkNulo
from normalizacion.core.modelo import Estado
from normalizacion.ingesta.workers import extractores, orquestador

WORKER_ID_FIJO = "w-prueba"
ESCRITURAS = {"transicionar", "registrar", "marcar_error", "fallo_transitorio"}
Fila = tuple[str, str | None]  # (estado, worker_id)


# ------------------------------------------------------------------ Postgres simulado


class _Cursor:
    def __init__(self, rowcount: int = 0, filas: list[tuple[Any, ...]] | None = None) -> None:
        self.rowcount = rowcount
        self._filas = filas or []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._filas)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._filas[0] if self._filas else None


def _tipo(sql: str) -> str:
    if "FROM discos" in sql:
        return "montajes"
    if "FROM control" in sql:
        return "control"
    if "WITH reclamadas" in sql:
        return "claim"
    if sql.startswith("UPDATE archivos SET lease_hasta"):
        return "renovar"
    if sql.startswith("UPDATE archivos SET worker_id = NULL") and "RETURNING" in sql:
        return "liberar"
    if sql.startswith("UPDATE archivos SET estado = 'PRECALIFICADO'"):
        return "recuperar"
    if sql.startswith("UPDATE archivos SET estado = %s, motivo"):
        return "transicionar"
    if "estado = 'INDEXADO'" in sql:
        return "registrar"
    if "estado = 'ERROR'" in sql:
        return "marcar_error"
    if "intentos = intentos + 1, error_motivo" in sql:
        return "fallo_transitorio"
    return "otro"


# Dónde va cada dato en los parámetros de cada escritura (el dueño, si lo hay, al final):
# transicionar (a, motivo, ID, de, [wid]) · registrar (hash, ID, [wid])
# marcar_error (motivo, ID, de, [wid]) · fallo_transitorio (destino, motivo, s, ID, de, [wid])
_POS_ID = {"transicionar": 2, "registrar": 1, "marcar_error": 1, "fallo_transitorio": 3}
_LARGO_SIN_DUENO = {"transicionar": 4, "registrar": 2, "marcar_error": 3, "fallo_transitorio": 5}


class _BD:
    """Estado compartido por todas las conexiones falsas de un test."""

    def __init__(self) -> None:
        self.lote: list[str] = []
        self.claims = 0
        self.log: list[tuple[str, str, str, Any]] = []  # (conexión, tipo, sql, params)
        self.fallar: dict[str, set[int]] = {}  # tipo → en qué llamadas (1, 2…) hay deadlock
        self.llamadas: dict[str, int] = {}
        self.rowcount_cero: set[tuple[str, str]] = set()  # (tipo, archivo_id): ya es de otro
        self.filas: dict[str, Fila] = {}  # lo CONFIRMADO
        self.conexiones: list[_Conexion] = []
        self.renovado_por_latido = threading.Event()
        self.liberado = threading.Event()
        self._candado = threading.Lock()

    def conectar(self, *_a: Any, **kw: Any) -> _Conexion:
        c = _Conexion(self, autocommit=bool(kw.get("autocommit")))
        self.conexiones.append(c)
        return c

    def ejecutar(self, conn: _Conexion, sql: str, params: Any) -> _Cursor:
        sql = " ".join(sql.split())
        tipo = _tipo(sql)
        with self._candado:
            self.log.append((conn.nombre, tipo, sql, params))
            self.llamadas[tipo] = n = self.llamadas.get(tipo, 0) + 1
            if n in self.fallar.get(tipo, set()):
                raise psycopg.errors.DeadlockDetected("deadlock detected (simulado)")
            return self._responder(conn, tipo, sql, params)

    def _responder(self, conn: _Conexion, tipo: str, sql: str, params: Any) -> _Cursor:
        if tipo == "montajes":
            return _Cursor(filas=[("d1", "/raiz")])
        if tipo == "claim":
            self.claims += 1
            if self.claims > 1:
                return _Cursor()
            for a in self.lote:
                conn.escribir(a, ("PRECALIFICADO", params[0]))
            return _Cursor(filas=[_fila_claim(a) for a in self.lote])
        if tipo == "renovar":
            if conn.autocommit:  # solo el hilo del latido renueva en autocommit
                self.renovado_por_latido.set()
            lote = params[1] if isinstance(params[1], list) else [None]
            return _Cursor(rowcount=len(lote))
        if tipo == "liberar":
            ids, wid, estado = params
            liberadas = [a for a in ids if conn.ve(a) == (estado, wid)]
            for a in liberadas:
                conn.escribir(a, (estado, None))
            self.liberado.set()
            return _Cursor(filas=[(a,) for a in liberadas])
        if tipo in ESCRITURAS:
            return _Cursor(rowcount=self._escritura(conn, tipo, sql, params))
        return _Cursor()

    def _escritura(self, conn: _Conexion, tipo: str, sql: str, params: Any) -> int:
        archivo_id = params[_POS_ID[tipo]]
        if (tipo, archivo_id) in self.rowcount_cero:
            return 0
        dueno = params[-1] if len(params) > _LARGO_SIN_DUENO[tipo] else None
        estado, worker = conn.ve(archivo_id) or ("?", None)
        if dueno is not None and worker != dueno:
            return 0
        if tipo == "transicionar":
            if estado != params[3]:
                return 0
            conserva = "worker_id = NULL" not in sql.split("WHERE")[0]
            conn.escribir(archivo_id, (params[0], worker if conserva else None))
        elif tipo == "registrar":
            if estado != "EN_PROCESO":
                return 0
            conn.escribir(archivo_id, ("INDEXADO", None))
        elif tipo == "marcar_error":
            if estado != params[2]:
                return 0
            conn.escribir(archivo_id, ("ERROR", None))
        else:  # fallo_transitorio
            if estado != params[4]:
                return 0
            conn.escribir(archivo_id, (params[0], None))
        return 1

    def de(self, tipo: str) -> list[tuple[str, str, Any]]:
        return [(c, s, p) for (c, t, s, p) in self.log if t == tipo]

    def worker_id_del_claim(self) -> str:
        return self.de("claim")[0][2][0]


class _Conexion:
    def __init__(self, bd: _BD, *, autocommit: bool) -> None:
        self.bd = bd
        self.autocommit = autocommit
        self.nombre = "autocommit" if autocommit else "datos"
        self.pendiente: dict[str, Fila] = {}  # lo que esta transacción aún no confirmó
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def ve(self, archivo_id: str) -> Fila | None:
        return self.pendiente.get(archivo_id, self.bd.filas.get(archivo_id))

    def escribir(self, archivo_id: str, fila: Fila) -> None:
        (self.bd.filas if self.autocommit else self.pendiente)[archivo_id] = fila

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        return self.bd.ejecutar(self, sql, params)

    def commit(self) -> None:
        self.commits += 1
        with self.bd._candado:
            self.bd.filas.update(self.pendiente)
            self.pendiente.clear()
            self.bd.log.append((self.nombre, "commit", "", None))

    def rollback(self) -> None:
        """Como Postgres: se pierde TODO lo que la transacción no había confirmado."""
        self.rollbacks += 1
        self.pendiente.clear()

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _Conexion:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _fila_claim(archivo_id: str) -> tuple[Any, ...]:
    # archivo_id, disco_id, ruta, nombre, extension, tamano, mtime, estado, intentos,
    # origen_contenedor, tipo_real, puntaje, senales, motivo, version_filtro, hash
    return (
        archivo_id, "d1", f"{archivo_id}.txt", f"{archivo_id}.txt", ".txt", 4,
        datetime(2024, 1, 1, tzinfo=UTC), "PRECALIFICADO", 0, None, "text/plain", 50,
        {}, "ok", "test", None,
    )  # fmt: skip


class _AlmacenFalso:
    def existe(self, _h: str) -> bool:
        return False

    def guardar(self, _h: str, _f: Any, _n: int) -> None:
        return None


class _LogEspia:
    def __init__(self) -> None:
        self.eventos: list[tuple[str, dict[str, Any]]] = []

    def warning(self, evento: str, **kw: Any) -> None:
        self.eventos.append((evento, kw))

    info = error = warning

    def de(self, evento: str) -> list[dict[str, Any]]:
        return [kw for (e, kw) in self.eventos if e == evento]


def _config() -> Config:
    return Config(
        _env_file=None,
        postgres_dsn="postgresql://falso@nada/x",
        worker=PerillasWorker(lote_claim=10, lease_segundos=30),
    )


@pytest.fixture()
def bd(monkeypatch: pytest.MonkeyPatch) -> _BD:
    """Worker HOT con Postgres simulado y todo lo que no es cola neutralizado."""
    base = _BD()
    monkeypatch.setattr(orquestador.psycopg, "connect", base.conectar)
    monkeypatch.setattr(orquestador.recursos, "esperar_si_presion", lambda *a, **k: None)
    monkeypatch.setattr(orquestador, "_abrir_fuente", lambda _c, _r, _f: io.BytesIO(b"hola"))
    monkeypatch.setattr(
        orquestador,
        "_extraer_o_reusar",
        lambda *_a, **_k: (extractores.ResultadoExtraccion(), False),
    )
    return base


def _procesar(config: Config, **kw: Any) -> orquestador.ResumenWorker:
    return orquestador.procesar_hot(config, sink=SinkNulo(), almacen=_AlmacenFalso(), **kw)


def _sql_de(conn: _Conexion, llamada: Callable[[], Any]) -> tuple[str, Any]:
    antes = len(conn.bd.log)
    llamada()
    [(_c, _t, sql, params)] = conn.bd.log[antes:]
    return sql, params


def _abrir_con_veneno(*envenenados: str) -> Callable[[Config, str, Any], Any]:
    def abrir(_c: Config, _r: str, fila: Any) -> Any:
        if fila.archivo_id in envenenados:
            raise ValueError("bytes hostiles")
        return io.BytesIO(b"hola")

    return abrir


# ------------------------------------------------------------------ 1) worker_id único


class TestWorkerIdUnico:
    def test_dos_procesos_sin_worker_id_no_comparten_identidad(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Como los 6 `norm worker` de /sup.sh: mismo contenedor, distinto pid."""
        vistos = []
        for pid in (101, 202):
            monkeypatch.setattr("os.getpid", lambda pid=pid: pid)
            bd.claims = 0
            bd.log.clear()
            _procesar(_config())
            vistos.append(bd.worker_id_del_claim())
        assert vistos[0] != vistos[1], f"dos procesos comparten worker_id: {vistos}"

    def test_mismo_pid_en_dos_contenedores_tampoco(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dentro de Docker el pid se repite entre contenedores; el hostname no."""
        monkeypatch.setattr("os.getpid", lambda: 7)
        vistos = []
        for host in ("contenedor-a", "contenedor-b"):
            monkeypatch.setattr("socket.gethostname", lambda host=host: host)
            bd.claims = 0
            bd.log.clear()
            _procesar(_config())
            vistos.append(bd.worker_id_del_claim())
        assert vistos[0] != vistos[1], f"dos contenedores comparten worker_id: {vistos}"

    def test_pipeline_en_dos_contenedores_no_repite_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`pipeline-w1` solo es único dentro de UN pipeline."""
        from normalizacion.ingesta import pipeline

        usados: list[str] = []

        def falso(_config: Config, worker_id: str, **_k: Any) -> orquestador.ResumenWorker:
            usados.append(worker_id)
            return orquestador.ResumenWorker(0, 0, 0, 0, 0, 0, 0)

        class _Cola:
            def put(self, _x: Any) -> None:
                return None

        monkeypatch.setattr(orquestador, "procesar_hot", falso)
        for host in ("matriz-a", "matriz-b"):
            monkeypatch.setattr("socket.gethostname", lambda host=host: host)
            pipeline._worker_en_proceso(_config(), False, threading.Event(), _Cola(), 1)
        assert usados[0] != usados[1], f"dos pipelines comparten worker_id: {usados}"
        assert all(u.startswith("pipeline-w1") for u in usados)


# ------------------------------------------------------------------ 2) renovar_lease


class TestRenovarLease:
    def test_solo_las_filas_del_lote_en_orden_y_sin_esperar(self) -> None:
        conn = _BD().conectar()
        sql, params = _sql_de(
            conn, lambda: cola.renovar_lease(conn, "w1", 300, archivo_ids=["c", "a", "b"])
        )
        assert "archivo_id = ANY(%s)" in sql, "debe acotarse al lote, no a todo el worker"
        assert "worker_id = %s" in sql, "solo las filas que siguen siendo suyas"
        assert "ORDER BY archivo_id" in sql
        assert "SKIP LOCKED" in sql, "un heartbeat que espera filas puede cerrar un ciclo"
        assert params == (300, ["a", "b", "c"], "w1")

    def test_lote_vacio_no_toca_la_base(self) -> None:
        conn = _BD().conectar()
        assert cola.renovar_lease(conn, "w1", 300, archivo_ids=[]) == 0
        assert conn.bd.log == []

    def test_sin_lote_tampoco_espera(self) -> None:
        """La llamada antigua (sin `archivo_ids`) sigue existiendo: tampoco puede esperar."""
        conn = _BD().conectar()
        sql, params = _sql_de(conn, lambda: cola.renovar_lease(conn, "w1", 300))
        assert "ANY(" not in sql
        assert "ORDER BY archivo_id" in sql
        assert "SKIP LOCKED" in sql
        assert params == (300, "w1")

    def test_recuperar_huerfanos_salta_filas_bloqueadas(self) -> None:
        """Una fila bloqueada la está tocando un proceso vivo: no es huérfana."""
        conn = _BD().conectar()
        sql, _ = _sql_de(conn, lambda: cola.recuperar_huerfanos(conn))
        assert "SKIP LOCKED" in sql
        assert "ORDER BY archivo_id" in sql


# ------------------------------------------------------------------ 3) exigir dueño


class TestSoloFilasPropias:
    @pytest.mark.parametrize(
        "llamada",
        [
            lambda c: cola.transicionar(
                c, "a", Estado.PRECALIFICADO, Estado.EN_PROCESO, worker_id="w1"
            ),
            lambda c: cola.registrar_persistencia(c, "a", "h" * 64, worker_id="w1"),
            lambda c: cola.marcar_error(c, "a", Estado.EN_PROCESO, "x", worker_id="w1"),
            lambda c: cola.fallo_transitorio(
                c,
                "a",
                estado_actual=Estado.EN_PROCESO,
                estado_retorno=Estado.PRECALIFICADO,
                motivo="x",
                intentos_actuales=0,
                intentos_max=3,
                backoff_s=1,
                worker_id="w1",
            ),
        ],
        ids=["transicionar", "registrar_persistencia", "marcar_error", "fallo_transitorio"],
    )
    def test_la_escritura_exige_que_la_fila_siga_siendo_suya(
        self, llamada: Callable[[Any], Any]
    ) -> None:
        conn = _BD().conectar()
        sql, params = _sql_de(conn, lambda: llamada(conn))
        assert sql.endswith("AND worker_id = %s"), sql
        assert params[-1] == "w1"

    def test_sin_worker_id_el_sql_no_cambia(self) -> None:
        """Guarda de compatibilidad (pasa también en HEAD): precalificador y verificador
        todavía no pasan dueño y su SQL debe seguir igual."""
        conn = _BD().conectar()
        sql, _ = _sql_de(
            conn, lambda: cola.transicionar(conn, "a", Estado.PENDIENTE, Estado.PRECALIFICADO)
        )
        assert "worker_id = %s" not in sql.split("WHERE", 1)[1]

    def test_el_worker_se_identifica_en_cada_escritura(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una fila sana y otra envenenada: toda escritura sobre ellas lleva el dueño."""
        bd.lote = ["a", "b"]
        monkeypatch.setattr(orquestador, "_abrir_fuente", _abrir_con_veneno("b"))
        _procesar(_config(), worker_id=WORKER_ID_FIJO)
        escrituras = [(t, p) for (_c, t, _s, p) in bd.log if t in ESCRITURAS]
        assert {t for t, _ in escrituras} == {"transicionar", "registrar", "marcar_error"}
        sin_dueno = [t for t, p in escrituras if p[-1] != WORKER_ID_FIJO]
        assert sin_dueno == [], f"escrituras sin comprobar dueño: {sin_dueno}"

    def test_fila_que_ya_es_de_otro_no_se_cuenta_como_procesada(self, bd: _BD) -> None:
        """Su lease venció y otro worker la re-reclamó: la contará quien la termine."""
        bd.lote = ["a", "b"]
        bd.rowcount_cero.add(("registrar", "b"))
        resumen = _procesar(_config(), worker_id=WORKER_ID_FIJO)
        assert resumen.procesados == 1

    def test_un_fallo_transitorio_sobre_fila_ajena_no_se_cuenta(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fallo_transitorio devolvía True aunque no tocara nada: el worker contaba un
        transitorio sobre una fila que ya era de otro."""
        bd.lote = ["a"]
        bd.rowcount_cero.add(("fallo_transitorio", "a"))

        def abrir(_c: Config, _r: str, _f: Any) -> Any:
            raise OSError("lectura intermitente")

        monkeypatch.setattr(orquestador, "_abrir_fuente", abrir)
        resumen = _procesar(_config(), worker_id=WORKER_ID_FIJO)
        assert (resumen.transitorios, resumen.errores) == (0, 0)

    def test_fallo_transitorio_distingue_fila_ajena(self) -> None:
        """Con dueño: None si la fila ya no era suya. Sin dueño (precalificador,
        verificador): el contrato de siempre, True."""
        base = _BD()
        base.filas["a"] = ("EN_PROCESO", "otro")
        conn = base.conectar()
        kw: dict[str, Any] = {
            "estado_actual": Estado.EN_PROCESO,
            "estado_retorno": Estado.PRECALIFICADO,
            "motivo": "x",
            "intentos_actuales": 0,
            "intentos_max": 3,
            "backoff_s": 1,
        }
        assert cola.fallo_transitorio(conn, "a", worker_id="w1", **kw) is None
        assert cola.fallo_transitorio(conn, "b", **kw) is True
        agotado = {**kw, "intentos_actuales": 2}
        assert cola.fallo_transitorio(conn, "a", worker_id="w1", **agotado) is None


# ------------------------------------------------------------------ 4) deadlock ≠ muerte


class TestDeadlockNoMataAlWorker:
    @pytest.mark.parametrize(
        ("donde", "llamada"),
        [
            ("recuperar", 1),
            ("claim", 1),
            ("transicionar", 1),
            ("transicionar", 2),
            ("registrar", 1),
            ("registrar", 2),
        ],
    )
    def test_un_deadlock_se_reintenta_sin_perder_el_lote(
        self, bd: _BD, donde: str, llamada: int
    ) -> None:
        """En HEAD el DeadlockDetected subía hasta matar el proceso (y la corrida). Y
        repetir solo la sentencia que falló no basta: Postgres deshace la transacción
        ENTERA, con los pasos a EN_PROCESO del lote. Sin rehacerlos, el cierre no
        encontraba las filas (medido en Postgres 16: 0 de 5 procesadas y las 5 retenidas
        300 s con sus documentos ya en el índice)."""
        bd.lote = ["a", "b"]
        bd.fallar[donde] = {llamada}
        resumen = _procesar(_config(), worker_id=WORKER_ID_FIJO)
        datos = next(c for c in bd.conexiones if not c.autocommit)
        assert datos.rollbacks >= 1, "tras un deadlock hay que hacer rollback"
        assert resumen.procesados == 2
        assert bd.filas == {"a": ("INDEXADO", None), "b": ("INDEXADO", None)}

    def test_un_deadlock_no_deshace_el_error_ya_anotado(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El dead-letter de un archivo envenenado va sin confirmar hasta el cierre: si
        el cierre choca, también hay que rehacerlo, o la fila vuelve a la cola retenida
        y el worker la cuenta como error sin que lo sea en la base."""
        bd.lote = ["a", "b", "c"]
        bd.fallar["registrar"] = {1}
        monkeypatch.setattr(orquestador, "_abrir_fuente", _abrir_con_veneno("c"))
        resumen = _procesar(_config(), worker_id=WORKER_ID_FIJO)
        assert (resumen.procesados, resumen.errores) == (2, 1)
        assert bd.filas == {
            "a": ("INDEXADO", None),
            "b": ("INDEXADO", None),
            "c": ("ERROR", None),
        }

    def test_tras_un_deadlock_devuelve_el_resultado_del_reintento(self) -> None:
        conn = _BD().conectar()
        llamadas = []

        def falla_una_vez() -> int:
            llamadas.append(1)
            if len(llamadas) == 1:
                raise psycopg.errors.DeadlockDetected("deadlock detected (simulado)")
            return 7

        assert cola.con_reintento(conn, falla_una_vez, que="prueba", espera_base_s=0) == 7
        assert (conn.rollbacks, conn.commits) == (1, 1)

    def test_sin_confirmar_no_hace_commit(self) -> None:
        conn = _BD().conectar()
        cola.con_reintento(conn, lambda: 1, que="prueba", confirmar=False)
        assert conn.commits == 0

    def test_dentro_del_lote_no_se_confirma_archivo_a_archivo(self, bd: _BD) -> None:
        """Guarda (pasa también en HEAD): un commit por archivo pagaría un fsync de WAL
        por archivo con la matriz al 57-87 % de presión de IO. Entre el primer paso a
        EN_PROCESO y el cierre del lote no hay commits (el lease es corto aquí: no toca
        el punto de confirmación entre archivos)."""
        bd.lote = ["a", "b", "c", "d", "e"]
        _procesar(_config(), worker_id=WORKER_ID_FIJO)
        tipos = [t for (c, t, _s, _p) in bd.log if c == "datos"]
        primero, cierre = tipos.index("transicionar"), tipos.index("registrar")
        assert tipos.count("transicionar") == 5
        assert "commit" not in tipos[primero:cierre], tipos

    def test_agotados_los_intentos_el_fallo_se_ve(self) -> None:
        """Jamás silencio: si la cola no deja escribir, el worker muere y la corrida
        se marca FALLIDA, como siempre."""
        conn = _BD().conectar()

        def siempre_deadlock() -> None:
            raise psycopg.errors.DeadlockDetected("deadlock detected (simulado)")

        with pytest.raises(psycopg.errors.DeadlockDetected):
            cola.con_reintento(conn, siempre_deadlock, que="prueba", intentos=4, espera_base_s=0)
        assert conn.rollbacks == 4
        assert conn.commits == 0

    def test_un_fallo_de_serializacion_tambien_se_reintenta(self) -> None:
        conn = _BD().conectar()
        llamadas = []

        def falla_una_vez() -> int:
            llamadas.append(1)
            if len(llamadas) == 1:
                raise psycopg.errors.SerializationFailure("could not serialize access")
            return 1

        assert cola.con_reintento(conn, falla_una_vez, que="prueba", espera_base_s=0) == 1

    @pytest.mark.parametrize(
        "error",
        [
            # Un fallo de conexión no es un deadlock: reintentarlo en silencio lo taparía.
            psycopg.OperationalError("server closed the connection"),
            # 40003: no se sabe si el commit se aplicó; repetir puede duplicar.
            psycopg.errors.StatementCompletionUnknown("statement completion unknown"),
        ],
        ids=["conexion_caida", "commit_incierto"],
    )
    def test_otros_errores_no_se_reintentan(self, error: Exception) -> None:
        conn = _BD().conectar()
        llamadas = []

        def falla() -> None:
            llamadas.append(1)
            raise error

        with pytest.raises(type(error)):
            cola.con_reintento(conn, falla, que="prueba", espera_base_s=0)
        assert len(llamadas) == 1


class TestTransaccionCola:
    def _tx(self) -> tuple[Any, list[str], Callable[..., Callable[[Any], str]]]:
        conn = _BD().conectar()
        hechas: list[str] = []
        fallos: set[str] = set()

        def paso(nombre: str, *, falla_una_vez: bool = False) -> Callable[[Any], str]:
            def op(_conn: Any) -> str:
                hechas.append(nombre)
                if falla_una_vez and nombre not in fallos:
                    fallos.add(nombre)
                    raise psycopg.errors.DeadlockDetected("deadlock detected (simulado)")
                return nombre

            return op

        return cola.TransaccionCola(conn), hechas, paso

    def test_tras_el_rollback_rehace_lo_pendiente_en_orden(self) -> None:
        tx, hechas, paso = self._tx()
        tx.escribir("uno", paso("uno"), confirmar=False)
        tx.escribir("dos", paso("dos"), confirmar=False)
        assert tx.escribir("tres", paso("tres", falla_una_vez=True)) == "tres"
        assert hechas == ["uno", "dos", "tres", "uno", "dos", "tres"]
        assert (tx.conn.rollbacks, tx.conn.commits) == (1, 1)

    def test_lo_confirmado_no_se_rehace(self) -> None:
        tx, hechas, paso = self._tx()
        tx.escribir("uno", paso("uno"), confirmar=False)
        tx.escribir("confirma", paso("confirma"))
        tx.escribir("dos", paso("dos"), confirmar=False)
        tx.escribir("cierre", paso("cierre", falla_una_vez=True))
        assert hechas == ["uno", "confirma", "dos", "cierre", "dos", "cierre"]

    def test_si_al_rehacer_cambia_el_resultado_se_ve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La fila dejó de ser del worker entre el primer intento y el rollback."""
        espia = _LogEspia()
        monkeypatch.setattr(cola, "log", espia)
        tx, _hechas, paso = self._tx()
        respuestas = iter([True, False])
        tx.escribir("transicionar", lambda _c: next(respuestas), confirmar=False)
        tx.escribir("cierre", paso("cierre", falla_una_vez=True))
        assert espia.de("cola_rehecho_distinto") == [
            {"operacion": "transicionar", "antes": True, "ahora": False}
        ]


# ------------------------------------------------------------------ 5) latido en hilo


def _espiar_latido(monkeypatch: pytest.MonkeyPatch, bd: _BD, **opciones: Any) -> None:
    """Cambia el latido del worker por uno rápido que anota cada `vigilar` en la base.
    Las opciones que el Latido aún no conoce se descartan, para que sin el arreglo el
    test falle por lo que hace el worker y no por la firma."""
    admitidas = inspect.signature(cola.Latido).parameters
    opciones = {k: v for k, v in opciones.items() if k in admitidas}

    class _Espia(cola.Latido):  # type: ignore[misc, valid-type]
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, intervalo_s=0.02, **opciones, **k)

        def vigilar(self, archivo_ids: Any) -> None:
            ids = tuple(archivo_ids)
            bd.log.append(("principal", "vigilar", "", ids))
            super().vigilar(ids)

    monkeypatch.setattr(cola, "Latido", _Espia)


class TestLatidoDuranteArchivoLargo:
    def test_el_lease_se_renueva_mientras_un_archivo_tarda(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Como la extracción de Matrix.rar (23 min 36 s) dentro de `_abrir_fuente`:
        con el latido solo entre archivos, el lease (300 s) del resto del lote vencía y
        otro worker se lo llevaba."""
        latido = getattr(cola, "Latido", None)
        if latido is not None:  # en HEAD no existe: el test falla por el comportamiento

            class _LatidoRapido(latido):  # type: ignore[misc, valid-type]
                def __init__(self, *a: Any, **k: Any) -> None:
                    super().__init__(*a, intervalo_s=0.02, **k)

            monkeypatch.setattr(cola, "Latido", _LatidoRapido)

        bd.lote = ["a", "b", "c"]
        renovado_durante: list[bool] = []

        def abrir_lento(_c: Config, _r: str, fila: Any) -> Any:
            if fila.archivo_id == "a":  # el archivo largo es el primero del lote
                renovado_durante.append(bd.renovado_por_latido.wait(timeout=2.0))
            return io.BytesIO(b"hola")

        monkeypatch.setattr(orquestador, "_abrir_fuente", abrir_lento)
        _procesar(_config(), worker_id=WORKER_ID_FIJO)
        assert renovado_durante == [True], "el lease no se renovó durante el archivo largo"
        del_latido = [p for (c, _s, p) in bd.de("renovar") if c == "autocommit"]
        assert del_latido[0] == (30, ["a", "b", "c"], WORKER_ID_FIJO)

    def test_al_cerrar_el_lote_el_worker_deja_de_renovarlo(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo que sobreviva a un lote debe poder vencer, no quedar retenido por el hilo."""
        _espiar_latido(monkeypatch, bd)
        bd.lote = ["a", "b"]
        _procesar(_config(), worker_id=WORKER_ID_FIJO)
        orden = [(t, p) for (_c, t, _s, p) in bd.log if t in {"vigilar", "registrar", "claim"}]
        assert ("vigilar", ("a", "b")) in orden
        cierre = max(i for i, (t, _p) in enumerate(orden) if t == "registrar")
        siguiente = orden[cierre + 1 : cierre + 3]
        assert [t for t, _p in siguiente] == ["vigilar", "claim"], orden
        assert siguiente[0][1] == (), f"tras cerrar el lote sigue vigilándolo: {orden}"

    def test_un_latido_que_falla_no_tumba_nada(self) -> None:
        def sin_red() -> Any:
            raise psycopg.OperationalError("connection refused")

        latido = cola.Latido(sin_red, "w1", 30)
        latido.vigilar(["a"])
        assert latido.latir() == 0

    def test_al_vaciar_el_lote_deja_de_renovarlo(self) -> None:
        base = _BD()
        conn = base.conectar(autocommit=True)
        latido = cola.Latido(lambda: conn, "w1", 30)
        latido.vigilar(["a", "b"])
        assert latido.latir() == 2
        latido.vigilar(())
        base.log.clear()
        assert latido.latir() == 0
        assert base.log == []

    def test_el_hilo_se_detiene_al_salir(self) -> None:
        base = _BD()
        with cola.Latido(
            lambda: base.conectar(autocommit=True), "w-hilo", 30, intervalo_s=0.01
        ) as latido:
            latido.vigilar(["a"])
            assert base.renovado_por_latido.wait(timeout=2.0)
        assert [h for h in threading.enumerate() if h.name == "latido-w-hilo"] == []
        assert all(c.closed for c in base.conexiones)


# ------------------------------------------------------------------ 6) worker colgado


class _Reloj:
    def __init__(self) -> None:
        self.ahora = 1000.0

    def __call__(self) -> float:
        return self.ahora


class TestWorkerColgado:
    """Un worker vivo pero colgado renovaba su lote para siempre y sin rastro: medido con
    lease de 3 s y un archivo parado 12 s, otro worker no reclamó ni una fila."""

    def _latido(
        self, monkeypatch: pytest.MonkeyPatch, umbral: float
    ) -> tuple[_BD, Any, _Reloj, _LogEspia]:
        base = _BD()
        for a in ("a", "b", "c"):
            base.filas[a] = ("PRECALIFICADO", "W")
        conn = base.conectar(autocommit=True)
        reloj, espia = _Reloj(), _LogEspia()
        monkeypatch.setattr(cola.time, "monotonic", reloj)
        monkeypatch.setattr(cola, "log", espia)
        latido = cola.Latido(lambda: conn, "W", 30, umbral_retenido_s=umbral)
        latido.vigilar(["a", "b", "c"])
        return base, latido, reloj, espia

    def test_suelta_lo_que_no_ha_tocado_y_lo_dice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        base, latido, reloj, espia = self._latido(monkeypatch, umbral=3600)
        latido.empezar("a")
        reloj.ahora += 3599
        latido.latir()
        assert base.de("liberar") == [], "un archivo largo pero legítimo no suelta nada"

        reloj.ahora += 2
        latido.latir()
        [(_c, _s, params)] = base.de("liberar")
        assert params == (["b", "c"], "W", "PRECALIFICADO")
        assert base.filas == {
            "a": ("PRECALIFICADO", "W"),  # la fila en curso sigue siendo suya
            "b": ("PRECALIFICADO", None),
            "c": ("PRECALIFICADO", None),
        }
        assert espia.de("lote_retenido") == [
            {
                "worker": "W",
                "archivo_id": "a",
                "segundos": 3601,
                "filas_liberadas": 2,
                "filas_sin_tocar": 0,
            }
        ]
        _c, _s, ultima = base.de("renovar")[-1]
        assert ultima == (30, ["a"], "W"), "lo liberado ya no se renueva"

    def test_el_aviso_se_repite_mientras_siga_colgado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _base, latido, reloj, espia = self._latido(monkeypatch, umbral=100)
        latido.empezar("a")
        for _ in range(3):
            reloj.ahora += 101
            latido.latir()
            latido.latir()  # un latido más dentro del mismo umbral no repite el aviso
        assert [e["segundos"] for e in espia.de("lote_retenido")] == [101, 202, 303]

    def test_el_worker_se_salta_lo_que_el_latido_solto(
        self, bd: _BD, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """De extremo a extremo: el primer archivo se cuelga, el latido suelta el resto
        del lote y, cuando el worker vuelve, no pisa esas filas (ya no son suyas)."""
        _espiar_latido(monkeypatch, bd, umbral_retenido_s=0.05)
        bd.lote = ["a", "b", "c"]
        soltado_durante: list[bool] = []

        def abrir_colgado(_c: Config, _r: str, fila: Any) -> Any:
            if fila.archivo_id == "a":
                soltado_durante.append(bd.liberado.wait(timeout=2.0))
            return io.BytesIO(b"hola")

        monkeypatch.setattr(orquestador, "_abrir_fuente", abrir_colgado)
        resumen = _procesar(_config(), worker_id=WORKER_ID_FIJO)
        assert soltado_durante == [True], "el latido no soltó el lote de un worker colgado"
        assert resumen.procesados == 1
        assert bd.filas == {
            "a": ("INDEXADO", None),
            "b": ("PRECALIFICADO", None),
            "c": ("PRECALIFICADO", None),
        }
