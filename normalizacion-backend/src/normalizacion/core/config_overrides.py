"""Overrides de perillas del FILTRO editables desde la UI (tabla `config_overrides`).

La config base sigue siendo inmutable por proceso (defaults + .env + NORM_*); esta
capa guarda en Postgres SOLO lo editado y lo mergea al construir la config de cada
corrida (`aplicar_overrides` en POST /pipeline/ejecutar) — la edición aplica a la
SIGUIENTE corrida sin reiniciar nada. `rescore-frio` re-evalúa lo ya enviado a frío.

Auditabilidad: si los overrides cambian la conducta del filtro y no traen una
`version_filtro` explícita, se deriva una con huella del contenido
(`base+ov-<sha256[:8]>`) — cada decisión guardada queda trazada a la versión real.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.config import Config, PerillasFiltro

SECCION_FILTRO = "filtro"

# Perillas que la UI puede tocar. Todo lo demás (guards K4, pesos K7, kill-rules…)
# sigue siendo territorio de .env/código — ampliar esta lista es una decisión.
CAMPOS_EDITABLES = frozenset(
    {
        "modo_lista",
        "tipos_interes",
        "tipos_interes_prefijos",
        "tipos_excluidos",
        "entropia_texto_max",
        "entropia_comprimido_min",
        "ratio_imprimibles_min",
        "umbral_hot",
        "umbral_cold",
        "prioridad_contenedores",
        "prioridad_extensiones",
        "version_filtro",
    }
)


def leer_overrides(
    conn: psycopg.Connection[Any], seccion: str = SECCION_FILTRO
) -> dict[str, Any]:
    fila = conn.execute(
        "SELECT valores FROM config_overrides WHERE seccion = %s", (seccion,)
    ).fetchone()
    return dict(fila[0]) if fila else {}


def guardar_overrides(
    conn: psycopg.Connection[Any], valores: dict[str, Any], seccion: str = SECCION_FILTRO
) -> None:
    desconocidos = set(valores) - CAMPOS_EDITABLES
    if desconocidos:
        raise ValueError(f"perillas no editables: {sorted(desconocidos)}")
    conn.execute(
        "INSERT INTO config_overrides (seccion, valores) VALUES (%s, %s)"
        " ON CONFLICT (seccion) DO UPDATE"
        " SET valores = EXCLUDED.valores, actualizado_en = now()",
        (seccion, Jsonb(valores)),
    )


def borrar_overrides(conn: psycopg.Connection[Any], seccion: str = SECCION_FILTRO) -> None:
    conn.execute("DELETE FROM config_overrides WHERE seccion = %s", (seccion,))


def derivar_version(version_base: str, overrides: dict[str, Any]) -> str:
    """`reglas-v3-lista-blanca` + overrides → `reglas-v3-lista-blanca+ov-1a2b3c4d`."""
    base = version_base.split("+ov-")[0]
    contenido = {k: v for k, v in sorted(overrides.items()) if k != "version_filtro"}
    huella = hashlib.sha256(
        json.dumps(contenido, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]
    return f"{base}+ov-{huella}"


def filtro_efectivo(config: Config, overrides: dict[str, Any]) -> PerillasFiltro:
    """Mergea y RE-VALIDA (rangos ge/le incluidos) — jamás un model_copy a secas."""
    if not overrides:
        return config.filtro
    return PerillasFiltro.model_validate({**config.filtro.model_dump(), **overrides})


def aplicar_overrides(config: Config) -> Config:
    """Config con los overrides guardados aplicados (para la corrida que arranca).

    Sin fila de overrides → la config vuelve intacta. Overrides inválidos (p. ej.
    guardados con una versión anterior del modelo) → ValueError, la API lo mapea a 400.
    """
    with psycopg.connect(config.postgres_dsn) as conn:
        overrides = leer_overrides(conn)
    if not overrides:
        return config
    return config.model_copy(update={"filtro": filtro_efectivo(config, overrides)})
