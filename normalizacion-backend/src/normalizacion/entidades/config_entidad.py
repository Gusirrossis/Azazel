"""Configuración editable del ESQUEMA de la entidad: atributos EXTRA declarados.

El núcleo canónico (nombre, curp, rfc, dirección, …) es fijo. Aquí el operador declara
atributos adicionales —color_favorito, placa, hobby…— que se CAPTURAN dentro de
`campos.atributos` cuando una fuente los trae mapeados. Decisión del usuario: se guarda
SOLO lo declarado (no una bolsa abierta); lo no declarado se descarta (pero el archivo
origen queda en el lago, reproyectable). Se guarda como un renglón JSON en `control`,
editable desde la UI, sin tocar código ni migraciones.
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg

from normalizacion.core.config import Config

_CLAVE = "entidad_atributos_declarados"
NORMALIZADORES = ("texto", "curp", "rfc", "email", "telefono", "nombre")
_RE_NOMBRE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# Nombres del núcleo canónico: un atributo declarado no puede pisarlos.
_RESERVADOS = {
    "nombre", "nombre_completo", "alias", "curp", "rfc", "sexo", "edad", "direccion",
    "email", "telefono", "relacion", "normalizados", "atributos",
}


def leer_atributos(config: Config) -> list[dict[str, str]]:
    """Lista de atributos declarados [{nombre, normalizador}] (vacía si no hay)."""
    with psycopg.connect(config.postgres_dsn) as conn:
        f = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CLAVE,)).fetchone()
    return json.loads(f[0]) if f else []


def guardar_atributos(config: Config, lista: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Valida y persiste la lista de atributos declarados. Rechaza nombres inválidos,
    reservados (chocan con el núcleo) o normalizadores desconocidos. Devuelve la limpia."""
    limpia: list[dict[str, str]] = []
    vistos: set[str] = set()
    for a in lista:
        nombre = str(a.get("nombre", "")).strip().lower()
        norm = str(a.get("normalizador", "texto")).strip().lower()
        if not _RE_NOMBRE.match(nombre):
            raise ValueError(f"nombre de atributo inválido: '{nombre}' (minúsculas, dígitos y _)")
        if nombre in _RESERVADOS:
            raise ValueError(f"'{nombre}' es un campo del núcleo: elige otro nombre")
        if norm not in NORMALIZADORES:
            opciones = ", ".join(NORMALIZADORES)
            raise ValueError(f"normalizador desconocido: '{norm}' (usa {opciones})")
        if nombre in vistos:
            continue
        vistos.add(nombre)
        limpia.append({"nombre": nombre, "normalizador": norm})
    with psycopg.connect(config.postgres_dsn) as conn:
        conn.execute(
            "INSERT INTO control (clave, valor) VALUES (%s, %s)"
            " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
            (_CLAVE, json.dumps(limpia, ensure_ascii=False)),
        )
        conn.commit()
    return limpia
