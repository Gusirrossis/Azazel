"""Configuración del DESTINO de envío de entidades al backend central (orquestador / AEB).

Aquí en Azazel SOLO se decide **a dónde** se mandan las entidades resueltas y **cada cuánto**
(envío automático). Azazel manda la entidad **completa, en formato canónico, tal como la resuelve**;
es el orquestador quien la proyecta a la forma de cada consumidor (FLUX/Gotham/Fz1). Por eso aquí
NO hay receta ni modo: el envío es siempre push del canónico. Se guarda como un renglón JSON en
`control`, editable desde la UI."""

from __future__ import annotations

import json
from typing import Any

import psycopg

from normalizacion.core.config import Config

_CLAVE = "entidades_destino"
_DEFAULT: dict[str, Any] = {
    "habilitado": False,
    "url": "",                 # endpoint del orquestador, p. ej. https://orquestador.vps
    "auth_token": "",          # la clave de ingesta (se manda en el header X-API-Key)
    "lote": 500,               # cuántas entidades por tanda
    # 0 = solo manual (botón "Enviar ahora"); >0 = el sistema envía SOLO cada N segundos.
    "intervalo_seg": 0,
}


def leer_destino(config: Config) -> dict[str, Any]:
    """Config actual del destino (solo claves conocidas, mezclada con los valores por defecto)."""
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        f = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CLAVE,)).fetchone()
    guardado: dict[str, Any] = json.loads(f[0]) if f else {}
    return {**_DEFAULT, **{k: v for k, v in guardado.items() if k in _DEFAULT}}


def guardar_destino(config: Config, valor: dict[str, Any]) -> dict[str, Any]:
    """Valida y persiste la config del destino. Si está habilitado, exige URL http(s)."""
    limpio = {**_DEFAULT, **{k: v for k, v in valor.items() if k in _DEFAULT}}
    if limpio["habilitado"] and not str(limpio["url"]).startswith(("http://", "https://")):
        raise ValueError("con el destino habilitado, la URL debe empezar con http:// o https://")
    limpio["lote"] = max(1, min(int(limpio["lote"]), 5000))
    limpio["intervalo_seg"] = max(0, min(int(limpio["intervalo_seg"]), 86400))  # 0..24h
    with psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn:
        conn.execute(
            "INSERT INTO control (clave, valor) VALUES (%s, %s)"
            " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
            (_CLAVE, json.dumps(limpio, ensure_ascii=False)),
        )
        conn.commit()
    return limpio
