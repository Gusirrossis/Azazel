"""Contrato de la API (Pydantic → OpenAPI): lo ÚNICO que el front puede pedir.

Seguridad por diseño: el cliente jamás manda DSL — solo estos campos tipados;
el servidor construye la consulta (allowlist implícita, PROPUESTA §9).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SolicitudBusqueda(BaseModel):
    """POST /buscar — texto + filtros + paginación profunda (search_after + PIT)."""

    # Seguridad: campos desconocidos se RECHAZAN (no hay forma de colar DSL)
    model_config = ConfigDict(extra="forbid")

    texto: str | None = Field(
        default=None, max_length=200, description="Búsqueda parcial por nombre (wildcard)"
    )
    tipo_real: str | None = Field(default=None, max_length=120)
    extension: str | None = Field(default=None, max_length=20)
    disco_id: str | None = Field(default=None, max_length=120)
    puntaje_min: int | None = Field(default=None, ge=0, le=100)
    tamano_min: int | None = Field(default=None, ge=0)
    tamano_max: int | None = Field(default=None, ge=0)
    facetas: bool = Field(default=False, description="Incluir conteos por tipo/extensión/disco")
    tamano_pagina: int = Field(default=20, ge=1, description="Se acota al máximo del servidor")
    cursor: list[Any] | None = Field(
        default=None, description="search_after de la página anterior (paginación profunda)"
    )
    abrir_pit: bool = Field(
        default=False, description="Abrir un Point-In-Time (vista estable para paginar)"
    )
    pit_id: str | None = Field(default=None, description="PIT de una búsqueda anterior")


class RespuestaBusqueda(BaseModel):
    total: int
    documentos: list[dict[str, Any]]
    cursor: list[Any] | None = None  # pasa esto como `cursor` para la siguiente página
    facetas: dict[str, dict[str, int]] | None = None
    pit_id: str | None = None


class RespuestaAutocompletar(BaseModel):
    sugerencias: list[str]


class Estadisticas(BaseModel):
    total_documentos: int
    bytes_totales: int
    por_tipo: dict[str, int]
    por_disco: dict[str, int]


class GrupoResumen(BaseModel):
    """Un corte del panel: cuántos archivos y cuántos bytes caen en `clave`."""

    clave: str
    archivos: int
    bytes: int


class ResumenPanel(BaseModel):
    """GET /resumen — agregados de la cola (Postgres), no del índice.

    A diferencia de /estadisticas (solo lo INDEXADO en OpenSearch), aquí se ve TODO
    lo catalogado: el frío incluido. Sirve para saber cuántos datos se dejan de lado.
    """

    total_archivos: int
    bytes_totales: int
    por_estado: list[GrupoResumen]  # PENDIENTE, PRECALIFICADO, COLD, EN_PROCESO, INDEXADO, ERROR
    por_decision: list[GrupoResumen]  # HOT, COLD, SIN_DECIDIR (aún sin precalificar)
    por_tipo: list[GrupoResumen]  # tipos reales con más peso (los ya tipificados)
    generado_en: str


# ------------------------------------------------------------------ pipeline


class SolicitudPipeline(BaseModel):
    """POST /pipeline/ejecutar — indexar una carpeta del sistema (del servidor)."""

    model_config = ConfigDict(extra="forbid")

    ruta: str = Field(max_length=1000, description="Carpeta a procesar (ruta del servidor)")
    disco_id: str | None = Field(default=None, max_length=120)
    # Carpeta de DESTINO elegida en el front (almacén HOT + frío bajo ella).
    # None = destino configurado en .env (MinIO o carpetas por defecto).
    destino: str | None = Field(default=None, max_length=1000)
    # Nº de PROCESOS worker en paralelo. None = automático (núcleos - 2).
    workers: int | None = Field(default=None, ge=1, le=64)


class SolicitudCarpetaNueva(BaseModel):
    """POST /sistema/carpetas — crear una subcarpeta de destino desde el front."""

    model_config = ConfigDict(extra="forbid")

    ruta: str = Field(max_length=1000, description="Carpeta padre (dentro de la raíz de destino)")
    nombre: str = Field(min_length=1, max_length=120)


class FaseEjecutada(BaseModel):
    fase: str
    duracion_s: float
    metricas: dict[str, Any]
    archivos_por_segundo: float | None = None


class Corrida(BaseModel):
    id: int
    disco_id: str
    ruta: str
    estado: str  # EN_CURSO | COMPLETADA | FALLIDA
    fase_actual: str | None
    fases: list[FaseEjecutada]
    seguro_para_desechar: bool | None
    error: str | None
    iniciada_en: Any
    terminada_en: Any | None
    destino: str | None = None  # carpeta elegida en el front; None = destino del .env


class EstadoPipeline(BaseModel):
    en_curso: Corrida | None
    historial: list[Corrida]
    destinos: dict[str, str]
    # Avance EN VIVO de la corrida actual: conteos de la cola por estado
    progreso: dict[str, int] | None = None
    # ¿El front puede ofrecer "elegir carpeta de destino"? (raíz escribible montada)
    destino_eligible: bool = False
    # Workers que usaría el modo automático (núcleos - 2) — el front lo muestra
    workers_auto: int = 1


class RespuestaCarpetas(BaseModel):
    ruta: str
    padre: str | None
    carpetas: list[str]


class ArchivoPreservado(BaseModel):
    """Un contenedor preservado SIN explorar (cifrado/corrupto/formato pendiente)."""

    disco_id: str
    ruta: str
    nombre: str
    tamano: int
    tipo_real: str | None
    motivo: str
    estado: str


class RespuestaPreservados(BaseModel):
    total: int
    por_motivo: dict[str, int]
    archivos: list[ArchivoPreservado]  # los más grandes primero (tope del servidor)
