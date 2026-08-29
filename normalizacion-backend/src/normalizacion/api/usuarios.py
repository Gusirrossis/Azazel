"""Usuarios del panel: alta, verificación de credenciales y gestión.

Sigue el mismo patrón de acceso a datos que `claves_busqueda`: psycopg directo
contra `config.postgres_dsn`, sin ORM. La tabla la crea la migración 0008.

De la contraseña solo vive su hash argon2id (ver `contrasena`). Un usuario no se
borra por defecto: se DESACTIVA, para no perder la traza de quién hizo qué.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import psycopg

from normalizacion.api import contrasena as pwd
from normalizacion.api.roles import Rol, valido
from normalizacion.core.config import Config

_TIMEOUT = 5

_COLUMNAS = "id, usuario, nombre, rol, activo, debe_cambiar"


@dataclass(frozen=True, slots=True)
class Usuario:
    id: int
    usuario: str
    nombre: str
    rol: str
    activo: bool
    debe_cambiar: bool


class UsuarioExiste(ValueError):
    """Ya hay un usuario con ese identificador."""


class RolInvalido(ValueError):
    """El rol no es uno de los tres conocidos."""


def normalizar(usuario: str) -> str:
    """`Ana`, `ana` y ` ANA ` son la misma persona: sin esto se pueden dar de alta
    tres cuentas indistinguibles a simple vista, que es una forma silenciosa de
    suplantación."""
    return usuario.strip().lower()


def _conectar(config: Config) -> psycopg.Connection:
    return psycopg.connect(config.postgres_dsn, connect_timeout=_TIMEOUT)


def _fila_a_usuario(f: tuple[Any, ...]) -> Usuario:
    return Usuario(id=f[0], usuario=f[1], nombre=f[2], rol=f[3], activo=f[4], debe_cambiar=f[5])


def hay_alguno(config: Config) -> bool:
    """¿Existe al menos un usuario? Si no, la API avisa en el arranque: un panel sin
    usuarios no se puede administrar, y NO se crea uno por defecto — una contraseña
    conocida de antemano es peor que no tener acceso."""
    with _conectar(config) as conn:
        fila = conn.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone()
    return fila is not None


_TTL_CACHE_S = 5.0
#: Tres estados, no dos: `None` = todavía no se sabe, y NO es lo mismo que `False`.
#: Confundirlos es lo que abría la puerta: un fallo de la BD dejaba un `False` en el
#: caché que las peticiones siguientes leían como "instalación sin usuarios" y que
#: `_autorizar` traduce a rol admin sin credencial.
_veredicto: bool | None = None
#: Momento del último veredicto CONFIRMADO. Solo se sella cuando la consulta
#: devolvió algo; sellarlo antes hacía que un fallo armara una ventana de 5 s.
_ultima_consulta: float = 0.0
#: Serializa la sonda. Sin él, en un arranque en frío con la BD SANA la primera
#: petición se queda cientos de ms consultando mientras las concurrentes deciden con
#: el estado previo — y el front lanza varias peticiones a la vez al cargar.
_candado = threading.Lock()


def hay_alguno_cacheado(config: Config) -> bool:
    """Igual que `hay_alguno` pero apto para el camino caliente: lo consulta cada
    request la autorización, y una consulta a la BD por request sobraría.

    Falla CERRADO en todos los caminos: BD caída, consulta lenta o tabla inexistente
    devuelven True ("hay usuarios" → exige credencial). Quedarse cerrado de más se
    arregla reintentando; abrirse de menos concede administración a cualquiera y no
    se arregla.

    El True se ENGANCHA: si alguien borrase todos los usuarios, la API no se reabre
    sola hasta reiniciar.
    """
    global _veredicto, _ultima_consulta
    if _veredicto is True:  # latch: ya se confirmó que hay usuarios
        return True
    with _candado:
        if _veredicto is True:
            return True
        ahora = time.monotonic()
        if _veredicto is False and ahora - _ultima_consulta < _TTL_CACHE_S:
            return False  # confirmado abierto hace poco: instalación sin configurar
        try:
            confirmado = hay_alguno(config)
        except Exception:
            # No se sella nada: la siguiente petición vuelve a intentarlo en vez de
            # heredar un veredicto que la base de datos nunca llegó a dar.
            return True
        _veredicto = confirmado
        _ultima_consulta = ahora
        return confirmado


def _reiniciar_cache_usuarios() -> None:
    """Solo para tests: devuelve el caché a su estado inicial (nada conocido)."""
    global _veredicto, _ultima_consulta
    with _candado:
        _veredicto = None
        _ultima_consulta = 0.0


def crear(
    config: Config,
    usuario: str,
    contrasena_clara: str,
    *,
    rol: Rol = "lector",
    nombre: str = "",
    debe_cambiar: bool = False,
) -> Usuario:
    """Da de alta un usuario. Valida la política ANTES de tocar la BD."""
    identificador = normalizar(usuario)
    if not identificador:
        raise ValueError("el usuario no puede estar vacío")
    if not valido(rol):
        raise RolInvalido(f"rol desconocido: {rol!r}")
    pwd.exigir_politica(contrasena_clara)

    with _conectar(config) as conn:
        existe = conn.execute(
            "SELECT 1 FROM usuarios WHERE usuario = %s", (identificador,)
        ).fetchone()
        if existe:
            raise UsuarioExiste(f"el usuario {identificador!r} ya existe")
        fila = conn.execute(
            "INSERT INTO usuarios (usuario, nombre, hash_contrasena, rol, debe_cambiar)"
            f" VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNAS}",
            (identificador, nombre.strip(), pwd.cifrar(contrasena_clara), rol, debe_cambiar),
        ).fetchone()
        conn.commit()
    assert fila is not None
    return _fila_a_usuario(fila)


def por_id(config: Config, usuario_id: int) -> Usuario | None:
    with _conectar(config) as conn:
        fila = conn.execute(
            f"SELECT {_COLUMNAS} FROM usuarios WHERE id = %s", (usuario_id,)
        ).fetchone()
    return _fila_a_usuario(fila) if fila else None


def listar(config: Config) -> list[dict[str, Any]]:
    """Para el panel de administración. Nunca incluye el hash."""
    with _conectar(config) as conn:
        filas = conn.execute(
            f"SELECT {_COLUMNAS}, creado_en, ultimo_acceso FROM usuarios ORDER BY usuario"
        ).fetchall()
    return [
        {
            "id": f[0],
            "usuario": f[1],
            "nombre": f[2],
            "rol": f[3],
            "activo": f[4],
            "debe_cambiar": f[5],
            "creado_en": f[6].isoformat() if f[6] else None,
            "ultimo_acceso": f[7].isoformat() if f[7] else None,
        }
        for f in filas
    ]


def verificar_credenciales(config: Config, usuario: str, contrasena_clara: str) -> Usuario | None:
    """Devuelve el usuario si las credenciales son correctas y la cuenta está activa.

    Cuando el usuario no existe se verifica igualmente contra un hash señuelo: sin
    eso, "no existe" responde en microsegundos y "contraseña mala" en ~50 ms, y esa
    diferencia deja enumerar qué cuentas hay midiendo el tiempo de respuesta.
    """
    identificador = normalizar(usuario)
    with _conectar(config) as conn:
        fila = conn.execute(
            f"SELECT {_COLUMNAS}, hash_contrasena FROM usuarios WHERE usuario = %s",
            (identificador,),
        ).fetchone()

    if fila is None:
        pwd.verificar(_hash_senuelo(), contrasena_clara)
        return None

    guardado = fila[6]
    if not pwd.verificar(guardado, contrasena_clara):
        return None
    if not fila[4]:  # activo
        return None

    # El login es el único momento en que tenemos la contraseña en claro: si el
    # hash quedó con parámetros viejos, se sube ahora y en silencio.
    if pwd.necesita_rehash(guardado):
        with _conectar(config) as conn:
            conn.execute(
                "UPDATE usuarios SET hash_contrasena = %s, actualizado_en = now() WHERE id = %s",
                (pwd.cifrar(contrasena_clara), fila[0]),
            )
            conn.commit()

    return _fila_a_usuario(fila)


def marcar_acceso(config: Config, usuario_id: int) -> None:
    with _conectar(config) as conn:
        conn.execute("UPDATE usuarios SET ultimo_acceso = now() WHERE id = %s", (usuario_id,))
        conn.commit()


def cambiar_contrasena(
    config: Config, usuario_id: int, nueva: str, *, debe_cambiar: bool = False
) -> None:
    pwd.exigir_politica(nueva)
    with _conectar(config) as conn:
        conn.execute(
            "UPDATE usuarios SET hash_contrasena = %s, debe_cambiar = %s,"
            " actualizado_en = now() WHERE id = %s",
            (pwd.cifrar(nueva), debe_cambiar, usuario_id),
        )
        conn.commit()


def actualizar(
    config: Config,
    usuario_id: int,
    *,
    rol: str | None = None,
    nombre: str | None = None,
    activo: bool | None = None,
) -> Usuario | None:
    """Cambios parciales desde el panel de administración."""
    campos: list[str] = []
    valores: list[Any] = []
    if rol is not None:
        if not valido(rol):
            raise RolInvalido(f"rol desconocido: {rol!r}")
        campos.append("rol = %s")
        valores.append(rol)
    if nombre is not None:
        campos.append("nombre = %s")
        valores.append(nombre.strip())
    if activo is not None:
        campos.append("activo = %s")
        valores.append(activo)
    if not campos:
        return por_id(config, usuario_id)

    campos.append("actualizado_en = now()")
    valores.append(usuario_id)
    with _conectar(config) as conn:
        fila = conn.execute(
            f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s RETURNING {_COLUMNAS}",
            tuple(valores),
        ).fetchone()
        conn.commit()
    return _fila_a_usuario(fila) if fila else None


class UltimoAdmin(RuntimeError):
    """El cambio dejaría el panel sin ningún administrador activo."""


def actualizar_protegiendo_admins(
    config: Config,
    usuario_id: int,
    *,
    rol: str | None = None,
    nombre: str | None = None,
    activo: bool | None = None,
    exigir_otro_admin: bool = False,
) -> Usuario | None:
    """Como `actualizar`, pero comprobando en la MISMA transacción que quede un admin.

    La comprobación y el UPDATE tienen que ir juntos y con las filas bloqueadas. Hechos
    por separado eran un TOCTOU clásico: dos peticiones concurrentes que degradan a los
    dos únicos admins leen ambas "queda otro", las dos pasan el guard, y el panel se
    queda sin ninguno. Recuperarse de eso exige entrar a Postgres a mano.

    El `FOR UPDATE` sobre los admins activos serializa: la segunda transacción espera y
    vuelve a contar sobre el estado ya cambiado por la primera.
    """
    if rol is not None and not valido(rol):
        raise RolInvalido(f"rol desconocido: {rol!r}")

    with _conectar(config) as conn:
        if exigir_otro_admin:
            otros = conn.execute(
                "SELECT count(*) FROM ("
                "  SELECT id FROM usuarios WHERE rol = 'admin' AND activo AND id <> %s"
                "  FOR UPDATE"
                ") t",
                (usuario_id,),
            ).fetchone()
            if not otros or int(otros[0]) == 0:
                conn.rollback()
                raise UltimoAdmin(
                    "Es el único admin activo: asciende a otro antes de cambiarlo."
                )

        campos: list[str] = []
        valores: list[Any] = []
        if rol is not None:
            campos.append("rol = %s")
            valores.append(rol)
        if nombre is not None:
            campos.append("nombre = %s")
            valores.append(nombre.strip())
        if activo is not None:
            campos.append("activo = %s")
            valores.append(activo)
        if not campos:
            fila = conn.execute(
                f"SELECT {_COLUMNAS} FROM usuarios WHERE id = %s", (usuario_id,)
            ).fetchone()
            return _fila_a_usuario(fila) if fila else None

        campos.append("actualizado_en = now()")
        valores.append(usuario_id)
        fila = conn.execute(
            f"UPDATE usuarios SET {', '.join(campos)} WHERE id = %s RETURNING {_COLUMNAS}",
            tuple(valores),
        ).fetchone()
        conn.commit()
    return _fila_a_usuario(fila) if fila else None


def contar_admins_activos(config: Config, *, excepto: int | None = None) -> int:
    """Cuántos admin activos quedan. Sirve para no permitir que el último admin se
    desactive o se degrade a sí mismo y deje el panel sin quién lo administre."""
    sql = "SELECT count(*) FROM usuarios WHERE rol = 'admin' AND activo"
    params: tuple[Any, ...] = ()
    if excepto is not None:
        sql += " AND id <> %s"
        params = (excepto,)
    with _conectar(config) as conn:
        fila = conn.execute(sql, params).fetchone()
    return int(fila[0]) if fila else 0


_senuelo: str | None = None


def _hash_senuelo() -> str:
    """Hash de una contraseña fija, calculado una sola vez y en el primer uso (no al
    importar: argon2 tarda ~50 ms a propósito y eso retrasaría el arranque)."""
    global _senuelo
    if _senuelo is None:
        _senuelo = pwd.cifrar("senuelo-sin-uso-real-solo-para-igualar-tiempos")
    return _senuelo
