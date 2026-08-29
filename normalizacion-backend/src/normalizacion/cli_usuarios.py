"""Subcomandos `norm usuarios …` — administración de cuentas desde la terminal.

Existe sobre todo por el problema del huevo y la gallina: el panel exige un usuario
para entrar, así que el PRIMERO tiene que crearse desde fuera. También sirve para
recuperar el acceso cuando el único admin se queda fuera.

La contraseña nunca se pasa como argumento: se pide por `prompt`, oculta. Un
argumento acaba en el historial del shell y en la lista de procesos de la máquina.
"""

from __future__ import annotations

import typer

from normalizacion.core.config import cargar_config

app = typer.Typer(name="usuarios", help="Alta y administración de usuarios del panel.")


@app.command("crear")
def crear(
    usuario: str = typer.Argument(help="Identificador para entrar (se guarda en minúsculas)"),
    rol: str = typer.Option("lector", help="lector | operador | admin"),
    nombre: str = typer.Option("", help="Nombre visible en el panel"),
    debe_cambiar: bool = typer.Option(
        False, help="Obliga a cambiar la contraseña en el primer acceso"
    ),
) -> None:
    """Crea un usuario. Pide la contraseña por teclado, sin eco."""
    from normalizacion.api import usuarios
    from normalizacion.api.contrasena import ContrasenaDebil
    from normalizacion.api.roles import valido

    if not valido(rol):
        typer.secho(f"Rol desconocido: {rol!r}. Usa lector, operador o admin.", fg="red")
        raise typer.Exit(code=2)

    config = cargar_config()
    contrasena = typer.prompt("Contraseña", hide_input=True, confirmation_prompt=True)

    try:
        creado = usuarios.crear(
            config, usuario, contrasena, rol=rol, nombre=nombre, debe_cambiar=debe_cambiar
        )
    except usuarios.UsuarioExiste:
        typer.secho(
            f"El usuario {usuario!r} ya existe. Usa `norm usuarios contrasena` para cambiarla.",
            fg="red",
        )
        raise typer.Exit(code=1) from None
    except ContrasenaDebil as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(code=2) from None

    typer.secho(f"Usuario {creado.usuario!r} creado con rol {creado.rol}.", fg="green")


@app.command("listar")
def listar() -> None:
    """Lista los usuarios con su rol y último acceso."""
    from normalizacion.api import usuarios

    filas = usuarios.listar(cargar_config())
    if not filas:
        typer.secho(
            "No hay usuarios. Crea el primero: `norm usuarios crear <usuario> --rol admin`",
            fg="yellow",
        )
        return
    typer.echo(f"{'USUARIO':<24} {'ROL':<10} {'ACTIVO':<7} ÚLTIMO ACCESO")
    for f in filas:
        marca = "sí" if f["activo"] else "no"
        ultimo = (f["ultimo_acceso"] or "—")[:19].replace("T", " ")
        typer.echo(f"{f['usuario']:<24} {f['rol']:<10} {marca:<7} {ultimo}")


@app.command("contrasena")
def contrasena(
    usuario: str = typer.Argument(help="Usuario al que cambiarle la contraseña"),
) -> None:
    """Cambia la contraseña de un usuario y cierra todas sus sesiones.

    La vía de recuperación cuando alguien se queda fuera. Cerrar las sesiones es
    deliberado: si se cambia porque la cuenta pudo quedar expuesta, dejar abiertas
    las sesiones que ya había no resuelve nada.
    """
    from normalizacion.api import sesiones, usuarios
    from normalizacion.api.contrasena import ContrasenaDebil

    config = cargar_config()
    encontrado = _buscar(config, usuario)
    nueva = typer.prompt("Contraseña nueva", hide_input=True, confirmation_prompt=True)
    try:
        usuarios.cambiar_contrasena(config, encontrado["id"], nueva)
    except ContrasenaDebil as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(code=2) from None
    cerradas = sesiones.revocar_todas(config, encontrado["id"])
    typer.secho(
        f"Contraseña de {encontrado['usuario']!r} cambiada; {cerradas} sesión(es) cerrada(s).",
        fg="green",
    )


@app.command("rol")
def rol(
    usuario: str = typer.Argument(help="Usuario a modificar"),
    nuevo: str = typer.Argument(help="lector | operador | admin"),
) -> None:
    """Cambia el rol de un usuario."""
    from normalizacion.api import usuarios
    from normalizacion.api.roles import valido

    if not valido(nuevo):
        typer.secho(f"Rol desconocido: {nuevo!r}.", fg="red")
        raise typer.Exit(code=2)

    config = cargar_config()
    encontrado = _buscar(config, usuario)
    _proteger_ultimo_admin(config, encontrado, degrada=nuevo != "admin")
    usuarios.actualizar(config, encontrado["id"], rol=nuevo)
    typer.secho(f"{encontrado['usuario']!r} ahora es {nuevo}.", fg="green")


@app.command("desactivar")
def desactivar(
    usuario: str = typer.Argument(help="Usuario a desactivar"),
) -> None:
    """Desactiva un usuario y corta sus sesiones. No lo borra: la cuenta desactivada
    conserva la traza de lo que hizo."""
    from normalizacion.api import sesiones, usuarios

    config = cargar_config()
    encontrado = _buscar(config, usuario)
    _proteger_ultimo_admin(config, encontrado, degrada=True)
    usuarios.actualizar(config, encontrado["id"], activo=False)
    cerradas = sesiones.revocar_todas(config, encontrado["id"])
    typer.secho(
        f"{encontrado['usuario']!r} desactivado; {cerradas} sesión(es) cerrada(s).", fg="green"
    )


@app.command("activar")
def activar(usuario: str = typer.Argument(help="Usuario a reactivar")) -> None:
    """Vuelve a habilitar un usuario desactivado."""
    from normalizacion.api import usuarios

    config = cargar_config()
    encontrado = _buscar(config, usuario)
    usuarios.actualizar(config, encontrado["id"], activo=True)
    typer.secho(f"{encontrado['usuario']!r} reactivado.", fg="green")


def _buscar(config: object, usuario: str) -> dict:
    """Busca por identificador y corta con un mensaje claro si no está."""
    from normalizacion.api import usuarios

    objetivo = usuarios.normalizar(usuario)
    for f in usuarios.listar(config):  # type: ignore[arg-type]
        if f["usuario"] == objetivo:
            return f
    typer.secho(f"No existe el usuario {objetivo!r}.", fg="red")
    raise typer.Exit(code=1)


def _proteger_ultimo_admin(config: object, encontrado: dict, *, degrada: bool) -> None:
    """Impide dejar el panel sin ningún admin activo. Recuperarse de eso obliga a
    entrar a la base de datos a mano, así que se corta antes."""
    from normalizacion.api import usuarios

    if not degrada or encontrado["rol"] != "admin" or not encontrado["activo"]:
        return
    if usuarios.contar_admins_activos(config, excepto=encontrado["id"]) == 0:  # type: ignore[arg-type]
        typer.secho(
            "Es el único admin activo. Asciende a otro usuario antes de degradar este.",
            fg="red",
        )
        raise typer.Exit(code=2)
