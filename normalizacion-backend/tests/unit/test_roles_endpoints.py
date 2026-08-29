"""Contrato: cada endpoint exige el rol que le toca.

Se lee el AST de `api/main.py` en vez de levantar la app porque así el test no
necesita ni Postgres ni OpenSearch, y sobre todo porque el fallo que importa es de
OMISIÓN: alguien añade mañana un endpoint destructivo y le pone `Autorizado` por
copiar el de al lado. Un test que solo comprobara las rutas de hoy no lo vería;
este obliga a declarar el rol de TODA ruta nueva.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parents[2] / "src" / "normalizacion" / "api" / "main.py"

# ruta + método → rol mínimo exigido.
#
# `lector` es el suelo: autenticado, sin poder cambiar nada. `operador` OPERA sobre
# los datos (lanza corridas, reprocesa, edita el filtro). `admin` GOBIERNA el
# sistema (usuarios, claves, recetas, recursos).
ESPERADO: dict[tuple[str, str], str] = {
    # --- gobierno: solo admin ---
    ("/seguridad/claves-busqueda", "get"): "admin",
    ("/seguridad/claves-busqueda", "post"): "admin",
    ("/seguridad/claves-busqueda/{nombre}", "delete"): "admin",
    ("/auth/usuarios", "get"): "admin",
    ("/auth/usuarios", "post"): "admin",
    ("/auth/usuarios/{usuario_id}", "put"): "admin",
    ("/sistema/recursos", "put"): "admin",
    ("/entidades/recetas/{clave}", "put"): "admin",
    ("/entidades/recetas/{clave}", "delete"): "admin",
    ("/entidades/config/atributos", "put"): "admin",
    ("/entidades/config/destino", "get"): "admin",
    ("/entidades/config/destino", "put"): "admin",
    # --- operación: operador ---
    ("/pipeline/ejecutar", "post"): "operador",
    ("/sistema/carpetas", "post"): "operador",
    ("/cola/reprocesar-errores", "post"): "operador",
    ("/cola/rescore-frio", "post"): "operador",
    ("/cola/reexplorar-preservados", "post"): "operador",
    ("/entidades/backfill", "post"): "operador",
    ("/entidades/enviar", "post"): "operador",
    ("/entidades/mapeo/proponer", "post"): "operador",
    ("/entidades/proyectar", "post"): "operador",
    ("/entidades/{entidad_id}/activo", "post"): "operador",
    ("/filtro", "put"): "operador",
    ("/filtro", "delete"): "operador",
}

# Rutas sin autenticación, a propósito y con motivo.
SIN_AUTENTICAR: dict[tuple[str, str], str] = {
    ("/auth/login", "post"): "es la puerta: exigir sesión para iniciar sesión no cierra",
    ("/auth/logout", "post"): "cerrar sesión con una cookie ya vencida debe funcionar",
}

# `Identificado` = autenticado pero SIN el corte por `debe_cambiar`, para los tres
# endpoints con los que una cuenta recién creada sale de ese estado. Cuenta como
# autenticado a efectos de este contrato, pero no exige rol.
_ANOTACION_A_ROL = {
    "Autorizado": "lector",
    "Identificado": "identificado",
    "Operador": "operador",
    "Admin": "admin",
}


def _rutas() -> dict[tuple[str, str], str | None]:
    """(ruta, método) → rol exigido, o None si el endpoint no pide autenticación."""
    arbol = ast.parse(MAIN.read_text(encoding="utf-8"))
    encontradas: dict[tuple[str, str], str | None] = {}

    for nodo in ast.walk(arbol):
        # AsyncFunctionDef tambien: un endpoint `async def` no es un FunctionDef, asi
        # que el parseo lo ignoraba por completo — y un endpoint sin autenticar
        # declarado asi habria pasado este contrato en verde.
        if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in nodo.decorator_list:
            # @aplicacion.<metodo>("<ruta>", ...)
            if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                continue
            if not isinstance(deco.func.value, ast.Name) or deco.func.value.id != "aplicacion":
                continue
            metodo = deco.func.attr
            if metodo not in ("get", "post", "put", "delete"):
                continue
            if not deco.args or not isinstance(deco.args[0], ast.Constant):
                continue
            ruta = deco.args[0].value

            rol: str | None = None
            for arg in [*nodo.args.args, *nodo.args.kwonlyargs]:
                nombre = getattr(arg.annotation, "id", None)
                if nombre in _ANOTACION_A_ROL:
                    rol = _ANOTACION_A_ROL[nombre]
                    break
            encontradas[(ruta, metodo)] = rol
    return encontradas


@pytest.fixture(scope="module")
def rutas() -> dict[tuple[str, str], str | None]:
    return _rutas()


def test_se_encontraron_rutas(rutas: dict[tuple[str, str], str | None]) -> None:
    """Red de seguridad del propio test: si el parseo dejara de reconocer los
    decoradores, todo lo demás pasaría en verde sin comprobar nada."""
    assert len(rutas) > 30, f"solo se reconocieron {len(rutas)} rutas; ¿cambió el decorador?"


@pytest.mark.parametrize(("clave", "minimo"), sorted(ESPERADO.items()))
def test_cada_endpoint_exige_su_rol(
    rutas: dict[tuple[str, str], str | None], clave: tuple[str, str], minimo: str
) -> None:
    assert clave in rutas, f"{clave[1].upper()} {clave[0]} ya no existe: actualiza la tabla"
    assert rutas[clave] == minimo, (
        f"{clave[1].upper()} {clave[0]} exige {rutas[clave]!r} y debería exigir {minimo!r}"
    )


def test_toda_ruta_pide_autenticacion(rutas: dict[tuple[str, str], str | None]) -> None:
    """Ninguna ruta queda abierta salvo las declaradas arriba con su motivo."""
    abiertas = {c for c, rol in rutas.items() if rol is None} - set(SIN_AUTENTICAR)
    assert not abiertas, f"rutas sin autenticación no declaradas: {sorted(abiertas)}"


def test_ninguna_escritura_se_queda_en_lector(rutas: dict[tuple[str, str], str | None]) -> None:
    """El fallo que este archivo existe para atrapar: un endpoint que MODIFICA algo
    y se quedó con el `Autorizado` de por defecto, accesible para cualquier lector
    —incluida una clave de consumidor externo."""
    flojas = [
        c
        for c, rol in rutas.items()
        if c[1] in ("post", "put", "delete")
        and rol in ("lector", "identificado")
        and c not in SIN_AUTENTICAR
    ]
    permitidas = {
        # POST porque la consulta viaja en el CUERPO (filtros, facetas, cursor), no
        # porque escriba nada: `/buscar` es de solo lectura y tiene que estar al
        # alcance de un lector — es la razón de ser del rol.
        ("/buscar", "post"),
        # Cualquiera gestiona lo SUYO propio: su contraseña y sus sesiones. Lo que no
        # puede es tocar las de los demás, y eso vive en `/auth/usuarios` (admin).
        ("/auth/contrasena", "post"),
        ("/auth/sesiones", "delete"),
    }
    inesperadas = [c for c in flojas if c not in permitidas]
    assert not inesperadas, (
        f"estos endpoints modifican datos con rol 'lector': {sorted(inesperadas)}"
    )
