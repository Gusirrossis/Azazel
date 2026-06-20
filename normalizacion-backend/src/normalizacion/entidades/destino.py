"""Configuración del DESTINO de envío de entidades: a qué endpoint/webhook del backend
central (AEB) Azazel manda las entidades resueltas cuando esté hosteado. Se guarda como un
renglón JSON en `control`, editable desde la UI. El WORKER que hace el envío es trabajo
posterior (requiere el endpoint de ingesta del AEB); aquí queda la CONFIGURACIÓN lista."""

from __future__ import annotations

import json
from typing import Any

import psycopg

from normalizacion.core.config import Config

_CLAVE = "entidades_destino"
MODOS = ("push", "webhook")
_DEFAULT: dict[str, Any] = {
    "habilitado": False,
    "modo": "push",            # push (lotes) | webhook (por cambio)
    "url": "",                 # endpoint de ingesta del AEB, p. ej. https://aeb.vps/v1/ingest
    "auth_header": "X-API-Key",
    "auth_token": "",
    "receta": "fz1_bundle",    # con qué receta se proyectan las entidades al enviarlas
    "lote": 500,
    # 0 = solo manual (botón "Enviar ahora"); >0 = el sistema envía SOLO cada N segundos.
    "intervalo_seg": 0,
}


def leer_destino(config: Config) -> dict[str, Any]:
    """Config actual del destino (mezclada con los valores por defecto)."""
    with psycopg.connect(config.postgres_dsn) as conn:
        f = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CLAVE,)).fetchone()
    guardado: dict[str, Any] = json.loads(f[0]) if f else {}
    return {**_DEFAULT, **guardado}


def guardar_destino(config: Config, valor: dict[str, Any]) -> dict[str, Any]:
    """Valida y persiste la config del destino. Si está habilitado, exige URL http(s)."""
    limpio = {**_DEFAULT, **{k: v for k, v in valor.items() if k in _DEFAULT}}
    if limpio["modo"] not in MODOS:
        raise ValueError(f"modo inválido: '{limpio['modo']}' (usa {', '.join(MODOS)})")
    if limpio["habilitado"] and not str(limpio["url"]).startswith(("http://", "https://")):
        raise ValueError("con el destino habilitado, la URL debe empezar con http:// o https://")
    limpio["lote"] = max(1, min(int(limpio["lote"]), 5000))
    limpio["intervalo_seg"] = max(0, min(int(limpio["intervalo_seg"]), 86400))  # 0..24h
    with psycopg.connect(config.postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO control (clave, valor) VALUES (%s, %s)"
            " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
            (_CLAVE, json.dumps(limpio, ensure_ascii=False)),
        )
        conn.commit()
    return limpio
