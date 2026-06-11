"""Integración del orquestador: una carpeta entra → 6 fases con métricas registradas.

Incluye el caso CARPETA VIVA: siguen entrando archivos y re-ejecutar solo
procesa lo nuevo (idempotencia + re-scan incremental del diseño).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.ingesta.pipeline import (
    consultar_estado,
    destinos,
    ejecutar_corrida,
    iniciar_corrida,
    listar_carpetas,
)

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str, tmp_path: Path) -> Config:
    return Config(
        _env_file=None,
        postgres_dsn=dsn,
        almacen_backend="local",
        almacen_local_raiz=str(tmp_path / "almacen"),
        almacen_frio_local_raiz=str(tmp_path / "frio"),
    )


def _correr(config: Config, carpeta: Path) -> list[dict[str, Any]]:
    corrida_id, disco_id = iniciar_corrida(config, carpeta)
    return ejecutar_corrida(config, corrida_id, carpeta.resolve(), disco_id, usar_indice=False)


class TestCorridaCompleta:
    def test_seis_fases_con_metricas(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        carpeta = tmp_path / "entrada"
        shutil.copytree(disco_sintetico, carpeta)
        fases = _correr(config, carpeta)

        assert [f["fase"] for f in fases] == [
            "catalogo",
            "precalificacion",
            "worker",
            "mover_frio",
            "verificacion",
            "puerta",
        ]
        assert all(f["duracion_s"] >= 0 for f in fases)
        metricas = {f["fase"]: f["metricas"] for f in fases}
        assert metricas["catalogo"]["archivos_vistos"] == 20
        assert metricas["precalificacion"]["hot"] == 15
        assert metricas["worker"]["procesados"] == 15
        assert metricas["mover_frio"]["movidos"] == 8
        assert metricas["verificacion"]["verificados"] == 15
        assert metricas["puerta"]["seguro_para_desechar"] is True

        estado = consultar_estado(config)
        assert estado["en_curso"] is None
        assert estado["historial"][0]["estado"] == "COMPLETADA"
        assert estado["historial"][0]["seguro_para_desechar"] is True
        assert "originales_hot" in estado["destinos"]

    def test_carpeta_viva_solo_procesa_lo_nuevo(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """LA PREGUNTA CLAVE: ¿puedo seguir metiendo datos a la carpeta? SÍ —
        la segunda corrida solo toma el archivo nuevo (incremental, sin duplicar)."""
        carpeta = tmp_path / "entrada"
        shutil.copytree(disco_sintetico, carpeta)
        _correr(config, carpeta)

        # Llegan datos nuevos a la carpeta viva…
        (carpeta / "datos" / "nuevo_reporte.csv").write_bytes(b"a,b,c\n1,2,3\n4,5,6\n")

        fases = {f["fase"]: f["metricas"] for f in _correr(config, carpeta)}
        assert fases["catalogo"]["archivos_vistos"] == 21
        assert fases["catalogo"]["archivos_nuevos"] == 1  # SOLO el nuevo
        assert fases["precalificacion"]["procesados"] == 1
        assert fases["worker"]["procesados"] == 1
        assert fases["puerta"]["seguro_para_desechar"] is True

        total = conexion.execute("SELECT COUNT(*) FROM archivos").fetchone()[0]
        assert total == 24  # 23 de la 1a corrida + 1 nuevo; cero duplicados

    def test_destino_elegido_desde_el_front(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """UX: la carpeta de destino se elige por corrida — el almacén HOT y el
        frío viven bajo ella, y la corrida registra cuál fue."""
        from normalizacion.ingesta.pipeline import config_con_destino

        carpeta = tmp_path / "entrada"
        shutil.copytree(disco_sintetico, carpeta)
        destino = tmp_path / "salida-elegida"

        cfg_corrida = config_con_destino(config, str(destino))
        corrida_id, disco_id = iniciar_corrida(cfg_corrida, carpeta, destino=str(destino))
        fases = ejecutar_corrida(
            cfg_corrida, corrida_id, carpeta.resolve(), disco_id, usar_indice=False
        )
        metricas = {f["fase"]: f["metricas"] for f in fases}
        assert metricas["puerta"]["seguro_para_desechar"] is True

        # Los blobs quedaron BAJO la carpeta elegida (no en el destino del .env)
        blobs_hot = list((destino / "almacen").rglob("*"))
        blobs_frio = list((destino / "frio").rglob("*"))
        assert any(b.is_file() for b in blobs_hot)
        assert any(b.is_file() for b in blobs_frio)
        assert not (tmp_path / "almacen").exists()  # el default ni se creó

        # La corrida registra el destino (visible en el front y el historial)
        estado = consultar_estado(config)
        assert estado["historial"][0]["destino"] == str(destino)

    def test_workers_en_paralelo_por_procesos(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """UX: el nº de workers se elige por corrida (front/CLI). 2 PROCESOS reales
        reparten la cola sin duplicar y la corrida agrega sus métricas."""
        carpeta = tmp_path / "entrada"
        shutil.copytree(disco_sintetico, carpeta)
        corrida_id, disco_id = iniciar_corrida(config, carpeta)
        fases = ejecutar_corrida(
            config, corrida_id, carpeta.resolve(), disco_id, usar_indice=False, workers=2
        )
        metricas = {f["fase"]: f["metricas"] for f in fases}
        assert metricas["worker"]["procesos"] == 2
        assert metricas["worker"]["procesados"] == 15  # entre AMBOS, sin duplicar
        assert metricas["puerta"]["seguro_para_desechar"] is True

    def test_una_corrida_a_la_vez(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        carpeta = tmp_path / "entrada"
        shutil.copytree(disco_sintetico, carpeta)
        iniciar_corrida(config, carpeta)  # queda EN_CURSO (no la ejecutamos)
        with pytest.raises(RuntimeError, match="en curso"):
            iniciar_corrida(config, carpeta)

    def test_carpeta_inexistente_falla_claro(self, config: Config, conexion: Any) -> None:
        with pytest.raises(ValueError, match="no es una carpeta"):
            iniciar_corrida(config, Path("/no/existe/jamas"))


class TestExploradorCarpetas:
    def test_lista_subcarpetas(self, tmp_path: Path) -> None:
        (tmp_path / "datos").mkdir()
        (tmp_path / "fotos").mkdir()
        (tmp_path / ".oculta").mkdir()
        (tmp_path / "archivo.txt").write_text("x")
        r = listar_carpetas(str(tmp_path))
        assert r["carpetas"] == ["datos", "fotos"]  # ni ocultas ni archivos
        assert r["padre"] == str(tmp_path.parent)

    def test_sin_ruta_es_home(self) -> None:
        r = listar_carpetas(None)
        assert r["ruta"] == str(Path.home())

    def test_confinado_a_la_raiz(self, tmp_path: Path) -> None:
        """Con raíz (Docker: /datos), NO se puede navegar fuera de ella."""
        (tmp_path / "permitida").mkdir()
        r = listar_carpetas(None, raiz=str(tmp_path))
        assert r["ruta"] == str(tmp_path)
        assert r["padre"] is None  # en el tope no hay "subir"

    def test_escapar_de_la_raiz_falla(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="fuera de la carpeta permitida"):
            listar_carpetas(str(tmp_path.parent), raiz=str(tmp_path))
        with pytest.raises(ValueError, match="fuera de la carpeta permitida"):
            listar_carpetas(str(tmp_path / ".." / ".."), raiz=str(tmp_path))


class TestPreservadosSinExplorar:
    def test_inventario_visible(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """UX: lo preservado sin explorar (cifrado/corrupto/guards) se ve de un
        vistazo — la bomba sintética debe aparecer en el inventario."""
        from normalizacion.ingesta.pipeline import preservados_sin_explorar

        carpeta = tmp_path / "entrada"
        shutil.copytree(disco_sintetico, carpeta)
        _correr(config, carpeta)

        r = preservados_sin_explorar(config)
        assert r["total"] >= 1
        assert "zip_bomb_sospechoso:guard_ratio" in r["por_motivo"]
        bomba = next(a for a in r["archivos"] if a["nombre"] == "bomba.zip")
        assert bomba["estado"] == "COLD"  # preservada en frío, jamás perdida


class TestDestinos:
    def test_destinos_visibles(self, config: Config) -> None:
        """El usuario VE dónde queda todo (su pregunta de '¿dónde se guarda?')."""
        d = destinos(config)
        assert set(d) == {"originales_hot", "frio_reversible", "indice_metadatos", "cola_estado"}
        assert "almacen" in d["originales_hot"]
