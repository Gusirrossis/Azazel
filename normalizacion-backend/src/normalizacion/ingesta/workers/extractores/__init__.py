"""Registro de extractores plugin (Fase 4): añadir un tipo = soltar un plugin.

Patrón del diseño (PROPUESTA §5.1, inspirado en AutoDetectParser de Tika): un
registro mapea `mime → función extractora`; el worker es un orquestador delgado
que no conoce ningún formato.

Reglas duras (patrón Tika, §2.2 del diseño):
- `extraer` JAMÁS lanza: crash del plugin → flag `extraccion_fallida:*`;
  timeout → flag `extraccion_timeout`. El doc se indexa con su L0 + el flag —
  el blob ya está a salvo y es reprocesable cuando haya un extractor mejor.
- Los límites (⚙K11: timeout, max_chars) producen flags + resultado parcial.

PLAZO COOPERATIVO (⚙K11b). Antes el timeout era solo el `future.result(timeout=…)`
de fuera: cuando vencía se devolvía un resultado VACÍO y se tiraba todo lo que el
plugin llevara reconocido. En un corpus de escaneos eso no era un caso raro — un
PDF de 20 páginas que alcanzaba a OCR-ear 15 se indexaba sin una sola línea.

Ahora el plazo viaja DENTRO del contexto (`ctx.vencido()`) y los plugins con bucle
(páginas, hojas) lo consultan y devuelven lo acumulado con la bandera `*_parcial`.
El timeout de fuera queda como red de seguridad para un plugin que se cuelgue en
una llamada nativa y no pueda mirar el reloj; por eso se le da un margen extra.

Aislamiento: el corte sigue siendo por HILO. Un plugin que respeta el plazo termina
solo y no deja nada colgando; uno que se cuelga dentro de C deja el hilo huérfano
hasta el fin del proceso. El aislamiento por PROCESO exige que la fuente cruce la
frontera del proceso (hoy es un `SpooledTemporaryFile` no serializable) y queda
para el hardening de Fase 7.
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

from normalizacion.core.config import PerillasWorker
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("extractores")

#: Margen del corte duro por encima del plazo cooperativo, como PROPORCIÓN del plazo.
#:
#: Proporcional y no fijo: con un margen fijo de, digamos, 30 s, un timeout de 0.2 s
#: (los tests, o un perfil muy ajustado) tendría un corte duro a los 30.2 s — es decir,
#: ninguno en la práctica, y el límite configurado dejaría de significar nada.
#:
#: Un 25% da holgura para que un plugin cooperativo termine la página que tenía entre
#: manos, y mantiene el tiempo total acotado y predecible: con `extractor_timeout_s`
#: en 300, ningún archivo puede pasar de 375 s.
_MARGEN_CORTE_DURO = 0.25


@dataclass
class ResultadoExtraccion:
    """Lo que un plugin aporta al doc JSON (todo opcional, todo parcial-friendly)."""

    campos: dict[str, Any] = field(default_factory=dict)
    texto: str | None = None
    perfil_calidad: dict[str, Any] | None = None
    flags: list[str] = field(default_factory=list)
    #: Confianza media del OCR en 0-100, o None si no hubo OCR (texto nativo, CSV…).
    #: Sin esto no se puede distinguir un texto bien reconocido de `|||l1 0O`, y por
    #: tanto no se puede ni filtrar la basura ni medir si una mejora mejoró algo.
    confianza: float | None = None


@dataclass(frozen=True)
class ContextoExtraccion:
    """Lo que recibe un plugin: la fuente seekable + límites. Nada de BD ni almacén."""

    fuente: IO[bytes]
    nombre: str
    tipo_real: str
    tamano: int
    perillas: PerillasWorker
    # OCR habilitado (fuente única: filtro.ocr_activo, que el worker propaga). Un plugin
    # de imagen/PDF lo consulta para decidir si intenta OCR. Default False = intacto.
    ocr_activo: bool = False
    #: Instante (reloj monótono) a partir del cual el plugin debe devolver lo que
    #: lleve. None = sin plazo (tests y llamadas directas).
    plazo: float | None = None

    def vencido(self) -> bool:
        """¿Se acabó el tiempo? Los plugins con bucle lo consultan en cada vuelta."""
        return self.plazo is not None and time.monotonic() >= self.plazo

    def restante(self) -> float:
        """Segundos que quedan (inf si no hay plazo). Para decidir si cabe otra página."""
        if self.plazo is None:
            return float("inf")
        return max(0.0, self.plazo - time.monotonic())


Extractor = Callable[[ContextoExtraccion], ResultadoExtraccion]

_REGISTRO: dict[str, Extractor] = {}


def registrar(*mimes: str) -> Callable[[Extractor], Extractor]:
    """Decora un plugin y lo registra para uno o más mimes (o prefijo `image/*`)."""

    def decorador(fn: Extractor) -> Extractor:
        for mime in mimes:
            _REGISTRO[mime] = fn
        return fn

    return decorador


def extractor_para(tipo_real: str | None) -> Extractor | None:
    if tipo_real is None:
        return None
    if tipo_real in _REGISTRO:
        return _REGISTRO[tipo_real]
    return _REGISTRO.get(tipo_real.split("/", 1)[0] + "/*")


def mimes_registrados() -> tuple[str, ...]:
    """Para el `doctor` y los tests de contrato: qué tipos saben extraerse."""
    return tuple(sorted(_REGISTRO))


# Un solo pool para todo el proceso. Antes se creaba un ThreadPoolExecutor NUEVO por
# archivo: con 39 000 archivos eso son 39 000 pools creados y destruidos, cada uno
# levantando y tumbando su hilo. El pool vive lo que vive el worker.
#
# Dos hilos, no uno: cuando un plugin ignora el plazo y hay que abandonarlo, su hilo
# queda ocupado; con un único hilo el SIGUIENTE archivo se quedaría esperando a un
# muerto y el worker entero se congelaría.
_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="extractor"
        )
    return _POOL


def cerrar_pool() -> None:
    """Libera el pool (fin del worker o de los tests). Idempotente."""
    global _POOL
    if _POOL is not None:
        _POOL.shutdown(wait=False, cancel_futures=True)
        _POOL = None


def extraer(
    perillas: PerillasWorker,
    fuente: IO[bytes],
    *,
    tipo_real: str | None,
    nombre: str,
    tamano: int,
    ocr_activo: bool = False,
) -> ResultadoExtraccion:
    """Despacha al plugin con plazo cooperativo + corte duro (⚙K11). NUNCA lanza."""
    plugin = extractor_para(tipo_real)
    if plugin is None:
        return ResultadoExtraccion(flags=["sin_extractor_l1"])

    fuente.seek(0)
    ctx = ContextoExtraccion(
        fuente=fuente,
        nombre=nombre,
        tipo_real=tipo_real or "",
        tamano=tamano,
        perillas=perillas,
        ocr_activo=ocr_activo,
        plazo=time.monotonic() + perillas.extractor_timeout_s,
    )
    futuro = _pool().submit(plugin, ctx)
    try:
        return futuro.result(timeout=perillas.extractor_timeout_s * (1 + _MARGEN_CORTE_DURO))
    except concurrent.futures.TimeoutError:
        # El plugin ignoró el plazo (colgado dentro de una librería nativa). Aquí sí
        # se pierde lo que llevara: no hay forma de sacárselo a un hilo que no vuelve.
        #
        # Y hay que RECICLAR el pool: el hilo abandonado sigue ocupado para siempre,
        # así que con solo dos plugins colgados el pool se quedaba sin hilos libres y
        # TODA extracción posterior del proceso devolvía `extraccion_timeout` — una
        # corrida entera indexada sin texto por dos archivos envenenados.
        log.warning("extractor_timeout_duro", archivo=nombre, tipo=tipo_real)
        cerrar_pool()
        return ResultadoExtraccion(flags=["extraccion_timeout"])
    except Exception as exc:
        log.warning("extractor_fallido", archivo=nombre, tipo=tipo_real, error=str(exc)[:200])
        return ResultadoExtraccion(flags=[f"extraccion_fallida:{type(exc).__name__}"])


# Importar los plugins puebla el registro (al final: evita el ciclo de imports)
from . import documentos, hoja, imagen, tabular, texto  # noqa: E402,F401
