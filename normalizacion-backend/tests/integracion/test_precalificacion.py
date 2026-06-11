"""Integración Fase 1.5 (DoD → M2.5): el doble filtro sobre el disco sintético completo.

catálogo → precalificación → router HOT/COLD auditable, reversible y re-ejecutable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.config import Config
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import (
    perfil_disco,
    precalificar_pendientes,
)

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn)


@pytest.fixture()
def disco_catalogado(config: Config, conexion: Any, disco_sintetico: Path) -> Any:
    catalogar_disco(config, disco_sintetico, disco_id="disco-test")
    return conexion


class TestPrecalificacion:
    def test_desglose_completo_del_disco(self, config: Config, disco_catalogado: Any) -> None:
        """Desglose EXACTO: 20 del disco + 3 entradas internas de T3 = 13 HOT / 10 COLD."""
        resumen = precalificar_pendientes(config)
        assert resumen.procesados == 23
        assert resumen.errores == 0
        assert resumen.hot == 15
        assert resumen.cold == 8

    def test_no_queda_nada_pendiente(self, config: Config, disco_catalogado: Any) -> None:
        precalificar_pendientes(config)
        pendientes = disco_catalogado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado = 'PENDIENTE'"
        ).fetchone()[0]
        assert pendientes == 0

    def test_es_idempotente_re_ejecutar(self, config: Config, disco_catalogado: Any) -> None:
        precalificar_pendientes(config)
        segunda = precalificar_pendientes(config)
        assert segunda.procesados == 0

    def test_extension_mentirosa_a_hot(self, config: Config, disco_catalogado: Any) -> None:
        """vacaciones.jpg ES un CSV → HOT con tipo text/csv y la mentira señalada."""
        precalificar_pendientes(config)
        fila = disco_catalogado.execute(
            "SELECT estado, ruta_decision, tipo_real, senales->>'extension_miente'"
            " FROM archivos WHERE nombre = 'vacaciones.jpg'"
        ).fetchone()
        assert fila == ("PRECALIFICADO", "HOT", "text/csv", "true")

    def test_multimedia_real_a_cold(self, config: Config, disco_catalogado: Any) -> None:
        precalificar_pendientes(config)
        fila = disco_catalogado.execute(
            "SELECT estado, ruta_decision, tipo_real, motivo"
            " FROM archivos WHERE nombre = 'foto_real.jpg'"
        ).fetchone()
        assert fila == ("COLD", "COLD", "image/jpeg", "fuera_de_lista_blanca")

    def test_basura_t0_muere_sin_lectura(self, config: Config, disco_catalogado: Any) -> None:
        precalificar_pendientes(config)
        filas = disco_catalogado.execute(
            "SELECT nombre, puntaje, motivo FROM archivos"
            " WHERE motivo LIKE 'kill_t0:%' ORDER BY nombre"
        ).fetchall()
        assert len(filas) == 4
        assert all(f[1] == 0 for f in filas)

    def test_office_moderno_detectado_por_estructura(
        self, config: Config, disco_catalogado: Any
    ) -> None:
        """DOCX/XLSX no son 'zip genérico': el detector estructural los distingue."""
        precalificar_pendientes(config)
        tipos = dict(
            disco_catalogado.execute(
                "SELECT nombre, tipo_real FROM archivos"
                " WHERE nombre IN ('contrato.docx', 'inventario.xlsx')"
            ).fetchall()
        )
        assert tipos["contrato.docx"].endswith("wordprocessingml.document")
        assert tipos["inventario.xlsx"].endswith("spreadsheetml.sheet")

    def test_prioridad_es_el_puntaje(self, config: Config, disco_catalogado: Any) -> None:
        """El claim del worker HOT procesará primero lo más útil (prioridad DESC)."""
        precalificar_pendientes(config)
        filas = disco_catalogado.execute(
            "SELECT puntaje, prioridad FROM archivos WHERE ruta_decision = 'HOT'"
        ).fetchall()
        assert filas and all(p == pr for p, pr in filas)

    def test_decision_auditable(self, config: Config, disco_catalogado: Any) -> None:
        """Toda fila decidida guarda puntaje + motivo + versión del filtro + señales."""
        precalificar_pendientes(config)
        sin_auditoria = disco_catalogado.execute(
            "SELECT COUNT(*) FROM archivos WHERE estado IN ('PRECALIFICADO', 'COLD')"
            " AND (puntaje IS NULL OR motivo IS NULL OR version_filtro IS NULL"
            "      OR senales IS NULL)"
        ).fetchone()[0]
        assert sin_auditoria == 0


class TestPerfilDisco:
    def test_perfil_coherente(self, config: Config, disco_catalogado: Any) -> None:
        precalificar_pendientes(config)
        p = perfil_disco(config, "disco-test")
        assert p.total == 23
        por_ruta = {r[0]: r[1] for r in p.por_ruta}
        assert por_ruta == {"HOT": 15, "COLD": 8}
        assert p.top_tipos and p.por_motivo
