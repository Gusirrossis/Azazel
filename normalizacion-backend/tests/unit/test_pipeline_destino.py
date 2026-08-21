"""Tests puros del destino elegible: config efectiva + crear carpeta confinada."""

from __future__ import annotations

from pathlib import Path

import pytest

from normalizacion.core.config import Config
from normalizacion.ingesta.pipeline import (
    config_con_destino,
    crear_carpeta,
    listar_carpetas,
    resolver_workers,
    workers_auto,
)


class TestResolverWorkers:
    """¿Quién decide cuántos workers? El usuario (front/CLI) > perilla > automático."""

    def test_automatico_es_nucleos_menos_dos(self) -> None:
        import os

        assert workers_auto() == max(1, (os.cpu_count() or 4) - 2)

    def test_prioridad_front_sobre_perilla_sobre_auto(self) -> None:
        config = Config(_env_file=None)  # perilla procesos=0 (auto)
        assert resolver_workers(config, None) == workers_auto()
        config_perilla = Config(_env_file=None, worker={"procesos": 6})
        assert resolver_workers(config_perilla, None) == 6
        assert resolver_workers(config_perilla, 2) == 2  # el front manda

    def test_acotado_a_limites_sanos(self) -> None:
        """El tope duro (64) sólo se puede observar en modo `fijo`.

        En `adaptativo` —el default desde K15— lo pedido es un TECHO, no una orden:
        el gobernador acota por núcleos y por RAM libre, así que pedir 999 devuelve
        `núcleos − 2` en una máquina normal. Este test pedía 999 y exigía 64, y
        pasaba sólo en un equipo con 66+ núcleos; en cualquier otro medía el
        hardware del runner, no el tope."""
        fijo = Config(_env_file=None, recursos={"modo": "fijo"})  # type: ignore[arg-type]
        assert resolver_workers(fijo, 999) == 64
        assert resolver_workers(fijo, 1) == 1

    def test_adaptativo_jamas_supera_los_nucleos(self) -> None:
        """La otra mitad del contrato: por muchos que pida el front, el gobernador
        no compromete más procesos de los que la máquina puede sostener."""
        import os

        config = Config(_env_file=None)
        nucleos = max(1, (os.cpu_count() or 4) - 2)
        assert 1 <= resolver_workers(config, 999) <= nucleos
        assert resolver_workers(config, 1) == 1


class TestConfigConDestino:
    def test_sin_destino_devuelve_la_misma_config(self) -> None:
        config = Config(_env_file=None)
        assert config_con_destino(config, None) is config

    def test_con_destino_usa_backend_local_bajo_esa_carpeta(self, tmp_path: Path) -> None:
        config = Config(_env_file=None)  # default: minio
        efectiva = config_con_destino(config, str(tmp_path / "salida"))
        assert efectiva.almacen_backend == "local"
        assert efectiva.almacen_local_raiz == str(tmp_path / "salida" / "almacen")
        assert efectiva.almacen_frio_local_raiz == str(tmp_path / "salida" / "frio")
        # las carpetas se crean de una vez (la corrida escribe sin sorpresas)
        assert (tmp_path / "salida" / "almacen").is_dir()
        assert (tmp_path / "salida" / "frio").is_dir()

    def test_no_toca_indice_ni_cola(self, tmp_path: Path) -> None:
        config = Config(_env_file=None)
        efectiva = config_con_destino(config, str(tmp_path))
        assert efectiva.opensearch_url == config.opensearch_url
        assert efectiva.postgres_dsn == config.postgres_dsn


class TestCrearCarpeta:
    def test_crea_y_es_idempotente(self, tmp_path: Path) -> None:
        nueva = crear_carpeta(str(tmp_path), "salida-junio", raiz=str(tmp_path))
        assert Path(nueva).is_dir()
        assert crear_carpeta(str(tmp_path), "salida-junio", raiz=str(tmp_path)) == nueva
        assert "salida-junio" in listar_carpetas(str(tmp_path), str(tmp_path))["carpetas"]

    def test_nombre_no_puede_escapar(self, tmp_path: Path) -> None:
        for malicioso in ("..", "a/b", "a\\b", "c:"):
            with pytest.raises(ValueError):
                crear_carpeta(str(tmp_path), malicioso, raiz=str(tmp_path))

    def test_padre_fuera_de_la_raiz_se_rechaza(self, tmp_path: Path) -> None:
        raiz = tmp_path / "permitida"
        raiz.mkdir()
        with pytest.raises(ValueError):
            crear_carpeta(str(tmp_path), "x", raiz=str(raiz))
