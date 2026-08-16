"""LOS TESTS SAGRADOS (DoD Fase 2 / riesgo R1): verificación y puerta de integridad.

Si alguno de estos se rompe, el build NO pasa. La puerta jamás se abre con un solo
archivo sin poner a salvo — esa es la promesa central del sistema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from normalizacion.core.almacen import AlmacenLocal
from normalizacion.core.config import Config
from normalizacion.core.modelo import clave_almacen
from normalizacion.ingesta.catalogo.walker import catalogar_disco
from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
from normalizacion.ingesta.workers.orquestador import procesar_hot
from normalizacion.ingesta.workers.verificador import (
    evaluar_puerta,
    mover_frio,
    verificar_indexados,
)

pytestmark = pytest.mark.integracion


@pytest.fixture()
def config(dsn: str) -> Config:
    return Config(_env_file=None, postgres_dsn=dsn, almacen_backend="local")


@pytest.fixture()
def entorno(config: Config, conexion: Any, disco_sintetico: Path, tmp_path: Path) -> dict[str, Any]:
    """Pipeline hasta el worker: 15 INDEXADO + 8 COLD, listo para verificar."""
    catalogar_disco(config, disco_sintetico, disco_id="disco-test")
    precalificar_pendientes(config)
    almacen = AlmacenLocal(tmp_path / "almacen")
    procesar_hot(config, almacen=almacen)
    return {
        "conexion": conexion,
        "almacen": almacen,
        "frio": AlmacenLocal(tmp_path / "frio"),
    }


class TestVerificador:
    def test_blobs_integros_llegan_a_hecho(self, config: Config, entorno: dict[str, Any]) -> None:
        resumen = verificar_indexados(config, almacen=entorno["almacen"])
        assert resumen.verificados == 15
        assert resumen.fallidos == 0
        hechos = (
            entorno["conexion"]
            .execute("SELECT COUNT(*) FROM archivos WHERE estado = 'HECHO'")
            .fetchone()[0]
        )
        assert hechos == 15

    def test_blob_corrupto_va_a_error(self, config: Config, entorno: dict[str, Any]) -> None:
        """Corrupción silenciosa detectada: el blob alterado NO pasa la verificación."""
        fila = (
            entorno["conexion"]
            .execute("SELECT hash_contenido FROM archivos WHERE nombre = 'ventas_2023.csv'")
            .fetchone()
        )
        ruta_blob = entorno["almacen"]._raiz / clave_almacen(fila[0])
        ruta_blob.write_bytes(b"CORRUPTO")  # bitrot simulado

        resumen = verificar_indexados(config, almacen=entorno["almacen"])
        assert resumen.fallidos == 1
        assert resumen.verificados == 14
        estado = (
            entorno["conexion"]
            .execute("SELECT estado, error_motivo FROM archivos WHERE nombre = 'ventas_2023.csv'")
            .fetchone()
        )
        assert estado[0] == "ERROR"
        assert "verificacion_fallida" in estado[1]


class TestPuertaSagrada:
    """INVARIANTE R1: estos tests son la puerta. Romperlos = romper el sistema."""

    def test_no_se_abre_con_indexados_sin_verificar(
        self, config: Config, entorno: dict[str, Any]
    ) -> None:
        estado = evaluar_puerta(config, "disco-test")
        assert estado.seguro_para_desechar is False
        assert estado.pendientes > 0

    def test_no_se_abre_con_cold_sin_mover(self, config: Config, entorno: dict[str, Any]) -> None:
        """Los COLD también son dato: sin moverlos al frío, el disco NO se desecha."""
        verificar_indexados(config, almacen=entorno["almacen"])
        estado = evaluar_puerta(config, "disco-test")
        assert estado.seguro_para_desechar is False
        assert estado.pendientes == 8  # los 8 COLD siguen solo en el disco origen

    def test_se_abre_solo_con_todo_a_salvo(self, config: Config, entorno: dict[str, Any]) -> None:
        """El camino feliz completo: verificar + mover frío → puerta VERDE."""
        verificar_indexados(config, almacen=entorno["almacen"])
        resumen_frio = mover_frio(config, almacen_frio=entorno["frio"])
        assert resumen_frio.movidos == 8
        assert resumen_frio.errores == 0

        estado = evaluar_puerta(config, "disco-test")
        assert estado.total == 23
        assert estado.hechos == 15
        assert estado.cold_movidos == 8
        assert estado.pendientes == 0
        assert estado.seguro_para_desechar is True

        marcado = (
            entorno["conexion"]
            .execute("SELECT seguro_para_desechar FROM discos WHERE disco_id = 'disco-test'")
            .fetchone()[0]
        )
        assert marcado is True

    def test_un_solo_error_bloquea_la_puerta(self, config: Config, entorno: dict[str, Any]) -> None:
        """UN archivo en ERROR (de 23) y el disco entero queda retenido."""
        fila = (
            entorno["conexion"]
            .execute("SELECT hash_contenido FROM archivos WHERE nombre = 'notas.txt'")
            .fetchone()
        )
        (entorno["almacen"]._raiz / clave_almacen(fila[0])).write_bytes(b"X")

        verificar_indexados(config, almacen=entorno["almacen"])
        mover_frio(config, almacen_frio=entorno["frio"])
        estado = evaluar_puerta(config, "disco-test")
        assert estado.errores == 1
        assert estado.seguro_para_desechar is False

    def test_nodo_que_no_es_maestro_nunca_da_verde(
        self, config: Config, entorno: dict[str, Any]
    ) -> None:
        """⚙K16 — todo a salvo AQUÍ no basta si este nodo no es el archivo maestro.

        Desechar el origen dejaría su única copia en una máquina que no es el
        archivo (p. ej. un VPS de 1 TB). Fail-closed: mientras la replicación no
        confirme, la puerta no abre — con motivo explícito, no en silencio."""
        from normalizacion.core.config import PerillasDespliegue

        verificar_indexados(config, almacen=entorno["almacen"])
        mover_frio(config, almacen_frio=entorno["frio"])
        assert evaluar_puerta(config, "disco-test").seguro_para_desechar is True

        vps = config.model_copy(
            update={
                "despliegue": PerillasDespliegue(perfil="hibrido-servicio", nodo_id="vps-01")
            }
        )
        estado = evaluar_puerta(vps, "disco-test")
        assert estado.pendientes == 0  # aquí no falta nada…
        assert estado.seguro_para_desechar is False  # …pero no es el archivo
        assert estado.motivo_bloqueo == "pendiente_replica_al_maestro"

    def test_disco_registrado_pero_vacio_no_es_seguro(
        self, config: Config, conexion: Any
    ) -> None:
        """0 archivos != verificado: un disco sin catalogar jamás es 'seguro'."""
        from normalizacion.core import cola

        cola.upsert_disco(conexion, "disco-vacio", "/mnt/vacio")
        conexion.commit()
        estado = evaluar_puerta(config, "disco-vacio")
        assert estado.seguro_para_desechar is False
        assert estado.motivo_bloqueo == "sin_catalogar"

    def test_disco_ajeno_no_recibe_veredicto(self, config: Config, conexion: Any) -> None:
        """⚙K16 — un disco que este nodo nunca registró NO obtiene veredicto.

        Antes devolvía `seguro=False`, que es una AFIRMACIÓN sobre algo que este nodo
        jamás observó (en híbrido, un disco del otro nodo). "No es seguro" induce a
        pensar que su procesamiento falló; lo correcto es negarse a responder."""
        from normalizacion.ingesta.workers.verificador import DiscoDesconocido

        with pytest.raises(DiscoDesconocido):
            evaluar_puerta(config, "disco-fantasma")

    def test_mover_frio_es_idempotente(self, config: Config, entorno: dict[str, Any]) -> None:
        mover_frio(config, almacen_frio=entorno["frio"])
        segunda = mover_frio(config, almacen_frio=entorno["frio"])
        assert segunda.movidos == 0

    def test_cold_envenenado_no_tumba_el_movimiento(
        self, config: Config, entorno: dict[str, Any], monkeypatch: Any
    ) -> None:
        """Mismo blindaje que el worker: un COLD que revienta con excepción
        inesperada va a ERROR (bloquea la puerta) y mover-frío SIGUE con los demás."""
        from normalizacion.ingesta.workers import verificador

        real = verificador._persistir
        n = {"i": 0}

        def persistir_con_veneno(*a: Any, **k: Any) -> Any:
            n["i"] += 1
            if n["i"] == 2:
                raise ValueError("bytes hostiles en una entrada de 7z")
            return real(*a, **k)

        monkeypatch.setattr(verificador, "_persistir", persistir_con_veneno)
        resumen = mover_frio(config, almacen_frio=entorno["frio"])
        assert resumen.errores == 1  # SOLO el envenenado
        assert resumen.movidos == 7  # los otros 7 COLD, movidos
        # el envenenado quedó ERROR → la puerta lo ve y NO deja desechar el disco
        estado = evaluar_puerta(config, "disco-test")
        assert estado.errores == 1
        assert estado.seguro_para_desechar is False
