"""Hash y política de contraseñas.

argon2id: es el ganador del Password Hashing Competition y lo que hoy recomienda
OWASP. A diferencia de sha256 (que usamos para los TOKENS, donde el secreto tiene
256 bits de entropía y basta), una contraseña la elige una persona y hay que
encarecer deliberadamente cada intento de adivinarla.

`argon2-cffi` trae parámetros por defecto sensatos y los revisa en cada versión;
no los fijamos a mano para no congelar hoy un coste que quedará corto mañana.
"""

from __future__ import annotations

import re
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher: Final = PasswordHasher()

LONGITUD_MINIMA: Final = 12


class ContrasenaDebil(ValueError):
    """La contraseña no cumple la política mínima."""


def exigir_politica(contrasena: str) -> None:
    """Longitud por encima de composición: doce caracteres cualesquiera resisten
    mucho más que ocho con un signo obligatorio, y no empujan al usuario hacia
    `Password1!` — el patrón que cualquier diccionario prueba primero."""
    if len(contrasena) < LONGITUD_MINIMA:
        raise ContrasenaDebil(
            f"La contraseña debe tener al menos {LONGITUD_MINIMA} caracteres."
        )
    if not re.search(r"\S", contrasena):
        raise ContrasenaDebil("La contraseña no puede ser solo espacios.")


def cifrar(contrasena: str) -> str:
    return _hasher.hash(contrasena)


def verificar(hash_guardado: str, presentada: str) -> bool:
    """Comparación en tiempo constante; la hace argon2 internamente."""
    try:
        return _hasher.verify(hash_guardado, presentada)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        # Un hash corrupto en la BD no debe tumbar el login de todos.
        return False


def necesita_rehash(hash_guardado: str) -> bool:
    """True si el hash se generó con parámetros más flojos que los actuales: se
    aprovecha el login (el único momento con la contraseña en claro) para subirlo."""
    try:
        return _hasher.check_needs_rehash(hash_guardado)
    except Exception:
        return False
