"""El filtro (precalificación) en N procesos.

En un disco de millones de archivos el filtro, en un solo hilo, era el cuello de botella
(luna kubo, 10-2026: ~14 000 archivos/h con 15,8 M pendientes). Reparte como los
workers: cada proceso con su worker_id, la cola con SKIP LOCKED + lease.
"""

from __future__ import annotations

import queue
from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.ingesta import pipeline
from normalizacion.ingesta.precalificacion import precalificador
from normalizacion.ingesta.precalificacion.precalificador import ResumenPrecalificacion


def _config() -> Config:
    return Config(_env_file=None)  # type: ignore[call-arg]


class _ProcesoEnSitio:
    """Proceso de mentira: corre su target al arrancar, en este mismo proceso."""

    def __init__(self, target: Any, args: tuple[Any, ...], daemon: bool = False) -> None:
        self._target, self._args = target, args
        self.exitcode: int | None = None

    def start(self) -> None:
        self._target(*self._args)
        self.exitcode = 0

    def join(self) -> None:
        return None


class _ProcesoQueMuere(_ProcesoEnSitio):
    def start(self) -> None:
        self.exitcode = 1  # murió sin reportar nada


class _Contexto:
    def __init__(self, proceso: type[_ProcesoEnSitio] = _ProcesoEnSitio) -> None:
        self.Process = proceso
        self.Queue = queue.Queue


def test_un_proceso_corre_en_el_sitio(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[dict[str, Any]] = []

    def falso(config: Config, **kw: Any) -> ResumenPrecalificacion:
        llamadas.append(kw)
        return ResumenPrecalificacion(5, 3, 2, 0, 1, 0)

    monkeypatch.setattr(precalificador, "precalificar_pendientes", falso)
    r = pipeline.precalificar_en_paralelo(_config(), 1)
    assert r == ResumenPrecalificacion(5, 3, 2, 0, 1, 0)
    assert llamadas == [{}], "con un proceso, exactamente como antes (worker_id por omisión)"


def test_varios_procesos_suman_y_cada_uno_con_su_id(monkeypatch: pytest.MonkeyPatch) -> None:
    ids: list[str] = []

    def falso(config: Config, worker_id: str = "") -> ResumenPrecalificacion:
        ids.append(worker_id)
        return ResumenPrecalificacion(10, 6, 4, 1, 2, 1)

    monkeypatch.setattr(precalificador, "precalificar_pendientes", falso)
    r = pipeline.precalificar_en_paralelo(_config(), 3, contexto=_Contexto())
    assert r == ResumenPrecalificacion(30, 18, 12, 3, 6, 3)
    assert len(ids) == 3 and len(set(ids)) == 3, f"worker_id repetido: {ids}"
    assert all(i.startswith(f"precalifica-{n}@") for n, i in zip((1, 2, 3), ids, strict=True))


def test_un_proceso_muerto_falla_a_la_vista(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        precalificador,
        "precalificar_pendientes",
        lambda *a, **kw: pytest.fail("un proceso que muere no corre el filtro"),
    )
    with pytest.raises(RuntimeError, match="murieron"):
        pipeline.precalificar_en_paralelo(_config(), 2, contexto=_Contexto(_ProcesoQueMuere))
