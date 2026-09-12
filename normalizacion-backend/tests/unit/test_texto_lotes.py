"""Un texto/log/SQL/dump grande troceado en ventanas: el 100 % del contenido, no los
primeros 100k chars. Espejo de `test_tabla_plana.py`. La propiedad es COBERTURA: la unión
de los trozos servidos reproduce el archivo entero, sin repetir ni perder — y el borde que
importa: un dato tras el byte 150k (más allá de lo que cabría en el doc único truncado)
ahora vive en un trozo buscable."""

from __future__ import annotations

import io

from normalizacion.core.config import PerillasFiltro
from normalizacion.ingesta.precalificacion.texto_lotes import explorar, planificar, servir_lote

GRANDE = 1 << 30
CURP = "GOMC800101HDFXXX01"


def _servir_todo(datos: bytes, lotes) -> bytes:
    partes = []
    for lote in lotes:
        spool = servir_lote(
            io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
        )
        partes.append(spool.read())
    return b"".join(partes)


class TestCobertura:
    def test_la_union_de_trozos_es_el_archivo_entero(self) -> None:
        # 5000 líneas → varios cientos de KB → varias ventanas de 64 KiB
        datos = ("\n".join(f"linea numero {i} con algo de relleno" for i in range(5000)) + "\n").encode()
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=16 * 1024)
        assert len(lotes) > 1
        assert _servir_todo(datos, lotes) == datos  # sin pérdida ni duplicado

    def test_un_dato_tras_el_byte_150k_es_buscable(self) -> None:
        """EL bug: hoy el texto se trunca a 100k chars y una CURP en el byte 150k es
        invisible. Troceado, cae en un trozo que sí se indexa."""
        relleno = ("x" * 78 + "\n").encode()  # ~79 B por línea
        cabeza = relleno * 1900  # ~150 KB antes de la CURP
        datos = cabeza + (f"registro con {CURP} dentro\n").encode() + relleno * 500
        assert len(cabeza) > 130_000  # la CURP está bien pasado el corte de 100k
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=64 * 1024)
        recuperado = _servir_todo(datos, lotes)
        assert recuperado.count(CURP.encode()) == 1  # aparece EXACTAMENTE una vez
        # y en particular, en algún trozo servido (no perdido por el truncado)
        assert CURP.encode() in recuperado

    def test_linea_que_cruza_la_ventana_se_sirve_una_vez(self) -> None:
        # líneas de ~100 B con ventana de 256 B → varias líneas cruzan bordes
        datos = ("\n".join(f"L{i:04d}-" + "y" * 90 for i in range(200)) + "\n").encode()
        lotes = planificar(io.BytesIO(datos), bytes_por_lote=256)
        assert _servir_todo(datos, lotes) == datos


class TestIncremental:
    def test_subir_el_tope_no_mueve_los_trozos(self) -> None:
        datos = ("\n".join(f"linea {i}" for i in range(2000)) + "\n").encode()
        pocos = planificar(io.BytesIO(datos), bytes_por_lote=1024, max_lotes=3)
        muchos = planificar(io.BytesIO(datos), bytes_por_lote=1024, max_lotes=1000)
        assert [x.ruta_interna for x in pocos] == [x.ruta_interna for x in muchos[:3]]


class TestServeDeterminista:
    def test_servir_el_mismo_trozo_da_los_mismos_bytes(self) -> None:
        datos = ("\n".join(f"linea {i} zzz" for i in range(3000)) + "\n").encode()
        lote = planificar(io.BytesIO(datos), bytes_por_lote=16 * 1024)[1]
        a = servir_lote(io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE).read()
        b = servir_lote(io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE).read()
        assert a == b and a


class TestExplorar:
    def test_entradas_y_topado(self) -> None:
        datos = ("\n".join(f"linea {i} con relleno" for i in range(3000)) + "\n").encode()
        entradas, motivo, topado = explorar(PerillasFiltro(), io.BytesIO(datos), 123)
        assert motivo is None and not topado
        assert entradas and entradas[0][0].startswith("texto/")

    def test_topado_se_reporta(self) -> None:
        datos = ("\n".join("x" * 100 for _ in range(4000)) + "\n").encode()  # ~400 KB
        entradas, _motivo, topado = explorar(PerillasFiltro(t3_entradas_max=2), io.BytesIO(datos), 0)
        assert topado and len(entradas) == 2
