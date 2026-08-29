"""Roles del panel y qué puede hacer cada uno.

Un solo lugar define la jerarquía para que la respuesta a "¿puede este usuario
hacer esto?" no quede repartida en veinte `if`. Los roles son ACUMULATIVOS:
`operador` puede todo lo de `lector`, y `admin` todo lo de `operador`.

  lector    consultar: buscar, ver tableros y entidades, descargar originales.
  operador  además OPERAR: lanzar corridas, reprocesar, mover frío, editar filtro.
  admin     además GOBERNAR: usuarios, claves de API, recetas y recursos.

Hasta aquí no existía esta distinción: cualquiera con la API key podía revocarle
la clave a los demás y borrar recetas. Separar `lector` de `admin` es la razón de
fondo de este cambio, no un adorno.
"""

from __future__ import annotations

from typing import Final, Literal

Rol = Literal["lector", "operador", "admin"]

ROLES: Final[tuple[Rol, ...]] = ("lector", "operador", "admin")

# Posición en la jerarquía. Mayor = puede más.
_NIVEL: Final[dict[str, int]] = {"lector": 0, "operador": 1, "admin": 2}


def valido(rol: str) -> bool:
    return rol in _NIVEL


def alcanza(rol: str, minimo: Rol) -> bool:
    """¿`rol` llega al menos a `minimo`? Un rol desconocido no alcanza nunca:
    ante un valor corrupto en la BD, se niega el acceso en vez de concederlo."""
    if rol not in _NIVEL:
        return False
    return _NIVEL[rol] >= _NIVEL[minimo]
