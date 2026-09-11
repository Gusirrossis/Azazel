"""Un CSV/NDJSON plano troceado en lotes: el 100 % de las filas, no los primeros 10 MB.

Espejo de `test_tabla_lotes.py`. La propiedad que se sostiene es COBERTURA: la unión de
todos los lotes reproduce todas las filas del archivo, sin repetir ni perder. Y el borde
que de verdad importa: un `\n` dentro de un campo CSV entrecomillado NO puede partir un
registro (eso sería corrupción, peor que truncar).
"""

from __future__ import annotations

import io
import json

from normalizacion.core.config import PerillasFiltro
from normalizacion.ingesta.precalificacion.tabla_plana import (
    explorar,
    planificar_csv,
    planificar_ndjson,
    servir_lote,
)

GRANDE = 1 << 30


def _servir_todo(datos: bytes, lotes) -> list[dict]:
    registros: list[dict] = []
    for lote in lotes:
        spool = servir_lote(
            io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE
        )
        for linea in spool.read().decode("utf-8").splitlines():
            if linea.strip():
                registros.append(json.loads(linea))
    return registros


class TestNdjsonCobertura:
    def test_la_union_de_lotes_es_el_archivo_entero(self) -> None:
        filas = [{"curp": f"C{i:05d}", "n": i} for i in range(200)]
        datos = ("\n".join(json.dumps(f) for f in filas) + "\n").encode()
        lotes = planificar_ndjson(io.BytesIO(datos), bytes_por_lote=256)
        assert len(lotes) > 1, "el troceado tiene que producir varios lotes"
        assert _servir_todo(datos, lotes) == filas  # orden, sin dup, sin pérdida

    def test_una_linea_mas_larga_que_la_ventana_se_sirve_una_vez(self) -> None:
        """Una fila enorme (un objeto con un campo largo) abarca varias ventanas; la regla
        de solapamiento la sirve exactamente una vez."""
        filas = [
            {"curp": "C1", "x": "a"},
            {"curp": "C2", "blob": "z" * 2000},  # mucho mayor que la ventana
            {"curp": "C3", "x": "b"},
        ]
        datos = ("\n".join(json.dumps(f) for f in filas) + "\n").encode()
        lotes = planificar_ndjson(io.BytesIO(datos), bytes_por_lote=128)
        recogidos = _servir_todo(datos, lotes)
        assert recogidos == filas
        assert [r["curp"] for r in recogidos].count("C2") == 1

    def test_frontera_justo_en_inicio_de_linea_no_pierde_esa_linea(self) -> None:
        """Si una ventana termina EXACTO al final de una línea, la siguiente no debe
        saltar la línea que arranca en su primer byte."""
        # líneas de exactamente 8 bytes (7 + '\n') con ventana múltiplo de 8
        filas = [{"i": i} for i in range(50)]
        datos = ("\n".join(json.dumps(f, separators=(",", ":")) for f in filas) + "\n").encode()
        lotes = planificar_ndjson(io.BytesIO(datos), bytes_por_lote=16)
        recogidos = _servir_todo(datos, lotes)
        assert recogidos == filas


class TestCsvCobertura:
    def test_todas_las_filas_una_vez(self) -> None:
        cab = "id,curp,nombre\n"
        filas = [f"{i},C{i:05d},Persona{i}" for i in range(200)]
        datos = (cab + "\n".join(filas) + "\n").encode()
        lotes = planificar_csv(io.BytesIO(datos), registros_por_lote=50)
        recogidos = _servir_todo(datos, lotes)
        assert len(recogidos) == 200
        assert recogidos[0] == {"id": "0", "curp": "C00000", "nombre": "Persona0"}
        assert recogidos[199]["curp"] == "C00199"
        assert len({r["curp"] for r in recogidos}) == 200  # sin duplicados

    def test_ultima_fila_sin_salto_final(self) -> None:
        datos = b"id,curp\n1,A\n2,B"  # sin '\n' final
        lotes = planificar_csv(io.BytesIO(datos), registros_por_lote=1)
        recogidos = _servir_todo(datos, lotes)
        assert [r["curp"] for r in recogidos] == ["A", "B"]

    def test_solo_cabecera_no_produce_lotes(self) -> None:
        datos = b"id,curp\n"
        assert planificar_csv(io.BytesIO(datos)) == []


class TestCsvComillas:
    def test_salto_de_linea_dentro_de_campo_no_parte_el_registro(self) -> None:
        """EL borde: un `\n` entre comillas no es fin de registro. Con troceo por byte
        crudo, el registro 1 se partiría en dos filas basura."""
        cab = "id,curp,notas\n"
        filas = [
            '1,C00001,"linea1\nlinea2"',  # salto de línea DENTRO del campo
            '2,C00002,"dijo ""hola"" y se fue"',  # comillas escapadas ""
            "3,C00003,simple",
        ]
        datos = (cab + "\n".join(filas) + "\n").encode()
        lotes = planificar_csv(io.BytesIO(datos), registros_por_lote=1)
        recogidos = _servir_todo(datos, lotes)
        assert len(recogidos) == 3, "el \\n interno no puede crear una fila fantasma"
        assert recogidos[0]["notas"] == "linea1\nlinea2"
        assert recogidos[1]["notas"] == 'dijo "hola" y se fue'
        assert recogidos[2]["curp"] == "C00003"

    def test_delimitador_punto_y_coma(self) -> None:
        cab = "id;curp;nombre\n"
        filas = [f"{i};C{i:05d};P{i}" for i in range(12)]
        datos = (cab + "\n".join(filas) + "\n").encode()
        lotes = planificar_csv(io.BytesIO(datos), registros_por_lote=4)
        recogidos = _servir_todo(datos, lotes)
        assert len(recogidos) == 12
        assert recogidos[0] == {"id": "0", "curp": "C00000", "nombre": "P0"}


class TestIncremental:
    def test_ndjson_subir_el_tope_no_mueve_los_lotes(self) -> None:
        filas = [{"i": i} for i in range(100)]
        datos = ("\n".join(json.dumps(f) for f in filas) + "\n").encode()
        pocos = planificar_ndjson(io.BytesIO(datos), bytes_por_lote=64, max_lotes=2)
        muchos = planificar_ndjson(io.BytesIO(datos), bytes_por_lote=64, max_lotes=1000)
        assert [x.ruta_interna for x in pocos] == [x.ruta_interna for x in muchos[:2]]

    def test_csv_subir_el_tope_no_mueve_los_lotes(self) -> None:
        cab = "id,curp\n"
        datos = (cab + "\n".join(f"{i},C{i:05d}" for i in range(300)) + "\n").encode()
        pocos = planificar_csv(io.BytesIO(datos), registros_por_lote=50, max_lotes=2)
        muchos = planificar_csv(io.BytesIO(datos), registros_por_lote=50, max_lotes=1000)
        assert [x.ruta_interna for x in pocos] == [x.ruta_interna for x in muchos[:2]]


class TestServeDeterminista:
    def test_servir_el_mismo_lote_da_los_mismos_bytes(self) -> None:
        cab = "id,curp\n"
        datos = (cab + "\n".join(f"{i},C{i:05d}" for i in range(60)) + "\n").encode()
        lote = planificar_csv(io.BytesIO(datos), registros_por_lote=20)[1]
        a = servir_lote(io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE).read()
        b = servir_lote(io.BytesIO(datos), lote.ruta_interna, umbral_memoria=1 << 20, limite_bytes=GRANDE).read()
        assert a == b and a


class TestExplorar:
    def test_entradas_y_topado(self) -> None:
        filas = [{"i": i} for i in range(100)]
        datos = ("\n".join(json.dumps(f) for f in filas) + "\n").encode()
        perillas = PerillasFiltro()
        entradas, motivo, topado = explorar(perillas, io.BytesIO(datos), "ndjson", 123)
        assert motivo is None and not topado
        assert entradas and entradas[0][0].startswith("ndjson/")

    def test_topado_se_reporta(self) -> None:
        # >2 ventanas de 64 KiB para que el tope de 2 lo trunque de verdad
        filas = [{"i": i, "pad": "x" * 1000} for i in range(300)]  # ~300 KiB
        datos = ("\n".join(json.dumps(f) for f in filas) + "\n").encode()
        perillas = PerillasFiltro(t3_entradas_max=2)
        entradas, _motivo, topado = explorar(perillas, io.BytesIO(datos), "ndjson", 0)
        assert topado and len(entradas) == 2
