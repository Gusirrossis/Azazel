"""Exportador Prometheus: el estado del sistema, scrapeable (Fase 6 → M6).

La fuente de verdad es la COLA (Postgres): el exportador la consulta cada N
segundos y publica gauges. Prometheus deriva throughput/ETA con delta() sobre
el gauge de HECHO; Grafana pinta los dashboards provisionados en deploy/grafana.

Las consultas NO cuestan lo mismo, y en la matriz (disco mecánico, `archivos` con
853.382 filas vivas en 11 GB de heap inflado) la diferencia es de dos órdenes. Los
planes, medidos con EXPLAIN el 25-09:

- backlog por estado → Parallel Index Only Scan de `ix_archivos_disco_estado` (181 MB).
- archivos y bytes por ruta → Parallel Seq Scan del heap ENTERO: ningún índice lleva
  `ruta_decision` y `tamano`. 2,7 s de media, y se lanzaba cada 15 s aunque la cola
  estuviera vacía: Postgres pasaba el 18 % del tiempo recorriendo 11 GB para publicar
  el mismo número. Un índice que la cubriera no sale gratis: solo el 0,4 % de los
  UPDATE de la cola son HOT, así que cada UPDATE pagaría una entrada de índice más.
- errores por motivo → `ix_archivos_estado_id_sel`, más una lectura del heap por fila
  ERROR (llegó a haber 411.293).

Por eso cada una va a su ritmo (ver `Exportador.recolectar`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psycopg
from prometheus_client import CollectorRegistry, Gauge, generate_latest, start_http_server

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("metricas")

#: Mínimo entre dos recorridos del heap entero mientras `archivos` cambia. La decisión
#: HOT/COLD solo se mueve durante la precalificación y ninguna alerta la lee
#: (`prometheus-alertas.yml` vigila `norm_backlog`): cinco minutos de retraso en una
#: tarta de Grafana no le importan a nadie; 2,7 s de IO cada 15 s en un disco que da
#: ~400 operaciones por segundo, sí.
INTERVALO_CARO_S = 300.0

#: Red de seguridad de la huella: aunque `archivos` parezca quieta, todo lo que la lee
#: se recalcula al menos una vez por hora. Cubre lo que la huella no ve (estadísticas
#: reseteadas, `track_counts` apagado) sin volver al recorrido constante.
EDAD_MAXIMA_S = 3600.0

#: Cualquier INSERT/UPDATE/DELETE sobre `archivos` mueve estos contadores, también
#: los que acaban en ROLLBACK (eso solo cuesta un recálculo de más). El filenode
#: cambia con TRUNCATE y VACUUM FULL, que no los mueven. Leerla es gratis: sale de
#: las estadísticas acumuladas, no de la tabla.
_SQL_HUELLA = (
    "SELECT n_tup_ins, n_tup_upd, n_tup_del, pg_relation_filenode(relid)"
    " FROM pg_stat_user_tables WHERE relid = 'archivos'::regclass"
)


def _sin_cambios(huella: tuple[Any, ...] | None, huella_previa: tuple[Any, ...] | None) -> bool:
    # Sin huella (tabla sin estadísticas todavía) no se puede afirmar que nada cambió.
    return huella is not None and huella == huella_previa


class Exportador:
    """Gauges con registry propio (instanciable: testeable sin estado global)."""

    def __init__(
        self,
        *,
        intervalo_caro_s: float = INTERVALO_CARO_S,
        edad_maxima_s: float = EDAD_MAXIMA_S,
        reloj: Callable[[], float] = time.monotonic,
        reloj_pared: Callable[[], float] = time.time,
    ) -> None:
        self._intervalo_caro_s = intervalo_caro_s
        self._edad_maxima_s = edad_maxima_s
        self._reloj = reloj
        self._reloj_pared = reloj_pared
        # Huella y momento de la última pasada BUENA de cada nivel. Separadas a
        # propósito: un cambio que llega mientras el nivel caro espera su turno tiene
        # que seguir contando cuando el turno llega, aunque en la pasada anterior la
        # huella ya no se moviera.
        self._huella_backlog: tuple[Any, ...] | None = None
        self._backlog_en: float | None = None
        self._huella_caro: tuple[Any, ...] | None = None
        self._caro_en: float | None = None

        self.registry = CollectorRegistry()
        self._backlog = Gauge(
            "norm_backlog", "Filas en la cola por estado", ["estado"], registry=self.registry
        )
        self._por_ruta = Gauge(
            "norm_archivos_por_ruta",
            "Archivos por decisión del filtro",
            ["ruta_decision"],
            registry=self.registry,
        )
        self._bytes = Gauge(
            "norm_bytes_por_ruta",
            "Bytes por decisión del filtro",
            ["ruta_decision"],
            registry=self.registry,
        )
        self._errores = Gauge(
            "norm_errores_por_motivo",
            "Dead-letter por motivo (top 20)",
            ["motivo"],
            registry=self.registry,
        )
        self._discos_seguros = Gauge(
            "norm_discos_seguros", "Discos con puerta verde", registry=self.registry
        )
        self._discos_pendientes = Gauge(
            "norm_discos_pendientes", "Discos aún NO seguros", registry=self.registry
        )
        self._pausado = Gauge(
            "norm_pausado", "1 si el operador pausó el sistema", registry=self.registry
        )
        # ⚙K16 — sin esto, una réplica detenida es invisible: el nodo de servicio
        # sigue respondiendo búsquedas con datos viejos y nadie se entera hasta que
        # alguien echa algo de menos. -1 = nunca ha replicado.
        self._replica_lag = Gauge(
            "norm_replica_lag_segundos",
            "Segundos desde la última replicación exitosa (-1 si nunca)",
            ["nodo_id", "papel"],
            registry=self.registry,
        )
        # Si una consulta falla, los gauges se quedan con el último valor bueno (mejor
        # que series que desaparecen), pero entonces un número congelado no se distingue
        # de una cola quieta, y `up` sigue a 1 aunque Postgres no conteste. Esto dice
        # hasta cuándo es cierto cada grupo: time() menos esto es su antigüedad.
        self._ultima_ok = Gauge(
            "norm_exportador_ultima_ok_timestamp",
            "Última pasada (epoch) que confirmó cada grupo de gauges: lo recalculó o"
            " comprobó que no pudo cambiar. backlog: norm_backlog; caro: rutas, bytes y"
            " errores; base: discos y pausa",
            ["nivel"],
            registry=self.registry,
        )

    def recolectar_replica(self, config: Config) -> None:
        """Gauge del retraso de replicación. Sólo tiene sentido fuera de `local`."""
        from normalizacion.core import despliegue, replicacion

        t = despliegue.de_config(config)
        if config.despliegue.es_local():
            return
        papel = "emisor" if t.es_archivo_maestro else "receptor"
        lag = replicacion.lag_segundos(config)
        self._replica_lag.labels(
            nodo_id=config.despliegue.nodo_id, papel=papel
        ).set(-1.0 if lag is None else lag)

    def recolectar(self, conn: psycopg.Connection[Any]) -> None:
        """Una pasada. Lo que no lee `archivos`, siempre; lo que sí, solo si cambió.

        - backlog por estado: en cuanto `archivos` cambia. Es el que leen las alertas
          y va por un índice pequeño.
        - rutas, bytes y errores: si cambió Y pasaron `intervalo_caro_s` desde el
          último recorrido. Con la cola quieta, una vez por `edad_maxima_s`.

        Entre pasadas los gauges conservan su valor: Prometheus sigue leyendo series
        completas, solo que el número no se recalcula si no pudo cambiar.

        Lo que no lee `archivos` va primero. Con `archivos` bloqueada (un VACUUM FULL
        con `lock_timeout` en el rol) la consulta del backlog falla; así la pausa, que
        es lo que se mira en esa ventana, sigue publicándose.
        """
        from normalizacion.core import cola

        fila = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE seguro_para_desechar),"
            "       COUNT(*) FILTER (WHERE NOT seguro_para_desechar) FROM discos"
        ).fetchone()
        if fila:
            self._discos_seguros.set(int(fila[0]))
            self._discos_pendientes.set(int(fila[1]))
        self._pausado.set(1 if cola.sistema_pausado(conn) else 0)
        self._confirmar("base")

        ahora = self._reloj()
        # ANTES de agregar: un cambio que entre durante el agregado deja la huella
        # vieja guardada y fuerza otro recálculo. Leída después, se perdería.
        huella = self._leer_huella(conn)

        recalcular = self._toca(huella, self._huella_backlog, self._backlog_en, ahora, espera_s=0.0)
        if recalcular:
            self._recolectar_backlog(conn)
            self._huella_backlog, self._backlog_en = huella, ahora
        if recalcular or _sin_cambios(huella, self._huella_backlog):
            self._confirmar("backlog")

        recalcular = self._toca(
            huella, self._huella_caro, self._caro_en, ahora, espera_s=self._intervalo_caro_s
        )
        if recalcular:
            inicio = time.monotonic()
            self._recolectar_caro(conn)
            self._huella_caro, self._caro_en = huella, ahora
            log.info("exportador_agregado_caro", segundos=round(time.monotonic() - inicio, 2))
        # Mientras espera su turno con la tabla cambiando, el valor caro ya no es cierto:
        # su marca envejece a propósito (hasta `intervalo_caro_s` más una pasada).
        if recalcular or _sin_cambios(huella, self._huella_caro):
            self._confirmar("caro")

    def _confirmar(self, nivel: str) -> None:
        self._ultima_ok.labels(nivel=nivel).set(self._reloj_pared())

    def _leer_huella(self, conn: psycopg.Connection[Any]) -> tuple[Any, ...] | None:
        fila = conn.execute(_SQL_HUELLA).fetchone()
        return tuple(fila) if fila else None

    def _toca(
        self,
        huella: tuple[Any, ...] | None,
        huella_previa: tuple[Any, ...] | None,
        previo_en: float | None,
        ahora: float,
        *,
        espera_s: float,
    ) -> bool:
        if previo_en is None:
            return True
        edad = ahora - previo_en
        if edad >= self._edad_maxima_s:
            return True
        return not _sin_cambios(huella, huella_previa) and edad >= espera_s

    def _recolectar_backlog(self, conn: psycopg.Connection[Any]) -> None:
        # Consultar ANTES de vaciar el gauge: si la consulta falla, Prometheus sigue
        # viendo el último valor bueno en vez de series que desaparecen.
        filas = conn.execute("SELECT estado, COUNT(*) FROM archivos GROUP BY estado").fetchall()
        self._backlog.clear()
        for estado, cuenta in filas:
            self._backlog.labels(estado=estado).set(cuenta)

    def _recolectar_caro(self, conn: psycopg.Connection[Any]) -> None:
        rutas = conn.execute(
            "SELECT COALESCE(ruta_decision, 'SIN_DECIDIR'), COUNT(*), COALESCE(SUM(tamano), 0)"
            " FROM archivos GROUP BY 1"
        ).fetchall()
        errores = conn.execute(
            "SELECT split_part(COALESCE(error_motivo, 'desconocido'), ':', 1), COUNT(*)"
            " FROM archivos WHERE estado = 'ERROR' GROUP BY 1"
            " ORDER BY COUNT(*) DESC LIMIT 20"
        ).fetchall()

        self._por_ruta.clear()
        self._bytes.clear()
        for ruta, cuenta, suma in rutas:
            self._por_ruta.labels(ruta_decision=ruta).set(cuenta)
            self._bytes.labels(ruta_decision=ruta).set(suma)

        self._errores.clear()
        for motivo, cuenta in errores:
            self._errores.labels(motivo=motivo).set(cuenta)

    def texto(self) -> bytes:
        """Exposition format (para tests y debug)."""
        return generate_latest(self.registry)


def correr_exportador(
    config: Config,
    puerto: int,
    intervalo_s: float = 15.0,
    *,
    intervalo_caro_s: float = INTERVALO_CARO_S,
) -> None:
    """Daemon: sirve /metrics en `puerto` y refresca desde Postgres cada intervalo."""
    exportador = Exportador(intervalo_caro_s=intervalo_caro_s)
    start_http_server(puerto, registry=exportador.registry)
    log.info(
        "exportador_arriba",
        puerto=puerto,
        intervalo_s=intervalo_s,
        intervalo_caro_s=intervalo_caro_s,
    )
    while True:
        try:
            with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
                exportador.recolectar(conn)
            exportador.recolectar_replica(config)
        except psycopg.OperationalError as exc:
            log.warning("exportador_sin_postgres", error=str(exc)[:150])
        time.sleep(intervalo_s)
