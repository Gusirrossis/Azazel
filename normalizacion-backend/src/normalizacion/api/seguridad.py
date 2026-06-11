"""Seguridad de la API: API key + rate-limit por llave (límites duros del plan F5)."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class LimitadorPorMinuto:
    """Ventana deslizante en memoria por llave. Suficiente para una instancia;
    multi-instancia (prod) lo sustituye por el límite del gateway/ingress."""

    def __init__(self, max_por_minuto: int) -> None:
        self._max = max_por_minuto
        self._eventos: dict[str, deque[float]] = defaultdict(deque)

    def permitir(self, llave: str) -> bool:
        ahora = time.monotonic()
        eventos = self._eventos[llave]
        while eventos and eventos[0] < ahora - 60.0:
            eventos.popleft()
        if len(eventos) >= self._max:
            return False
        eventos.append(ahora)
        return True


def llave_valida(api_keys: tuple[str, ...], presentada: str | None) -> bool:
    """Llaves vacías = auth deshabilitada (SOLO dev); si hay llaves, exigirlas."""
    if not api_keys:
        return True
    return presentada is not None and presentada in api_keys
