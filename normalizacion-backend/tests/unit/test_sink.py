"""Tests del SinkOpenSearch con cliente falso: triggers, retry/backoff y dead-letter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from normalizacion.core.config import Config, PerillasIndexador
from normalizacion.core.indexador.opensearch import SinkOpenSearch
from normalizacion.core.modelo import DocumentoArchivo


def _doc(n: int) -> DocumentoArchivo:
    return DocumentoArchivo(
        archivo_id=f"id-{n}",
        disco_id="d1",
        nombre=f"a{n}.csv",
        ruta_original=f"a{n}.csv",
        tamano=10,
        mtime=datetime(2023, 1, 1, tzinfo=UTC),
    )


class ClienteFalso:
    """Simula OpenSearch: cuenta llamadas, puede fallar N veces o rechazar items."""

    def __init__(self, fallos_transporte: int = 0, ids_rechazados: set[str] | None = None):
        self.llamadas: list[str] = []
        self._fallos = fallos_transporte
        self._rechazados = ids_rechazados or set()

    def bulk(self, body: str) -> dict[str, Any]:
        self.llamadas.append(body)
        if self._fallos > 0:
            self._fallos -= 1
            raise ConnectionError("simulado: OpenSearch caído")
        items = []
        hay_error = False
        for linea in body.splitlines():
            if '"index"' in linea and "_id" in linea:
                import json

                aid = json.loads(linea)["index"]["_id"]
                if aid in self._rechazados:
                    items.append({"index": {"_id": aid, "error": {"type": "mapper_parsing"}}})
                    hay_error = True
                else:
                    items.append({"index": {"_id": aid, "status": 201}})
        return {"errors": hay_error, "items": items}


def _config(**indexador: Any) -> Config:
    defaults: dict[str, Any] = {"backoff_base_s": 0.0}
    defaults.update(indexador)
    return Config(_env_file=None, indexador=PerillasIndexador(**defaults))


class TestTriggers:
    def test_flush_por_numero_de_acciones(self) -> None:
        cliente = ClienteFalso()
        sink = SinkOpenSearch(_config(flush_acciones=2), cliente=cliente)
        sink.entregar(_doc(1))
        assert cliente.llamadas == []  # aún no toca
        sink.entregar(_doc(2))
        assert len(cliente.llamadas) == 1  # trigger por acciones

    def test_flush_por_bytes(self) -> None:
        cliente = ClienteFalso()
        sink = SinkOpenSearch(_config(flush_acciones=10_000, flush_bytes=100), cliente=cliente)
        sink.entregar(_doc(1))  # un doc ya pesa > 100 bytes
        assert len(cliente.llamadas) == 1

    def test_drenar_confirma_todo(self) -> None:
        cliente = ClienteFalso()
        sink = SinkOpenSearch(_config(), cliente=cliente)
        sink.entregar(_doc(1))
        sink.entregar(_doc(2))
        confirmados, muertos = sink.drenar()
        assert sorted(confirmados) == ["id-1", "id-2"]
        assert muertos == []
        assert sink.total_indexados == 2


class TestRetryYDeadLetter:
    def test_reintenta_y_se_recupera(self) -> None:
        """429/transporte caído → backoff y reintento (patrón fscrawler ⚙K14)."""
        cliente = ClienteFalso(fallos_transporte=2)
        sink = SinkOpenSearch(_config(reintentos_max=3), cliente=cliente)
        sink.entregar(_doc(1))
        confirmados, muertos = sink.drenar()
        assert confirmados == ["id-1"] and muertos == []
        assert len(cliente.llamadas) == 3  # 2 fallos + 1 éxito

    def test_reintentos_agotados_son_muertos_TRANSITORIOS(self) -> None:
        """OpenSearch caído no es culpa del doc: muerto con flag transitorio=True
        (el worker lo devolverá a la cola con backoff, no a dead-letter)."""
        cliente = ClienteFalso(fallos_transporte=99)
        sink = SinkOpenSearch(_config(reintentos_max=2), cliente=cliente)
        sink.entregar(_doc(1))
        confirmados, muertos = sink.drenar()
        assert confirmados == []
        assert len(muertos) == 1
        assert muertos[0][0] == "id-1" and "transporte" in muertos[0][1]
        assert muertos[0][2] is True  # transitorio

    def test_item_rechazado_es_muerto_PERMANENTE(self) -> None:
        """Un doc con error de mapeo muere SOLO (permanente); el resto se confirma."""
        cliente = ClienteFalso(ids_rechazados={"id-2"})
        sink = SinkOpenSearch(_config(), cliente=cliente)
        for n in (1, 2, 3):
            sink.entregar(_doc(n))
        confirmados, muertos = sink.drenar()
        assert sorted(confirmados) == ["id-1", "id-3"]
        assert muertos[0][0] == "id-2"
        assert muertos[0][2] is False  # permanente → dead-letter
