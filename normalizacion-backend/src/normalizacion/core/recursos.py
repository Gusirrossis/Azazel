"""Gobernador de recursos (⚙ K15): cuánto puede trabajar el sistema AHORA mismo.

El problema que resuelve: la ingesta (N procesos worker) y la resolución de
entidades (backfill/envío) corren en la MISMA Mac, a veces junto a OTRO sistema.
Un `núcleos − 2` estático ignora la RAM real y la satura — macOS detecta presión
de memoria, mata al proceso Python más grande y se cae hasta el panel.

La solución es un gobernador que mira la RAM LIBRE en tiempo real y:

  · `presupuesto_workers()` — deriva cuántos procesos worker CABEN en la memoria
    disponible (dejando siempre la reserva de la política), no solo en los núcleos.
  · `presion()` / `bajo_presion()` — ¿la RAM libre ya cruzó el umbral de reserva?
  · `esperar_si_presion()` — pausa cooperativa entre lotes: el worker/entidad
    espera a que la RAM se recupere antes de pedir más trabajo (con tope, para no
    colgar nunca un lote).
  · `cabe_tarea()` — ¿hay memoria para una pasada de entidades dentro de la API?

Todo es best-effort: si psutil falla o el modo es "fijo", se degrada al
comportamiento anterior (núcleos − 2) sin tirar nada.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("recursos")

_MIB = 1024 * 1024


@dataclass(frozen=True)
class Memoria:
    """Foto de la memoria del sistema (MiB) en un instante."""

    total_mb: float
    disponible_mb: float
    reserva_mb: float  # cuánto se quiere mantener SIEMPRE libre (política)
    porcentaje_usado: float

    @property
    def libre_sobre_reserva_mb(self) -> float:
        """RAM utilizable AHORA sin invadir la reserva (>=0)."""
        return max(0.0, self.disponible_mb - self.reserva_mb)

    @property
    def bajo_presion(self) -> bool:
        """True si ya estamos comiendo de la reserva (hay que frenar)."""
        return self.disponible_mb < self.reserva_mb


def _nucleos_default() -> int:
    """Tope por CPU: núcleos − 2 (deja aire para el filtro, la API y las bases)."""
    return max(1, (os.cpu_count() or 4) - 2)


def medir(config: Config) -> Memoria | None:
    """Foto de la memoria. None si psutil no está disponible (degradación elegante)."""
    try:
        import psutil
    except Exception:  # psutil ausente → el caller cae al comportamiento estático
        return None
    try:
        vm = psutil.virtual_memory()
    except Exception as exc:  # lectura fallida → tampoco tumbamos nada
        log.warning("medir_memoria_fallo", error=str(exc)[:200])
        return None
    p = config.recursos
    total_mb = vm.total / _MIB
    reserva_mb = max(p.ram_minima_libre_mb, total_mb * p.fraccion_reserva())
    return Memoria(
        total_mb=total_mb,
        disponible_mb=vm.available / _MIB,
        reserva_mb=reserva_mb,
        porcentaje_usado=float(vm.percent),
    )


def presupuesto_workers(config: Config, solicitado: int | None = None) -> int:
    """Cuántos procesos worker correr AHORA. En modo adaptativo manda la RAM.

    Prioridad:
      · modo "fijo" → lo pedido (front) > perilla NORM_WORKER__PROCESOS > núcleos−2.
      · modo "adaptativo" → min(núcleos−2, RAM_utilizable / mem_por_worker), y lo
        pedido en el front actúa como TOPE (nunca fuerza más de lo que cabe).
    El resultado se acota a [1, workers_max o 64].
    """
    p = config.recursos
    nucleos = _nucleos_default()
    tope = p.workers_max or 64

    if p.modo != "adaptativo":
        n = solicitado or config.worker.procesos or nucleos
        return max(1, min(n, tope))

    mem = medir(config)
    if mem is None:  # sin psutil: adaptativo no puede medir → núcleos−2 (seguro)
        n = solicitado or nucleos
        return max(1, min(n, nucleos, tope))

    por_memoria = int(mem.libre_sobre_reserva_mb // p.mem_por_worker_mb)
    n = min(nucleos, max(1, por_memoria))  # al menos 1 worker, aunque apriete
    if solicitado:  # el front pide N: en adaptativo es un TECHO, no una orden
        n = min(n, solicitado)
    n = max(1, min(n, tope))
    log.info(
        "presupuesto_workers",
        elegidos=n,
        por_memoria=por_memoria,
        nucleos=nucleos,
        disponible_mb=round(mem.disponible_mb),
        reserva_mb=round(mem.reserva_mb),
        solicitado=solicitado,
    )
    return n


def bajo_presion(config: Config) -> bool:
    """¿Conviene frenar AHORA por memoria? False si no se puede medir (no bloquea)."""
    if config.recursos.modo != "adaptativo":
        return False
    mem = medir(config)
    return mem is not None and mem.bajo_presion


def esperar_si_presion(
    config: Config,
    etiqueta: str = "tarea",
    seguir: Callable[[], bool] | None = None,
) -> float:
    """Pausa cooperativa: bloquea mientras haya presión de memoria, hasta el tope.

    Pensada para llamarse ENTRE lotes (antes de reclamar más trabajo). Reducir la
    concurrencia efectiva así —en vez de matar procesos— es simple y robusto: el
    worker no toma más archivos hasta que la RAM se recupere.

    `seguir()` (opcional) permite abortar la espera si el proceso debe terminar
    (p. ej. el sistema se pausó). Devuelve los segundos esperados (0 si no hubo
    presión). Pasado `espera_max_presion_s` se devuelve igual (jamás cuelga).
    """
    p = config.recursos
    if p.modo != "adaptativo" or p.espera_max_presion_s <= 0:
        return 0.0
    inicio = time.monotonic()
    esperado = 0.0
    while bajo_presion(config):
        if seguir is not None and not seguir():
            break
        esperado = time.monotonic() - inicio
        if esperado >= p.espera_max_presion_s:
            log.warning("presion_memoria_persistente", etiqueta=etiqueta, esperado_s=round(esperado))
            break
        if esperado == 0.0 or int(esperado) % 10 == 0:
            log.info("esperando_memoria", etiqueta=etiqueta, esperado_s=round(esperado))
        time.sleep(p.intervalo_muestreo_s)
    return esperado


def cabe_tarea(config: Config, costo_mb: float | None = None) -> bool:
    """¿Hay memoria para una pasada de entidades (backfill/envío) dentro de la API?

    Protege el proceso de la API: si arrancar la resolución dejaría al SO bajo la
    reserva, mejor posponerla (la UI lo reintenta) que tumbar el panel. True si no
    se puede medir o el modo es fijo (no estorba)."""
    if config.recursos.modo != "adaptativo":
        return True
    mem = medir(config)
    if mem is None:
        return True
    costo = costo_mb if costo_mb is not None else config.recursos.mem_entidades_mb
    return mem.libre_sobre_reserva_mb >= costo


def estado(config: Config) -> dict[str, object]:
    """Resumen para la UI/diagnóstico: política activa, memoria y presupuesto."""
    p = config.recursos
    mem = medir(config)
    base: dict[str, object] = {
        "modo": p.modo,
        "politica": p.politica,
        "reserva_pct": round(p.fraccion_reserva() * 100),
        "nucleos_tope": _nucleos_default(),
        "workers_sugeridos": presupuesto_workers(config),
        "psutil": mem is not None,
    }
    if mem is not None:
        base.update(
            total_mb=round(mem.total_mb),
            disponible_mb=round(mem.disponible_mb),
            reserva_mb=round(mem.reserva_mb),
            porcentaje_usado=mem.porcentaje_usado,
            bajo_presion=mem.bajo_presion,
        )
    return base
