"""Integración Fase 1 (DoD): inventario exacto, idempotencia de re-scan, reanudación."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.ingesta.catalogo.walker import catalogar_disco

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


class TestCatalogo:
    def test_inventario_exacto(self, config: Config, conexion: Any, disco_sintetico: Path) -> None:
        """DoD: catalogar produce el inventario EXACTO (el generador crea 20 archivos)."""
        resumen = catalogar_disco(config, disco_sintetico, disco_id="disco-test")
        assert resumen.archivos_vistos == 20
        assert resumen.archivos_nuevos == 20
        assert resumen.errores == 0

        total = conexion.execute(
            "SELECT COUNT(*) FROM archivos WHERE disco_id = 'disco-test'"
        ).fetchone()[0]
        assert total == 20
        total_disco = conexion.execute(
            "SELECT total_catalogado FROM discos WHERE disco_id = 'disco-test'"
        ).fetchone()[0]
        assert total_disco == 20

    def test_recatalogar_no_duplica(
        self, config: Config, conexion: Any, disco_sintetico: Path
    ) -> None:
        """DoD: re-scan idempotente — el segundo catálogo no inserta nada nuevo."""
        catalogar_disco(config, disco_sintetico, disco_id="disco-test")
        segundo = catalogar_disco(config, disco_sintetico, disco_id="disco-test")
        assert segundo.archivos_vistos == 20
        assert segundo.archivos_nuevos == 0

        total = conexion.execute("SELECT COUNT(*) FROM archivos").fetchone()[0]
        assert total == 20

    def test_todo_entra_como_pendiente(
        self, config: Config, conexion: Any, disco_sintetico: Path
    ) -> None:
        catalogar_disco(config, disco_sintetico, disco_id="disco-test")
        estados = {
            f[0] for f in conexion.execute("SELECT DISTINCT estado FROM archivos").fetchall()
        }
        assert estados == {"PENDIENTE"}

    def test_comprimidos_entran_con_prioridad(
        self, config: Config, conexion: Any, disco_sintetico: Path
    ) -> None:
        """Decisión del usuario: lo comprimido se atiende PRIMERO (hint por extensión)."""
        catalogar_disco(config, disco_sintetico, disco_id="disco-test")
        filas = dict(
            conexion.execute(
                "SELECT nombre, prioridad FROM archivos"
                " WHERE nombre IN ('cajas.zip', 'bomba.zip', 'ventas_2023.csv')"
            ).fetchall()
        )
        # La perilla POR EXTENSIÓN manda sobre el hint genérico de contenedor
        # (decisión del usuario 2026-06-11: .txt → .7z → .rar → .zip). Así lo
        # documenta `prioridad_para_extension`: "perilla por extensión > hint de
        # contenedor > 0". Este test comprobaba el hint genérico (50) y se quedó
        # atrás cuando llegó el orden por extensión.
        prioridad_zip = config.filtro.prioridad_extensiones[".zip"]
        assert prioridad_zip > config.filtro.prioridad_inicial_contenedores
        assert filas["cajas.zip"] == prioridad_zip
        assert filas["bomba.zip"] == prioridad_zip
        assert filas["ventas_2023.csv"] == 0  # los demás esperan su turno

    def test_archivo_nuevo_en_rescan_si_entra(
        self, config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path
    ) -> None:
        """Re-escaneo incremental: solo lo nuevo/cambiado genera filas nuevas."""
        import shutil

        copia = tmp_path / "disco"
        shutil.copytree(disco_sintetico, copia)
        catalogar_disco(config, copia, disco_id="disco-test")

        (copia / "datos" / "archivo_nuevo.csv").write_bytes(b"a,b\n1,2\n")
        resumen = catalogar_disco(config, copia, disco_id="disco-test")
        assert resumen.archivos_nuevos == 1
