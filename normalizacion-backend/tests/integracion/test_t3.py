"""Integración T3 (DoD Fase 1.5): cajas dentro de cajas vía BFS y zip-bomb a COLD."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.config import Config, PerillasFiltro
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


class TestT3SobreElDiscoSintetico:
    @pytest.fixture()
    def drenado(self, config: Config, conexion: Any, disco_sintetico: Path) -> Any:
        catalogar_disco(config, disco_sintetico, disco_id="disco-test")
        self.resumen = precalificar_pendientes(config)
        return conexion

    def test_desglose_con_entradas_internas(self, drenado: Any) -> None:
        """20 del disco + 3 internas (leeme.txt, caja2.zip, datos_internos.csv) = 23."""
        assert self.resumen.procesados == 23
        assert self.resumen.re_encolados == 3
        assert self.resumen.errores == 0
        assert self.resumen.hot == 15
        assert self.resumen.cold == 8

    def test_zip_bomb_a_cold_con_flag(self, drenado: Any) -> None:
        """INVARIANTE: la bomba no cuelga al worker — COLD con guard señalado."""
        fila = drenado.execute(
            "SELECT estado, motivo, senales->>'guard_violado' FROM archivos"
            " WHERE nombre = 'bomba.zip'"
        ).fetchone()
        assert fila == ("COLD", "zip_bomb_sospechoso:guard_ratio", "guard_ratio")

    def test_contenedor_explorado_preservado_en_hot(self, drenado: Any) -> None:
        fila = drenado.execute(
            "SELECT ruta_decision, motivo, senales->>'entradas_re_encoladas'"
            " FROM archivos WHERE nombre = 'cajas.zip'"
        ).fetchone()
        assert fila == ("HOT", "contenedor_explorado", "2")

    def test_caja_interna_recorrida_en_bfs(self, drenado: Any) -> None:
        """caja2.zip (dentro de cajas.zip) también fue explorada como fila propia."""
        fila = drenado.execute(
            "SELECT ruta, motivo, origen_contenedor->>'profundidad' FROM archivos"
            " WHERE nombre = 'caja2.zip'"
        ).fetchone()
        assert fila == ("contenedores/cajas.zip!caja2.zip", "contenedor_explorado", "1")

    def test_csv_interno_llega_a_hot_con_su_path_spec(self, drenado: Any) -> None:
        """El CSV en el fondo de las cajas se puntúa individual (doc §1: granularidad)."""
        fila = drenado.execute(
            "SELECT ruta, ruta_decision, tipo_real, origen_contenedor->'cadena',"
            "       origen_contenedor->>'profundidad'"
            " FROM archivos WHERE nombre = 'datos_internos.csv'"
        ).fetchone()
        assert fila[0] == "contenedores/cajas.zip!caja2.zip!datos_internos.csv"
        assert fila[1] == "HOT"
        assert fila[2] == "text/csv"
        assert fila[3] == ["contenedores/cajas.zip", "caja2.zip", "datos_internos.csv"]
        assert fila[4] == "2"

    def test_nada_queda_pendiente_tras_el_bfs(self, drenado: Any) -> None:
        pendientes = drenado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'PENDIENTE'"
        ).fetchone()[0]
        assert pendientes == 0


class TestTopeDeProfundidad:
    def test_anidacion_mas_alla_del_tope_va_a_cold(
        self, dsn: str, conexion: Any, tmp_path: Path
    ) -> None:
        """Matryoshka de 3 niveles con tope=1: el nivel 1 ya no se explora (⚙K4)."""
        import io

        nivel2 = io.BytesIO()
        with zipfile.ZipFile(nivel2, "w") as zf:
            zf.writestr("x.csv", "a,b\n1,2\n")
        nivel1 = io.BytesIO()
        with zipfile.ZipFile(nivel1, "w") as zf:
            zf.writestr("nivel2.zip", nivel2.getvalue())
        disco = tmp_path / "disco"
        disco.mkdir()
        with zipfile.ZipFile(disco / "nivel0.zip", "w") as zf:
            zf.writestr("nivel1.zip", nivel1.getvalue())

        config = Config(
            _env_file=None, postgres_dsn=dsn, filtro=PerillasFiltro(t3_profundidad_max=1)
        )
        catalogar_disco(config, disco, disco_id="matryoshka")
        precalificar_pendientes(config)

        nivel0 = conexion.execute(
            "SELECT motivo FROM archivos WHERE nombre = 'nivel0.zip'"
        ).fetchone()
        assert nivel0 == ("contenedor_explorado",)
        nivel1_fila = conexion.execute(
            "SELECT estado, motivo, senales->>'profundidad' FROM archivos"
            " WHERE nombre = 'nivel1.zip'"
        ).fetchone()
        assert nivel1_fila == ("COLD", "profundidad_maxima", "1")
        # nivel2 jamás se materializó: el tope corta ANTES de explorar
        nivel2_fila = conexion.execute(
            "SELECT COUNT(*) FROM archivos WHERE nombre = 'nivel2.zip'"
        ).fetchone()
        assert nivel2_fila == (0,)
