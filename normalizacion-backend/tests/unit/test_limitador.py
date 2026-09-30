"""El 429 del límite por minuto dice CUÁNTO esperar.

Sin `Retry-After`, quien federa reintentaba a ciegas a los 2 y 5 s en una ventana de
60 s y seguía en 429: medido por Lilith el 29-09, 2 consultas de un rastro de 122
perdidas.
"""

from __future__ import annotations

import pytest

from normalizacion.api import seguridad
from normalizacion.api.seguridad import LimitadorPorMinuto


class TestEspera:
    def test_con_hueco_no_hay_que_esperar(self) -> None:
        lim = LimitadorPorMinuto(2)
        assert lim.permitir("k")
        assert lim.espera("k") == 0
        assert lim.espera("desconocida") == 0

    def test_lleno_dice_cuando_se_libera_el_primero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloj = [100.0]
        monkeypatch.setattr(seguridad.time, "monotonic", lambda: reloj[0])
        lim = LimitadorPorMinuto(2)
        assert lim.permitir("k")  # t=100
        reloj[0] = 110.0
        assert lim.permitir("k")  # t=110
        reloj[0] = 115.0
        assert not lim.permitir("k")
        assert lim.espera("k") == 45  # el de t=100 sale de la ventana en t=160
        reloj[0] = 160.5
        assert lim.permitir("k")

    def test_nunca_menos_de_un_segundo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reloj = [0.0]
        monkeypatch.setattr(seguridad.time, "monotonic", lambda: reloj[0])
        lim = LimitadorPorMinuto(1)
        assert lim.permitir("k")
        reloj[0] = 59.99
        assert lim.espera("k") == 1

    def test_cada_llave_lleva_su_cuenta(self) -> None:
        lim = LimitadorPorMinuto(1)
        assert lim.permitir("a")
        assert not lim.permitir("a")
        assert lim.espera("b") == 0
