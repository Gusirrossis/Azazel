"""Integración Fase 6 (DoD → M6): el operador VE el estado y PUEDE intervenir."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from normalizacion.core import cola
from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.config import Config
from normalizacion.core.observabilidad.metricas import Exportador
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
from normalizacion.ingesta.workers.orquestador import procesar_hot

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn, almacen_backend="local")


@pytest.fixture()
def catalogado(config: Config, conexion: Any, disco_sintetico: Path) -> Any:
    catalogar_disco(config, disco_sintetico, disco_id="disco-test")
    return conexion


class TestPausa:
    def test_pausa_detiene_todos_los_loops(
        self, config: Config, catalogado: Any, tmp_path: Path
    ) -> None:
        """DoD: `norm pausar` → los procesos drenan y se detienen; reanudar → siguen."""
        cola.fijar_pausa(catalogado, True)
        catalogado.commit()

        resumen = precalificar_pendientes(config)
        assert resumen.procesados == 0  # había 20 PENDIENTE y no tocó ninguno
        assert procesar_hot(config, almacen=AlmacenLocal(tmp_path / "a")).procesados == 0

        cola.fijar_pausa(catalogado, False)
        catalogado.commit()
        assert precalificar_pendientes(config).procesados == 23

    def test_pausa_es_visible_en_metricas(self, config: Config, conexion: Any) -> None:
        cola.fijar_pausa(conexion, True)
        conexion.commit()
        exportador = Exportador()
        exportador.recolectar(conexion)
        assert b"norm_pausado 1.0" in exportador.texto()


class TestReprocesarErrores:
    def test_cada_error_vuelve_a_su_etapa(self, config: Config, catalogado: Any) -> None:
        """ERROR de precalificación → PENDIENTE; del camino HOT → PRECALIFICADO;
        del movimiento a frío → COLD. Con intentos en cero."""
        precalificar_pendientes(config)
        # Fabricar errores representativos de las tres etapas
        catalogado.execute(
            "UPDATE archivos SET estado='ERROR', error_motivo='io_ilegible: x',"
            " puntaje=NULL, ruta_decision=NULL WHERE nombre = 'notas.txt'"
        )
        catalogado.execute(
            "UPDATE archivos SET estado='ERROR', error_motivo='agotado:almacen'"
            " WHERE nombre = 'ventas_2023.csv'"  # HOT
        )
        catalogado.execute(
            "UPDATE archivos SET estado='ERROR', error_motivo='agotado:io_frio'"
            " WHERE nombre = 'foto_real.jpg'"  # COLD
        )
        catalogado.commit()

        destinos = cola.reprocesar_errores(catalogado)
        catalogado.commit()
        assert destinos == {"PENDIENTE": 1, "PRECALIFICADO": 1, "COLD": 1}
        intentos = catalogado.execute(
            "SELECT MAX(intentos) FROM archivos WHERE nombre IN"
            " ('notas.txt', 'ventas_2023.csv', 'foto_real.jpg')"
        ).fetchone()[0]
        assert intentos == 0

    def test_filtro_por_motivo(self, config: Config, catalogado: Any) -> None:
        precalificar_pendientes(config)
        catalogado.execute(
            "UPDATE archivos SET estado='ERROR', error_motivo='agotado:almacen'"
            " WHERE nombre = 'ventas_2023.csv'"
        )
        catalogado.execute(
            "UPDATE archivos SET estado='ERROR', error_motivo='verificacion_fallida: x'"
            " WHERE nombre = 'clientes.csv'"
        )
        catalogado.commit()
        destinos = cola.reprocesar_errores(catalogado, motivo_como="agotado:%")
        catalogado.commit()
        assert sum(destinos.values()) == 1  # el de verificación NO se tocó
        sigue_error = catalogado.execute(
            "SELECT estado FROM archivos WHERE nombre = 'clientes.csv'"
        ).fetchone()[0]
        assert sigue_error == "ERROR"


class TestRescoreFrio:
    def test_rescore_manda_el_frio_a_repuntuarse(self, config: Config, catalogado: Any) -> None:
        """Reversibilidad del diseño: COLD → PENDIENTE → el filtro decide otra vez."""
        precalificar_pendientes(config)
        cuantos = cola.rescore_frio(catalogado, "disco-test")
        catalogado.commit()
        assert cuantos == 8

        resumen = precalificar_pendientes(config)
        assert resumen.procesados == 8  # solo el frío se re-evaluó
        estados = dict(
            catalogado.execute("SELECT estado, COUNT(*) FROM archivos GROUP BY estado").fetchall()
        )
        assert estados.get("PENDIENTE") is None  # nada quedó sin decidir


class TestMetricas:
    def test_exportador_refleja_la_cola(self, config: Config, catalogado: Any) -> None:
        precalificar_pendientes(config)
        exportador = Exportador()
        exportador.recolectar(catalogado)
        texto = exportador.texto().decode()
        assert 'norm_backlog{estado="PRECALIFICADO"} 15.0' in texto
        assert 'norm_backlog{estado="COLD"} 8.0' in texto
        assert 'norm_archivos_por_ruta{ruta_decision="HOT"} 15.0' in texto
        assert "norm_discos_pendientes 1.0" in texto

    def test_errores_agrupados_por_motivo(self, config: Config, catalogado: Any) -> None:
        precalificar_pendientes(config)
        catalogado.execute(
            "UPDATE archivos SET estado='ERROR', error_motivo='agotado:almacen caido'"
            " WHERE nombre = 'ventas_2023.csv'"
        )
        catalogado.commit()
        exportador = Exportador()
        exportador.recolectar(catalogado)
        assert b'norm_errores_por_motivo{motivo="agotado"} 1.0' in exportador.texto()
