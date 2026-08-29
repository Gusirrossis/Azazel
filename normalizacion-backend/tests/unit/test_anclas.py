"""Detección de anclas y ventana de contexto.

La misma función la usan el worker (resolución en vivo) y el backfill (histórico).
Si decidieran distinto, el mismo documento produciría entidades diferentes según por
dónde entró al sistema — y eso es de los errores que no se ven hasta que hay dos
fichas de la misma persona y nadie sabe por qué.
"""

from __future__ import annotations

from normalizacion.entidades import anclas

# CURP con dígito verificador correcto (el validador lo comprueba de verdad).
CURP_A = "MAAJ800101HDFRRN09"
CURP_B = "PEGL900215MDFRRS04"


class TestBuscar:
    def test_texto_vacio_o_none(self) -> None:
        assert anclas.buscar_en_texto(None) == []
        assert anclas.buscar_en_texto("") == []

    def test_sin_anclas(self) -> None:
        assert anclas.buscar_en_texto("Aquí no hay ninguna clave que valga.") == []

    def test_no_corta_dentro_de_una_cadena_mayor(self) -> None:
        """Un hash largo puede contener algo con forma de CURP. Los lookarounds están
        para que no se saque de ahí: sería un ancla inventada, y una persona falsa."""
        texto = f"hash=XXXX{CURP_A}XXXX"
        assert anclas.buscar_en_texto(texto) == []

    def test_no_duplica_la_misma_ancla(self) -> None:
        texto = f"{CURP_A} y otra vez {CURP_A}"
        encontradas = anclas.buscar_en_texto(texto)
        assert len([a for a in encontradas if a.tipo == "curp"]) == 1


class TestContexto:
    def test_guarda_lo_que_rodea_al_ancla(self) -> None:
        """La razón de ser de la ventana: el nombre está JUNTO a la CURP, y hoy se
        pierde porque asociarlo requiere NER. Guardarlo ahora evita tener que volver
        a OCR-ear 39 000 documentos cuando llegue esa fase."""
        texto = f"NOMBRE: JUAN PEREZ RAMIREZ   CURP: {CURP_A}   DOMICILIO: CALLE FALSA 123"
        encontradas = anclas.buscar_en_texto(texto)
        assert len(encontradas) == 1
        contexto = encontradas[0].contexto
        assert "JUAN PEREZ RAMIREZ" in contexto
        assert "CALLE FALSA 123" in contexto

    def test_el_contexto_conserva_las_mayusculas_originales(self) -> None:
        """El casado se hace en mayúsculas, pero el contexto se corta del ORIGINAL:
        un NER se apoya en la capitalización para reconocer nombres propios, así que
        devolverlo todo en mayúsculas lo dejaría ciego."""
        texto = f"Nombre: Juan Pérez. Curp: {CURP_A}."
        assert "Juan Pérez" in anclas.buscar_en_texto(texto)[0].contexto

    def test_la_ventana_se_acota(self) -> None:
        """El relleno va con espacios, no con letras: pegar una letra al ancla la
        invalida a propósito (los lookarounds evitan sacar una CURP de dentro de un
        hash). Ese comportamiento lo cubre `test_no_corta_dentro_de_una_cadena_mayor`."""
        texto = "reller o " * 500 + CURP_A + " mas relleno " * 500
        contexto = anclas.buscar_en_texto(texto, ventana=50)[0].contexto
        assert len(contexto) <= 50 + len(CURP_A) + 50

    def test_no_se_pasa_de_los_bordes(self) -> None:
        """Un ancla al principio del texto no debe reventar por índice negativo."""
        assert anclas.buscar_en_texto(f"{CURP_A} al inicio")[0].contexto.startswith(CURP_A)

    def test_tope_de_ventanas_por_documento(self) -> None:
        """Un padrón con miles de CURPs no debe inflar el doc del índice: para eso
        está la tabla de entidades, no el contexto suelto."""
        muchas = [
            anclas.Ancla(tipo="curp", valor=f"C{i}", inicio=i, contexto="x") for i in range(200)
        ]
        assert len(anclas.contexto_para_doc(muchas)) == anclas.MAX_VENTANAS


class TestFilasPersona:
    def test_una_fila_por_curp(self) -> None:
        encontradas = anclas.buscar_en_texto(f"{CURP_A} {CURP_B}")
        filas = anclas.filas_persona(encontradas)
        assert len(filas) == 2
        assert {f["curp"] for f in filas} == {CURP_A, CURP_B}

    def test_sin_anclas_no_hay_filas(self) -> None:
        assert anclas.filas_persona([]) == []

    def test_un_rfc_sin_curp_ancla_su_propia_persona(self) -> None:
        """No se descarta: un documento que solo trae RFC igual identifica a alguien."""
        filas = anclas.filas_persona(
            [anclas.Ancla(tipo="rfc", valor="MAAJ800101AB1", inicio=0, contexto="")]
        )
        assert filas == [{"rfc": "MAAJ800101AB1"}]

    def test_el_rfc_del_mismo_prefijo_enriquece_la_curp(self) -> None:
        """Compartir los 10 primeros caracteres (4 letras del nombre + AAMMDD) es
        garantía fuerte de misma persona: se fusionan en vez de crear dos fichas."""
        filas = anclas.filas_persona(
            [
                anclas.Ancla(tipo="curp", valor=CURP_A, inicio=0, contexto=""),
                anclas.Ancla(tipo="rfc", valor=CURP_A[:10] + "AB1", inicio=30, contexto=""),
            ]
        )
        assert len(filas) == 1
        assert filas[0]["curp"] == CURP_A
        assert filas[0]["rfc"] == CURP_A[:10] + "AB1"

    def test_un_rfc_de_otra_persona_no_se_asocia(self) -> None:
        """Documento con dos personas: el RFC de una no puede colgarse de la CURP de
        la otra. Ese error crearía una ficha con datos mezclados de dos personas."""
        filas = anclas.filas_persona(
            [
                anclas.Ancla(tipo="curp", valor=CURP_A, inicio=0, contexto=""),
                anclas.Ancla(tipo="rfc", valor="ZZZZ990909XY1", inicio=30, contexto=""),
            ]
        )
        assert len(filas) == 2
        assert {"rfc": "ZZZZ990909XY1"} in filas
