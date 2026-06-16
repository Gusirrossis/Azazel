"""Proyección DINÁMICA: persona canónica → esquema del sistema consumidor.

La resolución produce una persona canónica ESTABLE (siempre la misma forma). Cada
sistema consumidor pide su propia estructura: otros nombres de campo, otra
anidación, otros valores (p. ej. sexo "H/M" vs "male/female"). Eso lo define una
RECETA DE PROYECCIÓN como DATOS — añadir un sistema = otra receta, sin tocar código.

Definición de una receta de proyección (JSON):
    { "passthrough": true }                       # devuelve la canónica tal cual (fz1)
  o { "salida": [
        { "path": "contact.email", "de": "email" },
        { "path": "gender", "de": "sexo", "mapa": {"H":"male","M":"female"} },
        { "path": "source", "constante": "azazel" },
        { "path": "age", "de": "edad", "default": "n/d" }
    ] }
`de` es una ruta con puntos dentro de la canónica; `path` la ruta de salida.
"""

from __future__ import annotations

from typing import Any


def get_path(d: dict[str, Any], ruta: str) -> Any:
    """Lee una ruta con puntos: get_path(c, 'nombre.nombre1')."""
    cur: Any = d
    for parte in ruta.split("."):
        if not isinstance(cur, dict) or parte not in cur:
            return None
        cur = cur[parte]
    return cur


def set_path(d: dict[str, Any], ruta: str, valor: Any) -> None:
    """Escribe una ruta con puntos creando los sub-objetos necesarios."""
    partes = ruta.split(".")
    cur = d
    for parte in partes[:-1]:
        nxt = cur.get(parte)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[parte] = nxt
        cur = nxt
    cur[partes[-1]] = valor


def validar_definicion(definicion: dict[str, Any]) -> None:
    """Valida la forma de una receta de proyección (lanza ValueError si está mal)."""
    if definicion.get("passthrough"):
        return
    salida = definicion.get("salida")
    if not isinstance(salida, list) or not salida:
        raise ValueError("la receta debe tener 'passthrough' o 'salida' (lista no vacía)")
    for i, spec in enumerate(salida):
        if not isinstance(spec, dict) or "path" not in spec:
            raise ValueError(f"salida[{i}] debe ser un objeto con 'path'")
        if "de" not in spec and "constante" not in spec:
            raise ValueError(f"salida[{i}] necesita 'de' o 'constante'")
        if "mapa" in spec and not isinstance(spec["mapa"], dict):
            raise ValueError(f"salida[{i}].mapa debe ser un objeto")


def aplicar_proyeccion(canonico: dict[str, Any], definicion: dict[str, Any]) -> dict[str, Any]:
    """Proyecta la persona canónica al esquema de la receta."""
    if definicion.get("passthrough"):
        return canonico
    salida: dict[str, Any] = {}
    for spec in definicion.get("salida", []):
        if "constante" in spec:
            valor = spec["constante"]
        else:
            valor = get_path(canonico, spec["de"])
            mapa = spec.get("mapa")
            if mapa is not None and valor is not None:
                valor = mapa.get(str(valor), mapa.get(valor, valor))
        if valor in (None, "") and "default" in spec:
            valor = spec["default"]
        if valor not in (None, ""):
            set_path(salida, spec["path"], valor)
    return salida


# ----------------------------------------------------------- recetas semilla

# fz1: la persona canónica tal cual (lo que ya produce la resolución).
RECETA_FZ1 = {
    "clave": "fz1",
    "clase": "proyeccion",
    "tipo": "persona",
    "nombre": "Fz1 (canónica)",
    "descripcion": "La ficha de persona tal cual la produce la resolución (esquema Fz1).",
    "definicion": {"passthrough": True},
    "version": "v1",
    "editable": False,  # es la base; se clona para crear variantes
}

# Ejemplo de OTRO sistema consumidor: esquema plano, en inglés, sexo male/female.
RECETA_SISTEMA_PLANO = {
    "clave": "sistema_plano",
    "clase": "proyeccion",
    "tipo": "persona",
    "nombre": "Sistema plano (ejemplo)",
    "descripcion": "Misma persona, otra estructura: plano, en inglés, gender male/female.",
    "definicion": {
        "salida": [
            {"path": "full_name", "de": "nombre_completo"},
            {"path": "national_id", "de": "curp"},
            {"path": "tax_id", "de": "rfc"},
            {"path": "gender", "de": "sexo", "mapa": {"H": "male", "M": "female"}},
            {"path": "birth_date", "de": "normalizados.normalized_dob"},
            {"path": "age", "de": "edad"},
            {"path": "birth_state", "de": "normalizados.normalized_estado"},
            {"path": "contact.email", "de": "email"},
            {"path": "contact.phone", "de": "telefono"},
            {"path": "source", "constante": "azazel"},
        ]
    },
    "version": "v1",
    "editable": True,
}

SEMILLAS = (RECETA_FZ1, RECETA_SISTEMA_PLANO)
