"""Claves de búsqueda con nombre: generar/verificar/rotar/revocar contra `control` real."""

from __future__ import annotations

from normalizacion.api import claves_busqueda
from normalizacion.core.config import Config


def _config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


def test_flujo_generar_verificar_revocar(dsn: str, esquema: None) -> None:
    cfg = _config(dsn)
    # invalida cualquier cache de una prueba previa
    claves_busqueda._cache = None

    # Sin claves configuradas: canal ABIERTO (dev).
    assert claves_busqueda.autorizada(cfg, None) is True
    assert claves_busqueda.listar_claves(cfg) == []

    # Genero una clave con nombre; se devuelve el texto UNA vez.
    clave = claves_busqueda.generar_clave(cfg, "reddoor")
    assert clave.startswith("bus_") and len(clave) > 20

    # Ahora el canal está CERRADO: exige clave válida.
    assert claves_busqueda.autorizada(cfg, clave) is True
    assert claves_busqueda.autorizada(cfg, "bus_incorrecta") is False
    assert claves_busqueda.autorizada(cfg, None) is False

    # Listar no revela el secreto ni el hash.
    lista = claves_busqueda.listar_claves(cfg)
    assert [c["nombre"] for c in lista] == ["reddoor"]
    assert "hash" not in lista[0] and "clave" not in lista[0]

    # Rotar reemplaza la clave del mismo nombre (la vieja deja de valer).
    clave2 = claves_busqueda.generar_clave(cfg, "reddoor")
    assert clave2 != clave
    assert claves_busqueda.autorizada(cfg, clave2) is True
    assert claves_busqueda.autorizada(cfg, clave) is False
    assert len(claves_busqueda.listar_claves(cfg)) == 1  # no duplica

    # Una segunda clave con otro nombre coexiste; revocar una no toca la otra.
    clave_flux = claves_busqueda.generar_clave(cfg, "flux")
    assert claves_busqueda.autorizada(cfg, clave_flux) is True
    assert claves_busqueda.revocar_clave(cfg, "reddoor") is True
    assert claves_busqueda.autorizada(cfg, clave2) is False
    assert claves_busqueda.autorizada(cfg, clave_flux) is True  # flux sigue

    # Revocar algo inexistente = False.
    assert claves_busqueda.revocar_clave(cfg, "no-existe") is False

    # Limpieza: al quitar la última, vuelve a abrirse (dev).
    assert claves_busqueda.revocar_clave(cfg, "flux") is True
    assert claves_busqueda.autorizada(cfg, None) is True
