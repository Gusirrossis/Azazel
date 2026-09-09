"""`cobertura_de_bases`: la respuesta a «¿puedo dejar de recorrer esta base?».

Lo que se prueba aquí no es la consulta —eso lo cubre el índice y una medición— sino
la POLÍTICA: cuándo se puede decir que sí. Equivocarse hacia el «sí» significa que
quien pregunta se salta un barrido y pierde resultados sin que nadie lo note, así que
todas las dudas tienen que resolverse hacia el «no».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from normalizacion.ingesta import pipeline

MTIME = datetime(2026, 5, 21, 10, 18, 0, tzinfo=UTC)


class _Cfg:
    """Lo único que `cobertura_de_bases` le pide a la config."""

    postgres_dsn = "postgresql://prueba/prueba"


CFG = _Cfg()


class _Conn:
    def __init__(self, filas: list[tuple[Any, ...]]) -> None:
        self._filas = filas

    def execute(self, *_a: Any, **_k: Any) -> _Conn:
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._filas

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


def _fila(
    ruta: str = "b.db",
    tamano: int = 1000,
    mtime: datetime = MTIME,
    topada: bool = False,
    completa: bool = True,
) -> tuple[Any, ...]:
    """(ruta, tamano, mtime, actualizado_en, topada, completa) — el orden del SELECT."""
    return (ruta, tamano, mtime, MTIME, topada, completa)


@pytest.fixture
def con_filas(monkeypatch: pytest.MonkeyPatch):
    def _instalar(filas: list[tuple[Any, ...]]) -> None:
        monkeypatch.setattr(pipeline.psycopg, "connect", lambda *_a, **_k: _Conn(filas))

    return _instalar


class TestFailClosed:
    def test_sin_version_no_se_puede_confiar(self, con_filas) -> None:
        """Sin `tamano` y `mtime` no hay forma de saber si el fichero cambió desde que
        se indexó. La base puede estar COMPLETA y aun así la respuesta es no."""
        con_filas([_fila(completa=True)])
        r = pipeline.cobertura_de_bases(CFG, [{"nombre": "b.db"}])
        assert r[0]["completa"] is True
        assert r[0]["version_coincide"] is False

    def test_tamano_distinto_no_coincide(self, con_filas) -> None:
        con_filas([_fila(tamano=1000)])
        r = pipeline.cobertura_de_bases(
            CFG,
            [{"nombre": "b.db", "tamano": 2000, "mtime": MTIME}],
        )
        assert r[0]["version_coincide"] is False

    def test_mtime_distinto_no_coincide(self, con_filas) -> None:
        con_filas([_fila(mtime=MTIME)])
        otro = datetime(2026, 6, 1, tzinfo=UTC)
        r = pipeline.cobertura_de_bases(
            CFG,
            [{"nombre": "b.db", "tamano": 1000, "mtime": otro}],
        )
        assert r[0]["version_coincide"] is False

    def test_base_desconocida_se_responde_igual(self, con_filas) -> None:
        """Hay que distinguir «no la tengo» de «no te contesté por ella»."""
        con_filas([])
        r = pipeline.cobertura_de_bases(CFG, [{"nombre": "fantasma.db"}])
        assert len(r) == 1
        assert r[0]["nombre"] == "fantasma.db"
        assert r[0]["indexada"] is False
        assert r[0]["completa"] is False


class TestCuandoSiSePuede:
    def test_completa_al_dia_y_sin_topar(self, con_filas) -> None:
        con_filas([_fila(completa=True, topada=False)])
        r = pipeline.cobertura_de_bases(
            CFG,
            [{"nombre": "b.db", "tamano": 1000, "mtime": MTIME}],
        )
        assert (r[0]["completa"], r[0]["topada"], r[0]["version_coincide"]) == (True, False, True)

    def test_mtime_en_iso_tambien_vale(self, con_filas) -> None:
        """Quien federa manda JSON: el mtime llega como texto ISO, no como datetime."""
        con_filas([_fila()])
        r = pipeline.cobertura_de_bases(
            CFG,
            [{"nombre": "b.db", "tamano": 1000, "mtime": "2026-05-21T10:18:00Z"}],
        )
        assert r[0]["version_coincide"] is True

    def test_desfase_de_un_segundo_se_tolera(self, con_filas) -> None:
        """El mtime cruza sistemas de ficheros y serializaciones. Un desfase de menos
        de 2 s no significa que el contenido cambiara, y errar aquí sólo provoca un
        barrido de más — nunca un resultado de menos."""
        con_filas([_fila(mtime=MTIME)])
        casi = datetime(2026, 5, 21, 10, 18, 1, tzinfo=UTC)
        r = pipeline.cobertura_de_bases(
            CFG,
            [{"nombre": "b.db", "tamano": 1000, "mtime": casi}],
        )
        assert r[0]["version_coincide"] is True


class TestTopada:
    def test_topada_se_propaga(self, con_filas) -> None:
        """`topada` significa que la copia es PARCIAL por diseño: aunque todas las
        entradas enumeradas estén en HECHO, la cola de la base nunca se enumeró."""
        con_filas([_fila(completa=True, topada=True)])
        r = pipeline.cobertura_de_bases(
            CFG,
            [{"nombre": "b.db", "tamano": 1000, "mtime": MTIME}],
        )
        assert r[0]["completa"] is True
        assert r[0]["topada"] is True, "completa + topada = NO se puede saltar el barrido"


def test_sin_bases_no_toca_la_base_de_datos(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explota(*_a: Any, **_k: Any):
        raise AssertionError("no debería conectarse sin nada que consultar")

    monkeypatch.setattr(pipeline.psycopg, "connect", _explota)
    assert pipeline.cobertura_de_bases(CFG, []) == []
