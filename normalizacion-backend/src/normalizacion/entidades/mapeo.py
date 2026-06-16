"""Fase A — mapeo columna→campo SEMIAUTOMÁTICO ("propone y confirmo").

El sistema PROPONE el mapeo combinando (i) el NOMBRE de la columna contra el
diccionario de sinónimos de la receta y (ii) el CONTENIDO muestreado (una columna
cuyas celdas validan como CURP/RFC/correo/teléfono → ese campo). El operador
confirma UNA vez por "forma" de dataset (huella de columnas); el mapeo aprobado
se reutiliza en datasets con la misma forma.
"""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import normalizadores as N
from .receta import Receta

# Validadores de ancla para el mapeo por contenido (campo → función).
_VALIDADORES_CONTENIDO = {
    "curp": N.validar_curp,
    "rfc": N.validar_rfc,
    "email": N.normalizar_email,
    "telefono": N.normalizar_telefono_mx,
}


def huella_columnas(columnas: list[str]) -> str:
    """La 'forma' del dataset: sha256 de los nombres de columna plegados y ordenados."""
    base = "|".join(sorted(N.plegar(c) for c in columnas))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def proponer_mapeo(
    receta: Receta,
    columnas: list[str],
    muestras: dict[str, list[Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Propone {columna: {campo, confianza, motivo}} para cada columna del dataset.

    Confianza: 0.95 si el CONTENIDO valida como un ancla; 0.8 si el nombre coincide
    exacto con un sinónimo; 0.6 si lo contiene. None (sin propuesta) si nada encaja.
    """
    propuestas: dict[str, dict[str, Any]] = {}
    for col in columnas:
        plg = N.plegar(col)
        mejor: dict[str, Any] | None = None

        # (ii) por CONTENIDO: ¿las celdas validan como un ancla?
        valores = (muestras or {}).get(col) or []
        no_vacios = [str(v) for v in valores if v not in (None, "")]
        if no_vacios:
            for campo, validar in _VALIDADORES_CONTENIDO.items():
                validos = sum(1 for v in no_vacios if validar(v).valido)
                if validos / len(no_vacios) >= 0.6 and receta.por_nombre(campo):
                    mejor = {"campo": campo, "confianza": 0.95,
                             "motivo": f"{validos}/{len(no_vacios)} celdas validan como {campo}"}
                    break

        # (i) por NOMBRE de columna. Coincidencia EXACTA (0.8) o por PALABRA completa
        # (0.6) — nunca por subcadena suelta (evita que 'col' empate 'columna_rara').
        if mejor is None:
            plg_pad = f" {plg} "
            best: tuple[float, str, str] | None = None
            for c in receta.campos:
                for syn in c.sinonimos:
                    if plg == syn:
                        cand = (0.8, c.nombre, f"nombre coincide con sinónimo '{syn}'")
                    elif f" {syn} " in plg_pad:
                        cand = (0.6, c.nombre, f"nombre contiene la palabra '{syn}'")
                    else:
                        continue
                    if best is None or cand[0] > best[0]:
                        best = cand
            if best:
                mejor = {"campo": best[1], "confianza": best[0], "motivo": best[2]}

        propuestas[col] = mejor or {"campo": None, "confianza": 0.0, "motivo": "sin coincidencia"}
    return propuestas


def asignacion_desde_propuesta(
    propuestas: dict[str, dict[str, Any]], umbral: float = 0.6
) -> dict[str, str]:
    """Convierte las propuestas en una asignación {columna: campo} aplicando un
    umbral de confianza (lo que el front pre-marcaría para que el operador confirme)."""
    return {
        col: p["campo"]
        for col, p in propuestas.items()
        if p.get("campo") and p["confianza"] >= umbral
    }


def guardar_mapeo(
    conn: psycopg.Connection[Any], huella: str, tipo: str,
    asignacion: dict[str, str], version: str,
) -> None:
    conn.execute(
        "INSERT INTO mapeos_aprobados (huella, tipo_entidad, asignacion, version)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (huella) DO UPDATE SET asignacion = EXCLUDED.asignacion,"
        " version = EXCLUDED.version, creado_en = now()",
        (huella, tipo, Jsonb(asignacion), version),
    )


def leer_mapeo(conn: psycopg.Connection[Any], huella: str) -> dict[str, str] | None:
    fila = conn.execute(
        "SELECT asignacion FROM mapeos_aprobados WHERE huella = %s", (huella,)
    ).fetchone()
    return dict(fila[0]) if fila else None
