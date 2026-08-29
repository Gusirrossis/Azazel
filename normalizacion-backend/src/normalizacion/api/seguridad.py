"""Seguridad de la API: rate-limit general y freno de fuerza bruta en el login."""

from __future__ import annotations

import time
from collections import OrderedDict, deque


class LimitadorPorMinuto:
    """Ventana deslizante en memoria por llave. Suficiente para una instancia;
    multi-instancia (prod) lo sustituye por el límite del gateway/ingress."""

    def __init__(self, max_por_minuto: int) -> None:
        self._max = max_por_minuto
        self._eventos: dict[str, deque[float]] = {}

    def permitir(self, llave: str) -> bool:
        ahora = time.monotonic()
        eventos = self._eventos.get(llave)
        if eventos is None:
            eventos = deque()
            self._eventos[llave] = eventos
        while eventos and eventos[0] < ahora - 60.0:
            eventos.popleft()
        if len(eventos) >= self._max:
            return False
        eventos.append(ahora)
        return True


class FrenoDeIntentos:
    """Bloqueo temporal tras N fallos seguidos, para el login.

    El limitador de arriba cuenta TODAS las peticiones por minuto; este cuenta solo
    los FALLOS y bloquea durante un rato. Son cosas distintas: 120 req/min es un
    caudal normal para el panel y, sin embargo, 120 contraseñas por minuto contra
    una cuenta es un ataque de diccionario en marcha.

    Se lleva por usuario Y por IP a la vez. Solo por usuario, quien tenga una
    botnet prueba una contraseña en mil cuentas sin gastar el cupo de ninguna; solo
    por IP, cien personas tras el mismo NAT se bloquean entre ellas.

    En memoria y por proceso, igual que `LimitadorPorMinuto`: se pierde al
    reiniciar, y con varias réplicas cada una lleva su cuenta. Es un freno, no una
    garantía — la garantía es la política de contraseñas y argon2.
    """

    #: Tope de claves vivas. El login es ANÓNIMO: cualquiera puede inventarse un
    #: usuario y una IP distintos en cada intento, y sin tope cada uno dejaba una
    #: entrada que nunca se borraba (`_vigente` solo purga las que llegaron a
    #: bloquearse). 100.000 intentos = 200.000 entradas retenidas para siempre.
    _MAX_CLAVES = 20_000

    def __init__(self, max_intentos: int = 5, bloqueo_seg: float = 300.0) -> None:
        self._max = max_intentos
        self._bloqueo = bloqueo_seg
        # OrderedDict y no defaultdict: hace falta poder desalojar la más vieja.
        self._fallos: OrderedDict[str, deque[float]] = OrderedDict()
        self._hasta: dict[str, float] = {}

    def _podar(self) -> None:
        """Desaloja las entradas más antiguas que NO estén bloqueadas.

        Nunca desaloja un bloqueo vigente: eso sería regalarle al atacante la forma
        de limpiarse el castigo llenando la tabla de claves inventadas.
        """
        while len(self._fallos) > self._MAX_CLAVES:
            llave, _ = self._fallos.popitem(last=False)
            if llave in self._hasta and self._vigente(llave) > 0:
                self._fallos[llave] = deque()  # sigue bloqueada: se reinserta al final
                self._fallos.move_to_end(llave)
                break

    def _vigente(self, llave: str) -> float:
        """Segundos que le quedan al bloqueo de `llave` (0 si no está bloqueada)."""
        fin = self._hasta.get(llave)
        if fin is None:
            return 0.0
        restante = fin - time.monotonic()
        if restante <= 0:
            del self._hasta[llave]
            self._fallos.pop(llave, None)
            return 0.0
        return restante

    def bloqueado(self, *llaves: str) -> float:
        """Mayor bloqueo vigente entre las llaves dadas, en segundos. 0 = puede pasar."""
        return max((self._vigente(llave) for llave in llaves), default=0.0)

    def registrar_fallo(self, *llaves: str) -> None:
        ahora = time.monotonic()
        for llave in llaves:
            fallos = self._fallos.get(llave)
            if fallos is None:
                fallos = deque()
                self._fallos[llave] = fallos
            # Los fallos viejos no cuentan: dos errores de dedo con horas de por
            # medio no son un ataque y no deben acumularse hasta bloquear.
            while fallos and fallos[0] < ahora - self._bloqueo:
                fallos.popleft()
            fallos.append(ahora)
            self._fallos.move_to_end(llave)  # la más reciente, la última en desalojarse
            if len(fallos) >= self._max:
                self._hasta[llave] = ahora + self._bloqueo
        self._podar()

    def registrar_exito(self, *llaves: str) -> None:
        """Un acierto limpia el historial: el bloqueo persigue rachas de fallos."""
        for llave in llaves:
            self._fallos.pop(llave, None)
            self._hasta.pop(llave, None)
