"""Sink de indexación: el worker entrega documentos; el backend decide qué hacer.

Contrato clave: el worker NO marca una fila como INDEXADO hasta que `drenar()`
confirma que su doc quedó aceptado por el índice (o lo reporta muerto). Así el
estado de la cola nunca le miente al índice.
"""

from __future__ import annotations

from typing import Protocol

from normalizacion.core.modelo import DocumentoArchivo


class Sink(Protocol):
    """Contrato del destino de documentos."""

    def entregar(self, doc: DocumentoArchivo) -> None:
        """Acepta un documento (puede bufferear)."""
        ...

    def drenar(self) -> tuple[list[str], list[tuple[str, str, bool]]]:
        """Vacía el buffer y devuelve (confirmados, muertos).

        Cada muerto es (archivo_id, motivo, es_transitorio): transitorio = la
        dependencia falló (reintentable con backoff); permanente = el doc fue
        rechazado (mapeo inválido…) y debe ir a dead-letter."""
        ...

    def cerrar(self) -> None:
        """Drena lo pendiente. Tras cerrar, todo lo entregado está durable o reportado."""
        ...


class SinkNulo:
    """Acumula documentos sin indexarlos — tests y pipelines sin OpenSearch."""

    def __init__(self) -> None:
        self.documentos: list[DocumentoArchivo] = []
        self._pendientes: list[str] = []

    def entregar(self, doc: DocumentoArchivo) -> None:
        self.documentos.append(doc)
        self._pendientes.append(doc.archivo_id)

    def drenar(self) -> tuple[list[str], list[tuple[str, str, bool]]]:
        confirmados = self._pendientes
        self._pendientes = []
        return confirmados, []

    def cerrar(self) -> None:
        self.drenar()
