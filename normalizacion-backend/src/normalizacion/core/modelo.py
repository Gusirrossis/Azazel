"""Modelo de datos compartido: máquina de estados, identidades y documento JSON.

Invariantes protegidos por tests (bloquean el build si se rompen):
- `archivo_id` es determinista: mismo (ruta, tamaño, mtime) → mismo id, siempre.
- Las transiciones de estado fuera de TRANSICIONES son inválidas.
- Hay DOS hashes y no se confunden: `archivo_id` = sha256(ruta+tamaño+mtime) es la
  clave de TRABAJO (cola, idempotencia del índice); `hash_contenido` = sha256(bytes)
  es la clave del ALMACÉN (dedup, verificación).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Estado(StrEnum):
    """Estados de la fila en la cola durable (plano de control)."""

    PENDIENTE = "PENDIENTE"
    PRECALIFICADO = "PRECALIFICADO"
    COLD = "COLD"
    EN_PROCESO = "EN_PROCESO"
    INDEXADO = "INDEXADO"
    VERIFICADO = "VERIFICADO"
    HECHO = "HECHO"
    ERROR = "ERROR"


class RutaDecision(StrEnum):
    """Decisión del router de precalificación."""

    HOT = "HOT"
    COLD = "COLD"


# Máquina de estados estricta. El router NUNCA borra: COLD es re-evaluable
# (rescore → PENDIENTE para re-puntuar con filtro vN, o promoción directa) y
# ERROR es reprocesable hacia la etapa que le corresponda (norm reprocesar-errores).
TRANSICIONES: dict[Estado, frozenset[Estado]] = {
    Estado.PENDIENTE: frozenset({Estado.PRECALIFICADO, Estado.ERROR}),
    Estado.PRECALIFICADO: frozenset({Estado.EN_PROCESO, Estado.COLD, Estado.ERROR}),
    # COLD→PENDIENTE = rescore con filtro vN; COLD→PRECALIFICADO = promoción directa;
    # COLD→ERROR = no se pudo mover a frío (poison / I/O agotado) → bloquea la puerta
    Estado.COLD: frozenset({Estado.PRECALIFICADO, Estado.PENDIENTE, Estado.ERROR}),
    # error transitorio / lease vencido → vuelve a PRECALIFICADO (conserva el puntaje)
    Estado.EN_PROCESO: frozenset({Estado.INDEXADO, Estado.PRECALIFICADO, Estado.ERROR}),
    Estado.INDEXADO: frozenset({Estado.VERIFICADO, Estado.ERROR}),
    Estado.VERIFICADO: frozenset({Estado.HECHO}),
    Estado.HECHO: frozenset(),
    # reproceso dirigido: a PENDIENTE (falló precalificando), PRECALIFICADO (falló
    # el camino HOT) o COLD (falló el movimiento a frío)
    Estado.ERROR: frozenset({Estado.PRECALIFICADO, Estado.PENDIENTE, Estado.COLD}),
}


def es_transicion_valida(de: Estado, a: Estado) -> bool:
    """True si la máquina de estados permite pasar de `de` a `a`."""
    return a in TRANSICIONES[de]


def sanear_texto(s: str) -> str:
    """Texto SEGURO para BD/JSON/índice y determinista.

    Los nombres de archivo de volúmenes no-UTF8 (un RAID con archivos copiados de
    Windows/Linux: acentos en Latin-1, etc.) llegan a Python con 'surrogateescape'
    (chars \\udcXX). Sin sanear, encodearlos a UTF-8 revienta ('surrogates not
    allowed') y tumba inserciones, serialización JSON e indexado. Aquí se
    normalizan a UTF-8 válido (lo irrepresentable → U+FFFD) y se quitan los NUL
    (Postgres text los rechaza). Idempotente: aplicar dos veces da lo mismo.
    """
    limpio = s.encode("utf-8", "replace").decode("utf-8", "replace")
    return limpio.replace("\x00", "")


def ruta_canonica(ruta: str) -> str:
    """Normaliza la ruta para que el id no dependa del separador del SO."""
    return sanear_texto(ruta.replace("\\", "/").strip())


def calcular_archivo_id(ruta: str, tamano: int, mtime_ns: int) -> str:
    """Clave de TRABAJO, barata (no lee contenido) y determinista.

    Idempotencia de extremo a extremo: re-catalogar no duplica filas y, como el
    índice usa `_id = archivo_id`, reindexar sobrescribe en vez de duplicar.
    Si el archivo cambia (tamaño/mtime distinto) → id nuevo → se reprocesa solo ese.
    """
    base = f"{ruta_canonica(ruta)}|{tamano}|{mtime_ns}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def clave_almacen(hash_contenido: str) -> str:
    """Ruta del blob en el almacén content-addressed: ab/cd/abcd…"""
    return f"{hash_contenido[:2]}/{hash_contenido[2:4]}/{hash_contenido}"


class OrigenContenedor(BaseModel):
    """Path spec serializable (estilo plaso/dfVFS): de qué contenedor salió la fila."""

    contenedor_archivo_id: str
    ruta_interna: str
    profundidad: int = Field(ge=1)


class DocumentoArchivo(BaseModel):
    """El documento JSON que viaja del worker al índice (contrato con OpenSearch)."""

    archivo_id: str
    disco_id: str
    nombre: str
    ruta_original: str
    extension: str | None = None
    tamano: int = Field(ge=0)
    mtime: datetime

    # Precalificación (Fase 1.5)
    tipo_real: str | None = None
    puntaje: int | None = Field(default=None, ge=0, le=100)
    ruta_decision: RutaDecision | None = None
    senales: dict[str, Any] = Field(default_factory=dict)
    motivo: str | None = None
    version_filtro: str | None = None
    origen_contenedor: OrigenContenedor | None = None

    # Persistencia (Fase 2) — la ruta apunta a NUESTRO almacén, no al disco desechable
    hash_contenido: str | None = None
    clave_almacen: str | None = None
    procedencias: list[str] = Field(default_factory=list)

    # Extracción y normalización (Fases 2/4)
    campos_extraidos: dict[str, Any] = Field(default_factory=dict)
    texto_indexable: str | None = None
    perfil_calidad: dict[str, Any] | None = None
    limites_alcanzados: list[str] = Field(default_factory=list)
