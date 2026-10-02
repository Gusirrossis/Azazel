"""El snapshot de una luna ESPERA a terminar.

Sin un tiempo propio, opensearch-py cortaba la espera a los 30 s: el snapshot seguía en el
servidor, pero el ciclo de réplica ya exportaba el anterior y la matriz respondía «sin
cambios». Luna kubo, 02-10: 3,5 min de snapshot con la luna indexando y 166 000 documentos
que no llegaron a la matriz con un ciclo que decía «terminado».
"""

from __future__ import annotations

from typing import Any

import pytest

from normalizacion.core import replicacion
from normalizacion.core.config import Config, PerillasDespliegue


def _luna() -> Config:
    despliegue = PerillasDespliegue(
        perfil="hibrido-ingesta",  # type: ignore[arg-type]
        nodo_id="kubo-luna-01",
    )
    return Config(_env_file=None, despliegue=despliegue)  # type: ignore[call-arg]


class _Cliente:
    """Solo lo que `tomar_snapshot` toca: registra cada petición con su `timeout`."""

    def __init__(self) -> None:
        self.peticiones: list[tuple[str, str, dict[str, Any], float | None]] = []
        self.transport = self

    def perform_request(
        self,
        metodo: str,
        ruta: str,
        params: Any = None,
        body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        self.peticiones.append((metodo, ruta, dict(params or {}), timeout))
        return {}


def test_el_snapshot_espera_mas_que_el_timeout_del_cliente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replicacion, "_marcar", lambda *a, **kw: None)
    cliente = _Cliente()

    r = replicacion.tomar_snapshot(_luna(), cliente)

    assert r.ok and r.snapshot and r.snapshot.startswith("kubo-luna-01-")
    snapshots = [p for p in cliente.peticiones if p[0] == "PUT" and p[1].count("/") == 3]
    assert len(snapshots) == 1
    _, _, params, timeout = snapshots[0]
    assert params.get("wait_for_completion") == "true"
    # Los 30 s por omisión de opensearch-py no llegan: hay que esperar de verdad.
    assert timeout is not None and timeout >= 600
