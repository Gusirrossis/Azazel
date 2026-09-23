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
    def __init__(self, existe: bool, alias: dict[str, bool] | None = None) -> None:
        self._existe = existe
        self._alias = alias or {}  # índice → is_write_index
        self.creados: list[tuple[str, dict[str, Any]]] = []
        self.acciones: list[dict[str, Any]] = []

    def get_alias(self, name: str) -> dict[str, Any]:
        return {i: {"aliases": {name: {"is_write_index": w}}} for i, w in self._alias.items()}

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
    def __init__(self, existe: bool, alias: dict[str, bool] | None = None) -> None:
        self.indices = _Indices(existe, alias)
        self.transport = _Transporte()


def _aplicar(
    monkeypatch: pytest.MonkeyPatch, existe: bool, alias: dict[str, bool] | None = None
) -> _Cliente:
    cliente = _Cliente(existe, alias)
    monkeypatch.setattr(os_mod, "crear_cliente", lambda config: cliente)
    monkeypatch.setattr(os_mod, "_ism_disponible", lambda c: False)
    os_mod.aplicar_indice(Config(), Path("deploy"))
    return cliente


def _add(indice: str) -> dict[str, Any]:
    return {"add": {"index": indice, "alias": Config().indice_alias, "is_write_index": True}}


def test_indice_existente_recupera_el_is_write_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """El caso de la matriz: tras restaurar snapshots, NINGÚN índice de escritura."""
    propio = os_mod.indice_escritura(Config())
    cliente = _aplicar(monkeypatch, True, {propio: False, "archivos-otro-01-000001-r": False})
    assert cliente.indices.creados == []
    assert cliente.indices.acciones == [_add(propio)]


def test_no_devuelve_la_escritura_a_un_indice_ya_rotado(monkeypatch: pytest.MonkeyPatch) -> None:
    """El caso de Lilith: la ISM rotó a -000003. aplicar_indice corre en cada arranque y
    en cada corrida: forzar el inicial devolvería la escritura a un índice viejo."""
    base = os_mod.indice_escritura(Config()).rsplit("-", 1)[0]
    cliente = _aplicar(
        monkeypatch, True, {f"{base}-000001": False, f"{base}-000003": True}
    )
    assert cliente.indices.acciones == []


def test_sin_escritura_elige_el_propio_mas_reciente(monkeypatch: pytest.MonkeyPatch) -> None:
    base = os_mod.indice_escritura(Config()).rsplit("-", 1)[0]
    cliente = _aplicar(
        monkeypatch, True,
        {f"{base}-000001": False, f"{base}-000003": False, f"{base}-000002": False,
         "archivos-otro-01-000009-r": False},
    )
    assert cliente.indices.acciones == [_add(f"{base}-000003")]


def test_indice_nuevo_se_crea_ya_designado(monkeypatch: pytest.MonkeyPatch) -> None:
    cliente = _aplicar(monkeypatch, existe=False)
    [(nombre, cuerpo)] = cliente.indices.creados
    assert cuerpo["aliases"][Config().indice_alias]["is_write_index"] is True
    assert nombre == os_mod.indice_escritura(Config())
