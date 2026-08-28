"""⚙ K16 — topología del despliegue: qué SABE HACER este nodo.

Azazel corre de varias formas con la MISMA base de código:

  · `local` — todo en una máquina. El default, y byte a byte el sistema de siempre.
  · híbrido — dos nodos que ambos ingieren, pero cosas distintas:
      `hibrido-ingesta`  (mac-01): discos físicos desechables · archivo maestro
      `hibrido-servicio` (vps-01): fuentes de red · entidades · API pública
  · `online` — un solo VPS que lo hace TODO y está expuesto: ingiere lo que cae en
      su carpeta, resuelve entidades, sirve al público Y es su propio archivo
      maestro. Es `hibrido-servicio` + `es_archivo_maestro`: como no replica a
      nadie, su puerta da verde por sí sola y puede reclamar el espacio del origen.

**La regla que sostiene el diseño:** los sitios de uso NUNCA preguntan por el
perfil, preguntan por la CAPACIDAD. Este módulo es el único lugar del código donde
`config.despliegue.perfil` se lee.

    # ❌   if config.despliegue.perfil == "hibrido-servicio":
    # ✅   if not despliegue.de_config(config).corre_entidades:

Consecuencia práctica: añadir mañana una topología nueva (workers repartidos en
varias máquinas, Fase 7 de ARQUITECTURA §8) es añadir una fila a `_MATRIZ`, no
tocar los veinte sitios que consultan capacidades.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from normalizacion.core.config import Config, PerillasDespliegue


@dataclass(frozen=True)
class Topologia:
    """Lo que este nodo puede hacer. Derivado del perfil, nunca escrito a mano."""

    corre_ingesta: bool
    """Catálogo, precalificación, workers, mover-frío y verificación."""

    corre_entidades: bool
    """Backfill de entidades Y el demonio de envío al AEB.

    Sólo UN nodo puede tenerla activa: `entidad_id` es determinista, pero cada nodo
    resuelve sobre SU parte del índice, así que dos resolvedores producirían
    conjuntos incompletos y distintos que se pisarían en el AEB (el cable manda
    `modo_merge: "reemplazar"`, es decir last-write-wins)."""

    sirve_publico: bool
    """La API de este nodo está expuesta a internet (exige auth, TLS y rate-limit
    de verdad, no el limitador en memoria por proceso)."""

    es_archivo_maestro: bool
    """Aquí converge la copia permanente de TODO. El nodo que no lo es debe replicar
    sus blobs al maestro antes de que su puerta pueda dar verde."""

    destino_eligible: bool
    """El front puede elegir carpeta de destino por corrida (`config_con_destino`).

    Se apaga en el nodo que replica blobs hacia fuera: con N almacenes en carpetas
    sueltas no hay nada único que replicar."""


# perfil → (ingesta, entidades, publico, maestro, destino_eligible)
_MATRIZ: dict[str, tuple[bool, bool, bool, bool, bool]] = {
    "local": (True, True, False, True, True),
    "hibrido-ingesta": (True, False, False, True, True),
    "hibrido-servicio": (True, True, True, False, False),
    # Nodo único, expuesto y todo-en-uno. Enciende TODO. `destino_eligible=False`
    # a propósito: el almacén es un MinIO fijo y direccionable —no carpetas sueltas
    # por corrida— para que el vigilante ingiera sin elegir destino y para que un
    # reindex-desde-almacén a futuro tenga un único sitio de dónde leer. Es el único
    # perfil con maestro=True y destino=False: el invariante prohíbe (no maestro ∧
    # destino), no (maestro ∧ no destino).
    "online": (True, True, True, True, False),
}


def derivar(p: PerillasDespliegue) -> Topologia:
    """Capacidades de un perfil. `local` las enciende todas salvo la exposición
    pública, y es el único que no participa en ninguna replicación."""
    ingesta, entidades, publico, maestro, destino = _MATRIZ[p.perfil]
    return Topologia(
        corre_ingesta=ingesta,
        corre_entidades=entidades,
        sirve_publico=publico,
        es_archivo_maestro=maestro,
        destino_eligible=destino,
    )


def de_config(config: Config) -> Topologia:
    """Atajo: la topología de este proceso."""
    return derivar(config.despliegue)


# ------------------------------------------------------------------ identidad de disco


def prefijo_disco(config: Config) -> str:
    """Prefijo de namespace para los `disco_id` NUEVOS de este nodo.

    Vacío en `local`: los identificadores existentes siguen siendo válidos para
    siempre y nada se recalcula."""
    return "" if config.despliegue.es_local() else f"{config.despliegue.nodo_id}:"


def normalizar_disco_id(config: Config, disco_id: str) -> str:
    """`disco_id` de un disco que se registra POR PRIMERA VEZ, con su namespace.

    ⚠️ Sólo para discos NUEVOS. Recalcular el `disco_id` de un disco ya catalogado
    cambiaría TODOS sus `archivo_id` (la identidad es
    `sha256(f"{disco_id}:{ruta_rel}|{tamaño}|{mtime_ns}")`), y como
    `insertar_pendientes` hace `ON CONFLICT (archivo_id) DO NOTHING`, las filas
    viejas NO se borrarían y las nuevas SÍ se insertarían: el disco quedaría
    duplicado entero en la cola y en el índice, con la puerta contando el doble.

    Idempotente: un id que ya trae el prefijo de este nodo se devuelve intacto, así
    que re-catalogar el mismo disco no lo re-prefija."""
    disco_id = disco_id.strip()
    if not disco_id:
        raise ValueError("disco_id vacío")
    prefijo = prefijo_disco(config)
    if not prefijo or disco_id.startswith(prefijo):
        return disco_id
    return f"{prefijo}{disco_id}"


def resolver_disco_id(
    config: Config, disco_id: str, *, ya_existe: Callable[[str], bool]
) -> str:
    """El `disco_id` DEFINITIVO al (re)catalogar, consultando qué hay ya registrado.

    Regla: **un disco ya registrado conserva su id, siempre**. Incluye los discos
    LEGADOS —catalogados antes de existir K16, sin prefijo de nodo—: re-prefijarlos
    al re-catalogarlos cambiaría todos sus `archivo_id` y, como el INSERT es
    `ON CONFLICT DO NOTHING`, las filas viejas quedarían y las nuevas se añadirían:
    el disco duplicado entero, con la puerta contando el doble.

    Sólo los discos NUEVOS estrenan namespace."""
    disco_id = disco_id.strip()
    if not disco_id:
        raise ValueError("disco_id vacío")
    if ya_existe(disco_id):
        return disco_id
    return normalizar_disco_id(config, disco_id)


def exige_disco_id_explicito(config: Config) -> bool:
    """¿Este nodo puede seguir derivando el `disco_id` del nombre de la carpeta?

    En `local` sí (comportamiento de siempre). Fuera de `local` NO: dos nodos que
    catalogan carpetas con el mismo basename producirían el mismo `disco_id` y sus
    `archivo_id` colisionarían. Además, dentro de un mismo nodo, dos discos
    desechables llamados igual ya hoy se fusionan en un solo `disco_id` y la puerta
    emite un veredicto sobre una unidad que no existe físicamente."""
    return not config.despliegue.es_local()
