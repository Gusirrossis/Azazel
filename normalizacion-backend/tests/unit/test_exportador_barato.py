"""El exportador no recorre `archivos` entera en cada pasada.

Medido en la matriz (25-09): el GROUP BY por `ruta_decision` es un Parallel Seq Scan de
11 GB que tardaba 2,7 s y se lanzaba cada 15 s aunque la cola estuviera vacía. Estos
tests fijan cuándo se lanza: como mucho una vez por `intervalo_caro_s` mientras la
tabla cambia, y una vez por `edad_maxima_s` si está quieta. Sin Postgres: una conexión
falsa que responde por el texto de la consulta y cuenta cuántas veces se lanzó cada una.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest

from normalizacion.core.observabilidad.metricas import Exportador

RAIZ = Path(__file__).resolve().parents[2]

#: Fragmentos que identifican cada consulta del exportador.
_CARA = "ruta_decision"  # la del heap entero
_ERRORES = "estado = 'ERROR'"
_BACKLOG = "GROUP BY estado"
_HUELLA = "pg_stat_user_tables"


class _Cursor:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self._filas = filas

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._filas)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._filas[0] if self._filas else None


class ConexionFalsa:
    def __init__(self) -> None:
        self.huella: tuple[Any, ...] | None = (100, 50, 0, 16409)
        self.estados: list[tuple[Any, ...]] = [("INDEXADO", 10), ("COLD", 5)]
        self.rutas: list[tuple[Any, ...]] = [("HOT", 10, 1000), ("COLD", 5, 500)]
        self.errores: list[tuple[Any, ...]] = [("agotado", 1)]
        self.falla_la_cara = False
        self.falla_el_backlog = False
        self.pausado = False
        #: fragmento de consulta → efecto que ocurre mientras esa consulta corre.
        self.durante: dict[str, Callable[[], None]] = {}
        self.lanzadas: list[str] = []

    def tocar_archivos(self) -> None:
        """Lo que haría un worker: un UPDATE más en `archivos`."""
        assert self.huella is not None
        ins, upd, dele, filenode = self.huella
        self.huella = (ins, upd + 1, dele, filenode)

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.lanzadas.append(sql)
        for fragmento, efecto in self.durante.items():
            if fragmento in sql:
                efecto()
        if _HUELLA in sql:
            return _Cursor([self.huella] if self.huella else [])
        if _CARA in sql:
            if self.falla_la_cara:
                raise psycopg.OperationalError("canceling statement due to lock timeout")
            return _Cursor(self.rutas)
        if _ERRORES in sql:
            return _Cursor(self.errores)
        if _BACKLOG in sql:
            if self.falla_el_backlog:
                raise psycopg.OperationalError("canceling statement due to lock timeout")
            return _Cursor(self.estados)
        if "FROM discos" in sql:
            return _Cursor([(0, 1)])
        if "FROM control" in sql:
            return _Cursor([("true",)] if self.pausado else [])
        raise AssertionError(f"consulta inesperada: {sql}")

    def cuantas(self, fragmento: str) -> int:
        return sum(fragmento in sql for sql in self.lanzadas)


class Reloj:
    def __init__(self) -> None:
        self.ahora = 1000.0

    def __call__(self) -> float:
        return self.ahora


@pytest.fixture()
def conn() -> ConexionFalsa:
    return ConexionFalsa()


@pytest.fixture()
def reloj() -> Reloj:
    return Reloj()


class TestConsultaCara:
    def test_pasadas_seguidas_con_la_cola_moviendose_recorren_el_heap_una_vez(
        self, conn: ConexionFalsa
    ) -> None:
        """El caso de producción: una corrida actualiza filas entre pasada y pasada.
        Con los valores por defecto, diez pasadas seguidas caen dentro del intervalo."""
        exportador = Exportador()
        for _ in range(10):
            exportador.recolectar(conn)  # type: ignore[arg-type]
            conn.tocar_archivos()

        assert conn.cuantas(_CARA) == 1
        assert conn.cuantas(_ERRORES) == 1
        # El backlog es el que leen las alertas y va por índice: ese sí sigue a la cola.
        assert conn.cuantas(_BACKLOG) == 10

    def test_cola_quieta_no_toca_archivos(self, conn: ConexionFalsa, reloj: Reloj) -> None:
        """Con la cola vacía no hay nada que recalcular, pase el tiempo que pase
        (dentro de la edad máxima)."""
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        for _ in range(6):
            exportador.recolectar(conn)  # type: ignore[arg-type]
            reloj.ahora += 500

        assert conn.cuantas(_CARA) == 1
        assert conn.cuantas(_BACKLOG) == 1

    def test_un_cambio_durante_la_espera_no_se_pierde(
        self, conn: ConexionFalsa, reloj: Reloj
    ) -> None:
        """La fila cambia a los 60 s y la tabla se queda quieta después. El recálculo
        tiene que llegar cuando se cumple el intervalo, aunque en la pasada anterior
        la huella ya no se moviera."""
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]

        reloj.ahora += 60
        conn.tocar_archivos()
        conn.rutas = [("HOT", 11, 1100), ("COLD", 4, 400)]
        exportador.recolectar(conn)  # type: ignore[arg-type]
        reloj.ahora += 60
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert conn.cuantas(_CARA) == 1
        assert 'norm_archivos_por_ruta{ruta_decision="HOT"} 10.0' in exportador.texto().decode()

        reloj.ahora += 180  # 300 s desde el último recorrido
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert conn.cuantas(_CARA) == 2
        assert 'norm_archivos_por_ruta{ruta_decision="HOT"} 11.0' in exportador.texto().decode()

    @pytest.mark.parametrize(
        ("consulta", "espera_s"), [(_BACKLOG, 60), (_CARA, 300)], ids=["backlog", "caro"]
    )
    def test_un_cambio_que_entra_mientras_se_agrega_no_se_pierde(
        self, conn: ConexionFalsa, reloj: Reloj, consulta: str, espera_s: int
    ) -> None:
        """El último UPDATE de una corrida entra justo mientras corre el GROUP BY, y
        después la cola se queda quieta. Si la huella se guardara leída DESPUÉS de
        agregar, ya incluiría ese cambio y nadie volvería a contar: el número quedaría
        viejo hasta la edad máxima (1 h)."""
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        conn.durante[consulta] = conn.tocar_archivos
        exportador.recolectar(conn)  # type: ignore[arg-type]
        del conn.durante[consulta]
        assert conn.cuantas(consulta) == 1

        reloj.ahora += espera_s
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert conn.cuantas(consulta) == 2

    def test_edad_maxima_recalcula_aunque_la_huella_no_se_mueva(
        self, conn: ConexionFalsa, reloj: Reloj
    ) -> None:
        """Red de seguridad para estadísticas reseteadas o `track_counts` apagado."""
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]
        reloj.ahora += 3600
        exportador.recolectar(conn)  # type: ignore[arg-type]

        assert conn.cuantas(_CARA) == 2
        assert conn.cuantas(_BACKLOG) == 2

    def test_sin_huella_cuenta_como_cambio(self, conn: ConexionFalsa, reloj: Reloj) -> None:
        conn.huella = None
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert conn.cuantas(_BACKLOG) == 2
        assert conn.cuantas(_CARA) == 1  # el intervalo sigue mandando

    def test_vacuum_full_cambia_el_filenode_y_obliga_a_recalcular(
        self, conn: ConexionFalsa, reloj: Reloj
    ) -> None:
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert conn.huella is not None
        ins, upd, dele, _ = conn.huella
        conn.huella = (ins, upd, dele, 99999)
        reloj.ahora += 300
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert conn.cuantas(_CARA) == 2


class TestFallos:
    def test_si_la_consulta_cara_falla_se_conserva_el_valor_y_se_reintenta(
        self, conn: ConexionFalsa, reloj: Reloj
    ) -> None:
        """Un bloqueo pasajero (p. ej. un VACUUM FULL) no puede dejar series vacías ni
        esperar otros cinco minutos para reintentar."""
        exportador = Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]

        reloj.ahora += 300
        conn.tocar_archivos()
        conn.rutas = [("HOT", 12, 1200)]
        conn.falla_la_cara = True
        with pytest.raises(psycopg.OperationalError):
            exportador.recolectar(conn)  # type: ignore[arg-type]
        texto = exportador.texto().decode()
        assert 'norm_archivos_por_ruta{ruta_decision="HOT"} 10.0' in texto
        assert 'norm_bytes_por_ruta{ruta_decision="COLD"} 500.0' in texto

        reloj.ahora += 15  # la siguiente pasada del daemon
        conn.falla_la_cara = False
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert 'norm_archivos_por_ruta{ruta_decision="HOT"} 12.0' in exportador.texto().decode()

    def test_si_el_backlog_falla_las_alertas_siguen_viendo_el_ultimo_valor(
        self, conn: ConexionFalsa
    ) -> None:
        """Vaciar el gauge antes de consultar dejaba `norm_backlog` sin series en cuanto
        la consulta fallaba: para Prometheus, ni ERROR ni PENDIENTE, sin más."""
        exportador = Exportador()
        exportador.recolectar(conn)  # type: ignore[arg-type]

        conn.tocar_archivos()
        conn.falla_el_backlog = True
        with pytest.raises(psycopg.OperationalError):
            exportador.recolectar(conn)  # type: ignore[arg-type]
        assert 'norm_backlog{estado="INDEXADO"} 10.0' in exportador.texto().decode()

    def test_con_archivos_bloqueada_la_pausa_se_sigue_publicando(self, conn: ConexionFalsa) -> None:
        """La ventana del VACUUM FULL: el sistema se pausa y `archivos` queda bloqueada
        (lock_timeout de 1 s en el rol). Lo que no lee `archivos` tiene que seguir al
        día; antes iba detrás del backlog y se quedaba sin publicar."""
        exportador = Exportador()
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert "norm_pausado 0.0" in exportador.texto().decode()

        conn.pausado = True
        conn.tocar_archivos()
        conn.falla_el_backlog = True
        with pytest.raises(psycopg.OperationalError):
            exportador.recolectar(conn)  # type: ignore[arg-type]
        assert "norm_pausado 1.0" in exportador.texto().decode()


def _ultima_ok(exportador: Exportador, nivel: str) -> float | None:
    return exportador.registry.get_sample_value(
        "norm_exportador_ultima_ok_timestamp", {"nivel": nivel}
    )


def _con_marcas(reloj: Reloj) -> Exportador:
    return Exportador(intervalo_caro_s=300, edad_maxima_s=3600, reloj=reloj, reloj_pared=reloj)


class TestAntiguedad:
    """Un gauge que conserva su último valor tras un fallo no se distingue de una cola
    quieta, y `up` sigue a 1 aunque Postgres no conteste. La marca de cada nivel dice
    hasta cuándo es cierto lo publicado."""

    def test_un_fallo_se_ve_como_una_marca_que_envejece(
        self, conn: ConexionFalsa, reloj: Reloj
    ) -> None:
        exportador = _con_marcas(reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert _ultima_ok(exportador, "backlog") == 1000.0

        reloj.ahora += 60
        conn.tocar_archivos()
        conn.falla_el_backlog = True
        for _ in range(3):
            with pytest.raises(psycopg.OperationalError):
                exportador.recolectar(conn)  # type: ignore[arg-type]
            reloj.ahora += 60
        # La base (discos, pausa) sí se confirmó; el backlog se quedó en su último acierto.
        assert _ultima_ok(exportador, "base") == 1180.0
        assert _ultima_ok(exportador, "backlog") == 1000.0

        conn.falla_el_backlog = False
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert _ultima_ok(exportador, "backlog") == 1240.0

    def test_la_cola_quieta_confirma_sin_consultar(self, conn: ConexionFalsa, reloj: Reloj) -> None:
        """Sin cambios la marca avanza igual: no puede parecer un fallo que el
        exportador no relea lo que no pudo cambiar."""
        exportador = _con_marcas(reloj)
        for _ in range(5):
            exportador.recolectar(conn)  # type: ignore[arg-type]
            reloj.ahora += 600
        assert conn.cuantas(_CARA) == 1
        assert _ultima_ok(exportador, "backlog") == 3400.0
        assert _ultima_ok(exportador, "caro") == 3400.0

    def test_el_caro_envejece_mientras_espera_su_turno(
        self, conn: ConexionFalsa, reloj: Reloj
    ) -> None:
        """Con la tabla cambiando, el valor caro deja de ser cierto hasta el siguiente
        recorrido, y su marca lo dice; el backlog, que sigue a la cola, no envejece."""
        exportador = _con_marcas(reloj)
        exportador.recolectar(conn)  # type: ignore[arg-type]
        for _ in range(4):
            reloj.ahora += 60
            conn.tocar_archivos()
            exportador.recolectar(conn)  # type: ignore[arg-type]
        assert _ultima_ok(exportador, "caro") == 1000.0
        assert _ultima_ok(exportador, "backlog") == 1240.0

        reloj.ahora += 60  # 300 s: toca recorrer
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert _ultima_ok(exportador, "caro") == 1300.0


class TestContratoDeSeries:
    def test_lo_que_leen_alertas_y_grafana_sigue_publicado(self, conn: ConexionFalsa) -> None:
        """Mismos nombres de serie que consumen `prometheus-alertas.yml` y el dashboard."""
        alertas = (RAIZ / "deploy" / "prometheus-alertas.yml").read_text(encoding="utf-8")
        consumidas = set(re.findall(r"\bnorm_[a-z_]+", alertas))
        dashboard = RAIZ / "deploy" / "grafana" / "dashboards" / "normalizacion.json"
        tablero = json.loads(dashboard.read_text(encoding="utf-8"))
        for panel in tablero["panels"]:
            for objetivo in panel.get("targets", []):
                consumidas |= set(re.findall(r"\bnorm_[a-z_]+", objetivo["expr"]))
        assert "norm_backlog" in consumidas  # el patrón encuentra algo de verdad

        exportador = Exportador()
        exportador.recolectar(conn)  # type: ignore[arg-type]
        texto = exportador.texto().decode()
        for serie in sorted(consumidas):
            assert f"# TYPE {serie} gauge" in texto, serie

    def test_una_pasada_que_se_salta_la_consulta_no_borra_series(self, conn: ConexionFalsa) -> None:
        exportador = Exportador()
        exportador.recolectar(conn)  # type: ignore[arg-type]
        antes = _series_de_datos(exportador)
        exportador.recolectar(conn)  # type: ignore[arg-type]
        assert _series_de_datos(exportador) == antes
        assert conn.cuantas(_CARA) == 1


def _series_de_datos(exportador: Exportador) -> list[str]:
    """Las muestras publicadas, sin las marcas de tiempo, que cambian en cada pasada."""
    return [
        linea
        for linea in exportador.texto().decode().splitlines()
        if not linea.startswith("norm_exportador_ultima_ok_timestamp")
    ]


def _migracion_0013() -> ModuleType:
    ruta = RAIZ / "alembic" / "versions" / "0013_autovacuum_archivos.py"
    spec = importlib.util.spec_from_file_location("migracion_0013", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _OpFalso:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> None:
        self.sql.append(sql)


class TestMigracionAutovacuum:
    def test_downgrade_deshace_exactamente_lo_que_pone_upgrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        modulo = _migracion_0013()
        op = _OpFalso()
        monkeypatch.setattr(modulo, "op", op)

        modulo.upgrade()
        modulo.downgrade()

        puesto, quitado = (sql for sql in op.sql if sql.startswith("ALTER TABLE"))
        assert puesto.startswith("ALTER TABLE archivos SET (")
        assert quitado.startswith("ALTER TABLE archivos RESET (")
        nombres_puestos = set(re.findall(r"(autovacuum_\w+) =", puesto))
        nombres_quitados = set(re.findall(r"autovacuum_\w+", quitado))
        assert nombres_puestos == nombres_quitados == set(modulo.PARAMETROS)

    @pytest.mark.parametrize("paso", ["upgrade", "downgrade"])
    def test_el_alter_no_espera_sin_limite_y_no_deja_el_limite_puesto(
        self, monkeypatch: pytest.MonkeyPatch, paso: str
    ) -> None:
        """La migración corre en el arranque de `api`. Detrás de un REINDEX CONCURRENTLY
        el ALTER esperaría horas sin dejar rastro, con la API sin arrancar. Con límite
        falla y lo dice en el log. Y el límite no se hereda: las migraciones que vengan
        detrás en el mismo `upgrade head` comparten transacción."""
        modulo = _migracion_0013()
        op = _OpFalso()
        monkeypatch.setattr(modulo, "op", op)

        getattr(modulo, paso)()

        assert op.sql[0] == f"SET LOCAL lock_timeout = '{modulo.LOCK_TIMEOUT}'"
        assert op.sql[1].startswith("ALTER TABLE archivos ")
        assert op.sql[2:] == ["SET LOCAL lock_timeout = DEFAULT"]

    def test_encadena_con_la_anterior(self) -> None:
        modulo = _migracion_0013()
        assert (modulo.revision, modulo.down_revision) == ("0013", "0012")
