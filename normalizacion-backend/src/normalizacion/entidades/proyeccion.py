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

import re
from typing import Any

_RE_RUTA = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")


def get_path(d: dict[str, Any], ruta: str) -> Any:
    """Lee una ruta con puntos: get_path(c, 'nombre.nombre1')."""
    cur: Any = d
    for parte in ruta.split("."):
        if not isinstance(cur, dict) or parte not in cur:
            return None
        cur = cur[parte]
    return cur


def set_path(d: dict[str, Any], ruta: str, valor: Any) -> None:
    """Escribe una ruta con puntos creando los sub-objetos necesarios.

    Si un tramo intermedio ya tiene un valor ESCALAR (colisión de rutas, p. ej.
    'contact' y 'contact.email' en la misma receta), lanza en vez de pisarlo
    silenciosamente. La validación de la receta ya lo impide antes de llegar aquí.
    """
    partes = ruta.split(".")
    cur = d
    for parte in partes[:-1]:
        nxt = cur.get(parte)
        if nxt is None:
            nxt = {}
            cur[parte] = nxt
        elif not isinstance(nxt, dict):
            raise ValueError(f"colisión de rutas: '{parte}' en '{ruta}' ya tiene un escalar")
        cur = nxt
    cur[partes[-1]] = valor


def _validar_item(definicion: dict[str, Any]) -> None:
    """Valida una receta POR-ÍTEM (una persona → un objeto): passthrough o salida."""
    if definicion.get("passthrough"):
        return
    salida = definicion.get("salida")
    if not isinstance(salida, list) or not salida:
        raise ValueError("la receta debe tener 'passthrough' o 'salida' (lista no vacía)")
    paths: list[str] = []
    for i, spec in enumerate(salida):
        if not isinstance(spec, dict) or "path" not in spec:
            raise ValueError(f"salida[{i}] debe ser un objeto con 'path'")
        if ("de" in spec) == ("constante" in spec):
            raise ValueError(f"salida[{i}] necesita exactamente uno de 'de' o 'constante'")
        if "mapa" in spec:
            if "de" not in spec:
                raise ValueError(f"salida[{i}]: 'mapa' solo aplica con 'de' (no con 'constante')")
            if not isinstance(spec["mapa"], dict):
                raise ValueError(f"salida[{i}].mapa debe ser un objeto")
        if not _RE_RUTA.match(str(spec["path"])):
            raise ValueError(f"salida[{i}].path '{spec['path']}' no es una ruta válida")
        if "de" in spec and not _RE_RUTA.match(str(spec["de"])):
            raise ValueError(f"salida[{i}].de '{spec['de']}' no es una ruta válida")
        paths.append(str(spec["path"]))
    for a in paths:
        for b in paths:
            if a != b and b.startswith(a + "."):
                raise ValueError(f"colisión de rutas: '{a}' es prefijo de '{b}'")


def es_coleccion(definicion: dict[str, Any]) -> bool:
    """True si la receta arma el ARCHIVO completo (sobre + arreglo), no una persona."""
    return isinstance(definicion, dict) and "coleccion" in definicion


def validar_definicion(definicion: dict[str, Any]) -> None:
    """Valida una receta de proyección (lanza ValueError si está mal).

    Dos clases de receta:
      · POR-ÍTEM: {passthrough} o {salida:[...]} — una persona → un objeto.
      · COLECCIÓN: {sobre?, coleccion, item} — arma el ARCHIVO completo: el `sobre`
        (metadatos constantes) más `coleccion` (la clave, p.ej. 'personas') con el
        arreglo de personas proyectadas con la receta por-ítem `item`."""
    if not isinstance(definicion, dict):
        raise ValueError("la receta debe ser un objeto JSON, no una lista ni un valor suelto")
    # Datos pegados por error (el ARCHIVO de salida en vez de la receta).
    if any(k in definicion for k in ("personas", "_metadata", "_mapeo_normalizacion_sistema")):
        raise ValueError(
            "esto parece DATOS (tiene 'personas'/'_metadata'), no una receta. Una receta "
            "describe la TRANSFORMACIÓN: {salida:[...]} por persona, o {sobre, coleccion, item} "
            "para el archivo completo. El archivo de datos se EXPORTA, no se pega aquí."
        )
    if es_coleccion(definicion) or "sobre" in definicion or "item" in definicion:
        col = definicion.get("coleccion")
        if not isinstance(col, str) or not _RE_RUTA.match(col):
            raise ValueError("receta de colección: 'coleccion' debe ser una clave válida (p.ej. 'personas')")
        sobre = definicion.get("sobre", {})
        if not isinstance(sobre, dict):
            raise ValueError("receta de colección: 'sobre' debe ser un objeto")
        if col in sobre:
            raise ValueError(
                f"receta de colección: la clave 'coleccion' ('{col}') ya existe en 'sobre'; "
                "la sobrescribiría — usa otro nombre de colección"
            )
        item = definicion.get("item")
        if not isinstance(item, dict):
            raise ValueError("receta de colección: falta 'item' (la receta por persona)")
        _validar_item(item)
        return
    _validar_item(definicion)


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


def exportar_coleccion(
    canonicos: list[dict[str, Any]], definicion: dict[str, Any]
) -> Any:
    """Arma el ARCHIVO de salida a partir de N personas canónicas.

    Receta de colección ({sobre, coleccion, item}): devuelve el `sobre` con la clave
    `coleccion` = arreglo de personas proyectadas con `item`. Receta por-ítem usada en
    lote: devuelve el arreglo plano de personas proyectadas."""
    if es_coleccion(definicion):
        item_def = definicion.get("item", {"passthrough": True})
        arr = [aplicar_proyeccion(c, item_def) for c in canonicos]
        salida: dict[str, Any] = dict(definicion.get("sobre", {}))
        salida[definicion["coleccion"]] = arr
        return salida
    return [aplicar_proyeccion(c, definicion) for c in canonicos]


# ----------------------------------------------------------- recetas semilla
#
# El sistema arranca con SOLO DOS recetas, para que se entienda el mecanismo:
#   · fz1_bundle  → arma el ARCHIVO Fz1 completo (receta de COLECCIÓN). Es la que
#                   genera el JSON que pide Fz1. Se usa al EXPORTAR.
#   · sistema_plano → un EJEMPLO de receta por-persona (otra estructura/idioma) que
#                   muestra las 4 operaciones: renombrar (de), mapear (mapa),
#                   constante y anidar (path con puntos).
# Para crear más, se clonan desde la UI (pestaña Entidades → Recetas). La estructura
# de salida es DATO editable, no código.

# Ejemplo de OTRO sistema consumidor: esquema plano, en inglés, sexo male/female.
RECETA_SISTEMA_PLANO = {
    "clave": "sistema_plano",
    "clase": "proyeccion",
    "tipo": "persona",
    "nombre": "Ejemplo — otra estructura (1 persona)",
    "descripcion": "EJEMPLO: la misma persona en otra forma (plano, inglés, gender male/female). "
                   "Muestra renombrar, mapear valores, constante y anidar.",
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

# fz1_bundle: el ARCHIVO Fz1 completo (sobre _metadata + personas[] + _mapeo). Es
# una receta de COLECCIÓN: se usa para EXPORTAR todas las personas a un solo archivo
# con el formato que pide Fz1, no para proyectar una persona.
RECETA_FZ1_BUNDLE = {
    "clave": "fz1_bundle",
    "clase": "proyeccion",
    "tipo": "persona",
    "nombre": "Fz1 archivo completo (exportar)",
    "descripcion": "El archivo Fz1 entero: _metadata + personas[] + _mapeo. Para EXPORTAR la colección.",
    "definicion": {
        "sobre": {
            "_metadata": {
                "origen": "Fz1 Tactical Intelligence Platform",
                "tipo_documento": "Organización de Datos exportada por Azazel (Inyección e Indexación)",
                "descripcion": "Personas resueltas y normalizadas, listas para inyectar en la base"
                               " central de Fz1 (central.db / investigations.db).",
                "version_schema": "11.0",
            },
            "_mapeo_normalizacion_sistema": {
                "campos_normalizados": {
                    "normalized_name": "Unión del nombre1 + nombre2 + apellido1 + apellido2",
                    "normalized_dob": "Fecha de nacimiento (o inferida de CURP [dígitos 4 a 9])",
                    "normalized_curp": "Clave única identificadora (CURP / RFC / Teléfono)",
                    "normalized_sex": "Género o sexo normalizado (H/M)",
                    "normalized_estado": "Entidad federativa de procedencia o residencia",
                    "normalized_mpio": "Municipio, alcaldía o delegación normalizada",
                },
            },
        },
        "coleccion": "personas",
        # SOLO los campos que E1-E3 resuelve de verdad (sin placeholders engañosos).
        # `figura` es un default visual de Fz1. es_objetivo, redes{}, notas y
        # vincular_con{} se agregan aquí (una línea c/u) cuando E5 los resuelva.
        "item": {
            "salida": [
                {"path": "figura", "constante": "cube"},
                {"path": "nombre.nombre1", "de": "nombre.nombre1"},
                {"path": "nombre.nombre2", "de": "nombre.nombre2"},
                {"path": "nombre.apellido1", "de": "nombre.apellido1"},
                {"path": "nombre.apellido2", "de": "nombre.apellido2"},
                {"path": "alias", "de": "alias"},
                {"path": "edad", "de": "edad"},
                {"path": "curp", "de": "curp"},
                {"path": "rfc", "de": "rfc"},
                {"path": "sexo", "de": "sexo"},
                {"path": "direccion.calle", "de": "direccion.calle"},
                {"path": "direccion.numero_exterior", "de": "direccion.numero_exterior"},
                {"path": "direccion.numero_interior", "de": "direccion.numero_interior"},
                {"path": "direccion.colonia", "de": "direccion.colonia"},
                {"path": "direccion.municipio", "de": "direccion.municipio"},
                {"path": "direccion.codigo_postal", "de": "direccion.codigo_postal"},
                {"path": "direccion.estado", "de": "direccion.estado"},
                {"path": "direccion.pais", "de": "direccion.pais"},
                {"path": "email", "de": "email"},
                {"path": "telefono", "de": "telefono"},
                {"path": "relacion", "de": "relacion"},
            ]
        },
    },
    "version": "v1",
    "editable": True,
}

# Arranca con SOLO el archivo Fz1 + un ejemplo. Las demás se crean clonando.
SEMILLAS = (RECETA_FZ1_BUNDLE, RECETA_SISTEMA_PLANO)
