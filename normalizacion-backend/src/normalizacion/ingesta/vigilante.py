"""Vigilante de carpeta: ingesta AUTOMÁTICA para el nodo `online`.

Sondea `carpeta_raiz/*` —una subcarpeta = una fuente = un `disco_id`— y, cuando una
fuente queda QUIETA (nadie escribe desde hace `quiescencia_s`, o dejó su sentinela),
lanza su corrida completa. Dos reglas gobiernan el reparto:

  · SERIALIZA. Una corrida a la vez (la regla del lock por tabla `corridas`). El
    vigilante corre cada `ejecutar_corrida` de forma síncrona, así que nunca lanza
    dos; si además hay un disparo manual por la API, `iniciar_corrida` lo corta con
    un RuntimeError que aquí se captura y se reintenta al ciclo siguiente.
  · ROUND-ROBIN. Avanza un cursor entre las fuentes para que una grande y siempre
    activa no mate de hambre a las demás.

NO observa el FS con inotify: sondea. inotify es poco fiable sobre los bind-mounts
de Docker, y el pipeline ya es idempotente/incremental —re-ver una fuente sin
cambios es barato (una pasada de `stat`) y re-lanzarla nunca duplica (dedup por
`archivo_id`)—. La huella `Firma` evita incluso esa pasada cuando nada cambió.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("vigilante")

_NS_POR_S = 1_000_000_000


@dataclass(frozen=True)
class Firma:
    """Huella barata de una fuente. Si no cambia entre sondeos, no hay trabajo nuevo
    y ni siquiera se vuelve a catalogar."""

    n_archivos: int
    bytes_totales: int
    mtime_max_ns: int

    @property
    def vacia(self) -> bool:
        return self.n_archivos == 0


def firmar(raiz: Path, *, ignorar: str | None = None) -> Firma:
    """Recorre `raiz` con `stat` (sin leer contenido) y resume nº de archivos, bytes
    y el mtime más reciente. `ignorar` excluye un nombre de archivo del cómputo (la
    sentinela: dejarla caer no debe contar como 'contenido nuevo').

    `os.walk(followlinks=False)`: no seguimos symlinks de directorio para no colgarnos
    en un ciclo ni recorrer fuera del árbol de la fuente."""
    n = 0
    total = 0
    mmax = 0
    for dirpath, _dirnames, filenames in os.walk(raiz, followlinks=False):
        for nombre in filenames:
            if ignorar is not None and nombre == ignorar:
                continue
            try:
                st = os.stat(os.path.join(dirpath, nombre))
            except OSError:
                continue  # borrado a media pasada / permiso: no cuenta
            n += 1
            total += st.st_size
            if st.st_mtime_ns > mmax:
                mmax = st.st_mtime_ns
    return Firma(n, total, mmax)


def lista_para_procesar(
    firma: Firma,
    *,
    ahora_ns: int,
    quiescencia_ns: int,
    sentinela_presente: bool,
    sentinela_requerida: bool,
) -> bool:
    """¿Se puede ingerir ya esta fuente? Función pura: toda la política de disparo en
    un sitio, sin tocar reloj ni FS.

      · Vacía → no.
      · Con sentinela requerida → basta con que la sentinela esté (el origen declara
        'terminé de copiar'). Es lo más robusto contra lotes a medio copiar.
      · Sin sentinela → QUIESCENCIA: nadie ha escrito en los últimos `quiescencia_s`.
        Evita catalogar un lote mientras el otro VPS aún lo está volcando."""
    if firma.vacia:
        return False
    if sentinela_requerida:
        return sentinela_presente
    return (ahora_ns - firma.mtime_max_ns) >= quiescencia_ns


def fuentes(carpeta_raiz: Path) -> list[Path]:
    """Subcarpetas inmediatas de la raíz, ordenadas. Cada una es una fuente/disco.
    Los archivos sueltos en la raíz se ignoran a propósito: sin subcarpeta no hay
    `disco_id` de fuente, y mezclarlos rompería el modelo 'una fuente = un disco'."""
    try:
        hijos = sorted(p for p in carpeta_raiz.iterdir() if p.is_dir())
    except FileNotFoundError:
        return []
    return hijos


@dataclass
class _EstadoFuente:
    """Memoria por fuente entre ciclos."""

    ultima_procesada: Firma | None = None  # firma que ya llevamos hasta 'COMPLETADA'
    cooldown_hasta_ns: int = 0  # tras un fallo, no reintentar hasta aquí


class Vigilante:
    """El bucle, con su estado. Separado de `correr_vigilante` para poder inyectar
    reloj/parada/reclamador en los tests."""

    def __init__(
        self,
        config: Config,
        carpeta_raiz: Path,
        *,
        intervalo_s: float = 30.0,
        quiescencia_s: float = 60.0,
        sentinela: str | None = None,
        cooldown_fallo_s: float = 300.0,
        workers: int | None = None,
        reclamar: Callable[..., bool] | None = None,
        # Reloj de PARED (no monotónico): la quiescencia compara contra `st_mtime_ns`
        # de los archivos, que vive en el dominio de time.time_ns, no de monotonic.
        reloj_ns: Callable[[], int] = time.time_ns,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.raiz = carpeta_raiz
        self.intervalo_s = intervalo_s
        self.quiescencia_ns = int(quiescencia_s * _NS_POR_S)
        self.sentinela = sentinela
        self.cooldown_fallo_ns = int(cooldown_fallo_s * _NS_POR_S)
        self.workers = workers
        self._reclamar = reclamar
        self._reloj_ns = reloj_ns
        self._dormir = dormir
        self._estado: dict[str, _EstadoFuente] = {}
        self._cursor = 0  # para el round-robin

    # -- un ciclo: elige a lo sumo UNA fuente y la procesa -----------------------

    def un_ciclo(self) -> str | None:
        """Sondea las fuentes, procesa a lo sumo UNA (la primera lista, en orden
        round-robin) y devuelve su nombre, o None si no había nada que hacer (el
        bucle duerme). Aislar 'un ciclo' hace el bucle trivial de testear."""
        subdirs = fuentes(self.raiz)
        if not subdirs:
            return None
        ahora = self._reloj_ns()
        n = len(subdirs)
        for i in range(n):
            idx = (self._cursor + i) % n
            fuente = subdirs[idx]
            nombre = fuente.name
            est = self._estado.setdefault(nombre, _EstadoFuente())
            if ahora < est.cooldown_hasta_ns:
                continue  # backoff tras un fallo reciente
            firma = firmar(fuente, ignorar=self.sentinela)
            if est.ultima_procesada is not None and firma == est.ultima_procesada:
                continue  # sin cambios desde la última corrida completada
            sentinela_presente = bool(self.sentinela) and (fuente / self.sentinela).exists()
            if not lista_para_procesar(
                firma,
                ahora_ns=ahora,
                quiescencia_ns=self.quiescencia_ns,
                sentinela_presente=sentinela_presente,
                sentinela_requerida=bool(self.sentinela),
            ):
                continue
            resultado = self._procesar_fuente(fuente, firma, est)
            if resultado == "bloqueada":
                # Hay una corrida en curso ajena (disparo manual): no probamos más
                # fuentes ni avanzamos el cursor; el bucle duerme y reintenta.
                return None
            # 'hecha' o 'fallida': usamos el turno, avanzamos el cursor.
            self._cursor = (idx + 1) % n
            return nombre
        return None

    def _procesar_fuente(self, fuente: Path, firma: Firma, est: _EstadoFuente) -> str:
        """Corre la corrida completa de una fuente. Devuelve 'hecha' | 'fallida' |
        'bloqueada' (una corrida ajena tiene el lock)."""
        from normalizacion.ingesta.pipeline import ejecutar_corrida, iniciar_corrida

        nombre = fuente.name
        try:
            corrida_id, disco_id = iniciar_corrida(self.config, fuente, disco_id=nombre)
        except RuntimeError as exc:
            log.info("corrida_en_curso_reintento", fuente=nombre, motivo=str(exc)[:120])
            return "bloqueada"
        except (ValueError, OSError) as exc:
            log.error("fuente_invalida", fuente=nombre, error=str(exc)[:200])
            est.cooldown_hasta_ns = self._reloj_ns() + self.cooldown_fallo_ns
            return "fallida"
        log.info("corrida_iniciada", fuente=nombre, corrida=corrida_id, disco_id=disco_id,
                 n_archivos=firma.n_archivos, bytes=firma.bytes_totales)
        try:
            ejecutar_corrida(self.config, corrida_id, fuente, disco_id, workers=self.workers)
        except Exception as exc:  # la corrida ya se marcó FALLIDA en la BD
            log.error("corrida_fallida", fuente=nombre, corrida=corrida_id, error=str(exc)[:200])
            est.cooldown_hasta_ns = self._reloj_ns() + self.cooldown_fallo_ns
            return "fallida"
        # Completada: no reprocesar esta firma. Guardamos la que había ANTES de correr
        # (si llegó contenido durante la corrida, la firma habrá crecido y el próximo
        # ciclo lo recogerá).
        est.ultima_procesada = firma
        est.cooldown_hasta_ns = 0
        if self._reclamar is not None:
            try:
                if self._reclamar(
                    self.config, disco_id, fuente, firma, sentinela=self.sentinela
                ):
                    # El origen se borró: la firma vieja ya no aplica, empezamos limpio.
                    est.ultima_procesada = None
            except Exception as exc:  # reclamar nunca debe tumbar el vigilante
                log.error("reclamacion_fallida", fuente=nombre, disco_id=disco_id,
                          error=str(exc)[:200])
        return "hecha"

    # -- el bucle -----------------------------------------------------------------

    def correr(
        self, *, parar: Callable[[], bool] | None = None, max_ciclos: int | None = None
    ) -> None:
        parar = parar or (lambda: False)
        ciclos = 0
        log.info("vigilante_arranca", raiz=str(self.raiz), intervalo_s=self.intervalo_s,
                 quiescencia_ns=self.quiescencia_ns, sentinela=self.sentinela,
                 reclamar=self._reclamar is not None)
        while not parar():
            try:
                procesada = self.un_ciclo()
            except Exception as exc:  # un ciclo nunca debe matar el bucle
                log.error("ciclo_error", error=str(exc)[:300])
                procesada = None
            ciclos += 1
            if max_ciclos is not None and ciclos >= max_ciclos:
                return
            # Si acabamos de procesar algo, no dormimos: puede haber más fuentes listas.
            if procesada is None:
                self._dormir(self.intervalo_s)


def correr_vigilante(
    config: Config,
    carpeta: str | None = None,
    *,
    intervalo_s: float = 30.0,
    quiescencia_s: float = 60.0,
    sentinela: str | None = None,
    reclamar: bool = False,
    workers: int | None = None,
) -> None:
    """Punto de entrada del comando `norm vigilante`. Resuelve la carpeta raíz,
    instala el reclamador si procede y corre el bucle hasta recibir SIGTERM/SIGINT
    (parada limpia en Docker)."""
    from normalizacion.core import despliegue

    topo = despliegue.de_config(config)
    raiz_str = carpeta or config.api_carpeta_raiz
    if not raiz_str:
        raise ValueError(
            "no hay carpeta que vigilar: pasa --carpeta o define NORM_API_CARPETA_RAIZ"
        )
    raiz = Path(raiz_str).expanduser().resolve()
    if not raiz.is_dir():
        raise ValueError(f"la carpeta a vigilar no existe: {raiz}")

    reclamador: Callable[[Config, str], bool] | None = None
    if reclamar:
        # Solo el archivo maestro puede borrar el origen: su puerta en verde significa
        # que el blob está a salvo AQUÍ. En un nodo que replica hacia fuera, verde no
        # garantiza copia local, y borrar dejaría el dato en una sola máquina remota.
        if not topo.es_archivo_maestro:
            raise ValueError(
                f"--reclamar exige un nodo archivo maestro; el perfil"
                f" '{config.despliegue.perfil}' no lo es"
            )
        from normalizacion.ingesta.reclamacion import reclamar_origen

        reclamador = reclamar_origen

    v = Vigilante(
        config,
        raiz,
        intervalo_s=intervalo_s,
        quiescencia_s=quiescencia_s,
        sentinela=sentinela,
        workers=workers,
        reclamar=reclamador,
    )

    parar = {"flag": False}

    def _manejar(_sig: int, _frame: object) -> None:
        log.info("vigilante_parada_solicitada")
        parar["flag"] = True

    signal.signal(signal.SIGTERM, _manejar)
    signal.signal(signal.SIGINT, _manejar)

    # Al arrancar, sanea corridas huérfanas de un proceso anterior muerto a mitad:
    # si no, su EN_CURSO bloquearía el lock y el vigilante no ingeriría nunca.
    from normalizacion.ingesta.pipeline import marcar_corridas_huerfanas

    marcar_corridas_huerfanas(config)
    v.correr(parar=lambda: parar["flag"])
    log.info("vigilante_detenido")
