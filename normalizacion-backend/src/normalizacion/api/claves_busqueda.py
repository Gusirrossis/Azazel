"""Claves de API **con nombre** para el endpoint de búsqueda, gestionadas desde el panel.

Solo se guarda el **hash sha256** de cada clave (nunca el texto): al generar se muestra una vez
y ya no se puede recuperar, solo revocar/rotar. Se persisten como un renglón JSON en `control`,
igual que el resto de la config dinámica. Verificación en tiempo constante.

Auth abierta (dev) SOLO si no hay ninguna clave —ni estática de `config.api_keys` ni con nombre—;
en cuanto se genera la primera, el endpoint queda cerrado."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any

import psycopg

from normalizacion.core.config import Config

_CLAVE = "busqueda_api_keys"
_TTL_CACHE_S = 5.0  # evita leer la BD en cada request de búsqueda
_cache: tuple[float, list[dict[str, Any]]] | None = None


def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _leer(config: Config) -> list[dict[str, Any]]:
    """Lista cruda [{nombre, hash, creada_en}] desde `control` (o [])."""
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        f = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CLAVE,)).fetchone()
    if not f:
        return []
    datos = json.loads(f[0])
    return datos if isinstance(datos, list) else []


def _guardar(config: Config, claves: list[dict[str, Any]]) -> None:
    global _cache
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        conn.execute(
            "INSERT INTO control (clave, valor) VALUES (%s, %s)"
            " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
            (_CLAVE, json.dumps(claves, ensure_ascii=False)),
        )
        conn.commit()
    _cache = None  # invalida el cache tras un cambio


def _leer_cacheado(config: Config) -> list[dict[str, Any]]:
    global _cache
    ahora = time.monotonic()
    if _cache is not None and ahora - _cache[0] < _TTL_CACHE_S:
        return _cache[1]
    claves = _leer(config)
    _cache = (ahora, claves)
    return claves


def listar_claves(config: Config) -> list[dict[str, Any]]:
    """Para el panel: nombre y fecha, SIN el hash ni el secreto."""
    return [
        {"nombre": c.get("nombre", ""), "creada_en": c.get("creada_en")}
        for c in _leer(config)
    ]


def generar_clave(config: Config, nombre: str) -> str:
    """Genera una clave nueva para `nombre` (reemplaza si ya existía), guarda su hash y
    devuelve el texto en claro UNA sola vez."""
    nombre = nombre.strip()
    if not nombre:
        raise ValueError("el nombre de la clave no puede estar vacío")
    clave = f"bus_{secrets.token_urlsafe(30)}"
    claves = [c for c in _leer(config) if c.get("nombre") != nombre]
    claves.append(
        {"nombre": nombre, "hash": _hash(clave), "creada_en": datetime.now(UTC).isoformat()}
    )
    _guardar(config, claves)
    return clave


def revocar_clave(config: Config, nombre: str) -> bool:
    """Elimina la clave `nombre`. Devuelve True si existía."""
    claves = _leer(config)
    quedan = [c for c in claves if c.get("nombre") != nombre]
    if len(quedan) == len(claves):
        return False
    _guardar(config, quedan)
    return True


def autorizada(config: Config, presentada: str | None) -> bool:
    """¿La clave presentada es válida? Combina las estáticas de `config.api_keys` (texto plano,
    compat) y las dinámicas con nombre (por hash). Sin NINGUNA clave configurada → abierto (dev)."""
    estaticas = tuple(config.api_keys)
    dinamicas = _leer_cacheado(config)
    if not estaticas and not dinamicas:
        return True  # dev: sin claves, canal abierto
    if not presentada:
        return False
    if presentada in estaticas:
        return True
    h = _hash(presentada)
    return any(hmac.compare_digest(h, c.get("hash", "")) for c in dinamicas)
