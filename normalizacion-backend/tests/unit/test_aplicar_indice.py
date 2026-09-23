"""`aplicar_indice` debe dejar SIEMPRE designado el índice de escritura de este nodo.

Existe porque restaurar el snapshot de otro nodo dejó TODOS los índices del alias en
`is_write_index: false` y `aplicar_indice` —el arreglo que recomienda `norm doctor`— no
hacía nada: solo designaba el índice al CREARLO, y el de este nodo ya existía. Resultado
medido en la matriz: toda indexación nueva rechazada con «no write index is defined».
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.core.indexador import opensearch as os_mod


class _Indices:
    def __init__(self, existe: bool) -> None:
        self._existe = existe
        self.creados: list[tuple[str, dict[str, Any]]] = []
        self.acciones: list[dict[str, Any]] = []

    def put_index_template(self, **kw: Any) -> None:
        pass

    def exists(self, index: str) -> bool:
        return self._existe

    def create(self, index: str, body: dict[str, Any]) -> None:
        self.creados.append((index, body))

    def update_aliases(self, body: dict[str, Any]) -> None:
        self.acciones.extend(body["actions"])


class _Transporte:
    def perform_request(self, *a: Any, **kw: Any) -> None:
        pass


class _Cliente:
    def __init__(self, existe: bool) -> None:
        self.indices = _Indices(existe)
        self.transport = _Transporte()


def _aplicar(monkeypatch: pytest.MonkeyPatch, existe: bool) -> _Cliente:
    cliente = _Cliente(existe)
    monkeypatch.setattr(os_mod, "crear_cliente", lambda config: cliente)
    monkeypatch.setattr(os_mod, "_ism_disponible", lambda c: False)
    os_mod.aplicar_indice(Config(), Path("deploy"))
    return cliente


def test_indice_existente_recupera_el_is_write_index(monkeypatch: pytest.MonkeyPatch) -> None:
    cliente = _aplicar(monkeypatch, existe=True)
    esperado = os_mod.indice_escritura(Config())
    assert cliente.indices.creados == []
    assert {
        "add": {"index": esperado, "alias": Config().indice_alias, "is_write_index": True}
    } in cliente.indices.acciones


def test_indice_nuevo_se_crea_ya_designado(monkeypatch: pytest.MonkeyPatch) -> None:
    cliente = _aplicar(monkeypatch, existe=False)
    [(nombre, cuerpo)] = cliente.indices.creados
    assert cuerpo["aliases"][Config().indice_alias]["is_write_index"] is True
    assert nombre == os_mod.indice_escritura(Config())
