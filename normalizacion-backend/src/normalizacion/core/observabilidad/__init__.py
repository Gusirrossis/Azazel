"""Observabilidad: logging estructurado (structlog) y métricas Prometheus.

Regla del plan: cada componente emite sus métricas y logs en el MISMO PR que su
código — la observabilidad no se deja "para el final".
"""

from __future__ import annotations

import logging
import sys

import structlog


def configurar_logging(nivel: int = logging.INFO) -> None:
    """Configura logging estructurado (JSON en producción, legible en terminal)."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if sys.stderr.isatty()
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(nivel),
        cache_logger_on_first_use=True,
    )


def obtener_logger(nombre: str) -> structlog.stdlib.BoundLogger:
    """Logger estructurado con nombre de componente."""
    return structlog.get_logger(nombre)  # type: ignore[no-any-return]
