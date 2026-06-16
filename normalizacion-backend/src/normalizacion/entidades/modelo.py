"""Modelo de la ENTIDAD canónica y su identidad idempotente.

`entidad_id = sha256(tipo:ancla_tipo:ancla_valor)`: la misma persona resuelta por
el mismo ancla fuerte (p. ej. la misma CURP) produce SIEMPRE el mismo id, sin
importar de cuántas fuentes venga. Reprocesar no duplica — el mismo invariante
que `archivo_id` en la Fase 1.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AnclaTipo(StrEnum):
    """Identificadores que resuelven una persona sin ambigüedad (orden de fuerza)."""

    CURP = "curp"
    RFC = "rfc"
    EMAIL = "email"
    TELEFONO = "telefono"


# Fuerza del ancla: la CURP manda; a falta de ella, RFC, luego contacto.
ORDEN_ANCLAS: tuple[AnclaTipo, ...] = (
    AnclaTipo.CURP, AnclaTipo.RFC, AnclaTipo.EMAIL, AnclaTipo.TELEFONO,
)


class Procedencia(BaseModel):
    """De dónde salió un dato: qué archivo/fila y con qué valor crudo. Heredado de
    Azazel para trazabilidad completa (cada dato sabe su origen)."""

    archivo_id: str | None = None
    disco_id: str | None = None
    ruta: str | None = None
    campo_origen: str | None = None
    valor_crudo: str | None = None


class Entidad(BaseModel):
    """Una persona canónica resuelta a partir de una o más fuentes."""

    entidad_id: str
    tipo: str = "persona"
    ancla_tipo: AnclaTipo
    ancla_valor: str
    campos: dict[str, Any] = Field(default_factory=dict)  # forma de la receta (Fz1)
    confianza: float = Field(default=1.0, ge=0.0, le=1.0)
    version_receta: str
    version_resolucion: str
    activo: bool = True
    procedencias: list[Procedencia] = Field(default_factory=list)


def calcular_entidad_id(tipo: str, ancla_tipo: AnclaTipo, ancla_valor: str) -> str:
    """Identidad determinista por ancla fuerte (idempotencia de la resolución)."""
    if not ancla_valor or not ancla_valor.strip():
        raise ValueError("ancla_valor no puede ser vacío para calcular entidad_id")
    base = f"{tipo}:{ancla_tipo.value}:{ancla_valor.strip().upper()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()
