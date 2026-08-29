"""Sesiones del lado del servidor: la cookie solo lleva un identificador opaco.

Por qué así y no un JWT:

  * Un JWT es válido hasta que caduca. Para echar a alguien YA haría falta una lista
    negra — es decir, una tabla; exactamente la que un JWT venía a evitar.
  * Aquí la sesión ES la fila: `revocada_en = now()` y el siguiente request ya no pasa.
  * El token va en cookie `HttpOnly`, así que un XSS no puede leerlo. El
    `localStorage` que usaba el panel sí era legible por cualquier script inyectado.

De la cookie se guarda solo su **sha256**: quien lea la BD no puede suplantar a nadie.
sha256 basta (a diferencia de las contraseñas, que llevan argon2) porque el token
tiene 256 bits de entropía aleatoria y no hay nada que adivinar por diccionario.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from normalizacion.api.usuarios import Usuario
from normalizacion.core.config import Config

_TIMEOUT = 5

#: Nombre de la cookie. Con prefijo del proyecto para no chocar con otra app del dominio.
COOKIE = "norm_sesion"


@dataclass(frozen=True, slots=True)
class SesionActiva:
    """Lo que hace falta para atender un request: identidad ya resuelta."""

    sesion_id: int
    usuario_id: int
    usuario: str
    nombre: str
    rol: str
    debe_cambiar: bool


def _conectar(config: Config) -> psycopg.Connection:
    return psycopg.connect(config.postgres_dsn, connect_timeout=_TIMEOUT)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def crear(
    config: Config,
    usuario: Usuario,
    *,
    ip: str = "",
    agente: str = "",
) -> tuple[str, datetime]:
    """Abre una sesión y devuelve `(token_en_claro, expira_en)`.

    El token en claro se devuelve UNA vez, para ponerlo en la cookie; después solo
    queda su hash y ya no se puede reconstruir.
    """
    token = secrets.token_urlsafe(32)
    expira = datetime.now(UTC) + timedelta(minutes=config.sesion_duracion_min)
    with _conectar(config) as conn:
        conn.execute(
            "INSERT INTO sesiones (usuario_id, hash_token, expira_en, ip, agente)"
            " VALUES (%s, %s, %s, %s, %s)",
            (usuario.id, _hash(token), expira, ip[:120], agente[:300]),
        )
        conn.commit()
    return token, expira


def validar(config: Config, token: str | None) -> SesionActiva | None:
    """Resuelve el token a una identidad, o None si no sirve.

    Exige de una vez que la sesión no esté revocada ni vencida Y que el usuario siga
    activo: desactivar una cuenta corta sus sesiones abiertas en el acto, sin tener
    que ir a buscarlas una por una.
    """
    if not token:
        return None
    with _conectar(config) as conn:
        fila = conn.execute(
            "SELECT s.id, u.id, u.usuario, u.nombre, u.rol, u.debe_cambiar"
            " FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id"
            " WHERE s.hash_token = %s AND s.revocada_en IS NULL"
            "   AND s.expira_en > now() AND u.activo",
            (_hash(token),),
        ).fetchone()
        if fila is None:
            return None
        # Renovación deslizante: mientras se use, la sesión no caduca por reloj;
        # caduca por INACTIVIDAD, que es lo que de verdad importa aquí.
        conn.execute(
            "UPDATE sesiones SET vista_en = now(), expira_en = now() + %s * interval '1 minute'"
            " WHERE id = %s",
            (config.sesion_duracion_min, fila[0]),
        )
        conn.commit()
    return SesionActiva(
        sesion_id=fila[0],
        usuario_id=fila[1],
        usuario=fila[2],
        nombre=fila[3],
        rol=fila[4],
        debe_cambiar=fila[5],
    )


def revocar(config: Config, token: str | None) -> bool:
    """Cierra la sesión de este token (logout). True si había algo que cerrar."""
    if not token:
        return False
    with _conectar(config) as conn:
        fila = conn.execute(
            "UPDATE sesiones SET revocada_en = now()"
            " WHERE hash_token = %s AND revocada_en IS NULL RETURNING id",
            (_hash(token),),
        ).fetchone()
        conn.commit()
    return fila is not None


def revocar_todas(config: Config, usuario_id: int, *, excepto: int | None = None) -> int:
    """Cierra las sesiones del usuario. Con `excepto` deja viva la actual — el caso
    de "cerrar sesión en los demás dispositivos" sin echarse uno mismo."""
    sql = (
        "UPDATE sesiones SET revocada_en = now()"
        " WHERE usuario_id = %s AND revocada_en IS NULL"
    )
    params: tuple[Any, ...] = (usuario_id,)
    if excepto is not None:
        sql += " AND id <> %s"
        params = (usuario_id, excepto)
    with _conectar(config) as conn:
        cur = conn.execute(sql, params)
        n = cur.rowcount
        conn.commit()
    return max(n, 0)


def listar(config: Config, usuario_id: int) -> list[dict[str, Any]]:
    """Sesiones abiertas del usuario, para que reconozca las suyas y cierre lo raro."""
    with _conectar(config) as conn:
        filas = conn.execute(
            "SELECT id, creada_en, vista_en, expira_en, ip, agente FROM sesiones"
            " WHERE usuario_id = %s AND revocada_en IS NULL AND expira_en > now()"
            " ORDER BY vista_en DESC",
            (usuario_id,),
        ).fetchall()
    return [
        {
            "id": f[0],
            "creada_en": f[1].isoformat() if f[1] else None,
            "vista_en": f[2].isoformat() if f[2] else None,
            "expira_en": f[3].isoformat() if f[3] else None,
            "ip": f[4],
            "agente": f[5],
        }
        for f in filas
    ]


def barrer(config: Config) -> int:
    """Borra sesiones muertas (vencidas o revocadas hace rato). La tabla crece con
    cada login; sin esto solo sube. Se llama al arrancar la API."""
    with _conectar(config) as conn:
        cur = conn.execute(
            "DELETE FROM sesiones WHERE expira_en < now() - interval '7 days'"
            "    OR (revocada_en IS NOT NULL AND revocada_en < now() - interval '7 days')"
        )
        n = cur.rowcount
        conn.commit()
    return max(n, 0)
