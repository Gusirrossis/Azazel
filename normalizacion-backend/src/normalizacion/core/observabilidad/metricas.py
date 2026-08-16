"""Exportador Prometheus: el estado del sistema, scrapeable (Fase 6 → M6).

La fuente de verdad es la COLA (Postgres): el exportador la consulta cada N
segundos y publica gauges. Prometheus deriva throughput/ETA con delta() sobre
el gauge de HECHO; Grafana pinta los dashboards provisionados en deploy/grafana.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from prometheus_client import CollectorRegistry, Gauge, generate_latest, start_http_server

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("metricas")


class Exportador:
    """Gauges con registry propio (instanciable: testeable sin estado global)."""

    def __init__(self) -> None:
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
        """Una pasada de agregaciones SQL → gauges (costo ~0, no toca archivos)."""
        self._backlog.clear()
        for estado, cuenta in conn.execute(
            "SELECT estado, COUNT(*) FROM archivos GROUP BY estado"
        ).fetchall():
            self._backlog.labels(estado=estado).set(cuenta)

        self._por_ruta.clear()
        self._bytes.clear()
        for ruta, cuenta, suma in conn.execute(
            "SELECT COALESCE(ruta_decision, 'SIN_DECIDIR'), COUNT(*), COALESCE(SUM(tamano), 0)"
            " FROM archivos GROUP BY 1"
        ).fetchall():
            self._por_ruta.labels(ruta_decision=ruta).set(cuenta)
            self._bytes.labels(ruta_decision=ruta).set(suma)

        self._errores.clear()
        for motivo, cuenta in conn.execute(
            "SELECT split_part(COALESCE(error_motivo, 'desconocido'), ':', 1), COUNT(*)"
            " FROM archivos WHERE estado = 'ERROR' GROUP BY 1"
            " ORDER BY COUNT(*) DESC LIMIT 20"
        ).fetchall():
            self._errores.labels(motivo=motivo).set(cuenta)

        fila = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE seguro_para_desechar),"
            "       COUNT(*) FILTER (WHERE NOT seguro_para_desechar) FROM discos"
        ).fetchone()
        if fila:
            self._discos_seguros.set(int(fila[0]))
            self._discos_pendientes.set(int(fila[1]))

        from normalizacion.core import cola

        self._pausado.set(1 if cola.sistema_pausado(conn) else 0)

    def texto(self) -> bytes:
        """Exposition format (para tests y debug)."""
        return generate_latest(self.registry)


def correr_exportador(config: Config, puerto: int, intervalo_s: float = 15.0) -> None:
    """Daemon: sirve /metrics en `puerto` y refresca desde Postgres cada intervalo."""
    exportador = Exportador()
    start_http_server(puerto, registry=exportador.registry)
    log.info("exportador_arriba", puerto=puerto, intervalo_s=intervalo_s)
    while True:
        try:
            with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
                exportador.recolectar(conn)
            exportador.recolectar_replica(config)
        except psycopg.OperationalError as exc:
            log.warning("exportador_sin_postgres", error=str(exc)[:150])
        time.sleep(intervalo_s)
