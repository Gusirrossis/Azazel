"""Registro de extractores plugin (Fase 4): añadir un tipo = soltar un plugin.

Patrón del diseño (PROPUESTA §5.1, inspirado en AutoDetectParser de Tika): un
registro mapea `mime → función extractora`; el worker es un orquestador delgado
que no conoce ningún formato.

Reglas duras (patrón Tika, §2.2 del diseño):
- `extraer` JAMÁS lanza: crash del plugin → flag `extraccion_fallida:*`;
  timeout → flag `extraccion_timeout`. El doc se indexa con su L0 + el flag —
  el blob ya está a salvo y es reprocesable cuando haya un extractor mejor.
- Los límites (⚙K11: timeout, max_chars) producen flags + resultado parcial.

Nota de aislamiento: el timeout v1 es por hilo (un plugin colgado se abandona;
el hilo huérfano queda hasta el fin del proceso). El aislamiento por PROCESO
con límites de recursos llega con el hardening de Fase 7.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

from normalizacion.core.config import PerillasWorker
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("extractores")


@dataclass
class ResultadoExtraccion:
    """Lo que un plugin aporta al doc JSON (todo opcional, todo parcial-friendly)."""

    campos: dict[str, Any] = field(default_factory=dict)
    texto: str | None = None
    perfil_calidad: dict[str, Any] | None = None
    flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ContextoExtraccion:
    """Lo que recibe un plugin: la fuente seekable + límites. Nada de BD ni almacén."""

    fuente: IO[bytes]
    nombre: str
    tipo_real: str
    tamano: int
    perillas: PerillasWorker


Extractor = Callable[[ContextoExtraccion], ResultadoExtraccion]

_REGISTRO: dict[str, Extractor] = {}


def registrar(*mimes: str) -> Callable[[Extractor], Extractor]:
    """Decora un plugin y lo registra para uno o más mimes (o prefijo `image/*`)."""

    def decorador(fn: Extractor) -> Extractor:
        for mime in mimes:
            _REGISTRO[mime] = fn
        return fn

    return decorador


def extractor_para(tipo_real: str | None) -> Extractor | None:
    if tipo_real is None:
        return None
    if tipo_real in _REGISTRO:
        return _REGISTRO[tipo_real]
    return _REGISTRO.get(tipo_real.split("/", 1)[0] + "/*")


def extraer(
    perillas: PerillasWorker,
    fuente: IO[bytes],
    *,
    tipo_real: str | None,
    nombre: str,
    tamano: int,
) -> ResultadoExtraccion:
    """Despacha al plugin con timeout (⚙K11). NUNCA lanza — flags en su lugar."""
    plugin = extractor_para(tipo_real)
    if plugin is None:
        return ResultadoExtraccion(flags=["sin_extractor_l1"])

    fuente.seek(0)
    ctx = ContextoExtraccion(
        fuente=fuente,
        nombre=nombre,
        tipo_real=tipo_real or "",
        tamano=tamano,
        perillas=perillas,
    )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        futuro = pool.submit(plugin, ctx)
        try:
            return futuro.result(timeout=perillas.extractor_timeout_s)
        except concurrent.futures.TimeoutError:
            log.warning("extractor_timeout", archivo=nombre, tipo=tipo_real)
            return ResultadoExtraccion(flags=["extraccion_timeout"])
        except Exception as exc:
            log.warning("extractor_fallido", archivo=nombre, tipo=tipo_real, error=str(exc)[:200])
            return ResultadoExtraccion(flags=[f"extraccion_fallida:{type(exc).__name__}"])
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# Importar los plugins puebla el registro (al final: evita el ciclo de imports)
from . import documentos, hoja, imagen, tabular, texto  # noqa: E402,F401
