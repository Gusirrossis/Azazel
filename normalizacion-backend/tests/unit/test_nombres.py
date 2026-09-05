"""El verificador CURP↔nombre y la extracción desde el contexto del ancla.

Lo que de verdad se prueba aquí no es que encuentre nombres: es que NO invente
ninguno. Este corpus tiene volcados de autorrelleno con decenas de personas por
fichero, donde el nombre más cercano a una CURP suele ser de otra; un extractor que
se fíe de la cercanía llena la base de nombres equivocados, y un nombre equivocado en
una ficha de persona es peor que no tener nombre.
"""

from __future__ import annotations

from normalizacion.entidades import anclas, nombres
from normalizacion.entidades.anclas import Ancla

# ---------------------------------------------------------------- el verificador

def test_vectores_conocidos_verifican() -> None:
    """Nombres reales contra sus CURP reales. Si esto falla, todo lo demás miente."""
    assert nombres.casa_curp("HEGG560427MVZRRL04", "HERNANDEZ", "GARCIA", "GLORIA")
    assert nombres.casa_curp("AAMJ850315HDFLRN09", "ALVAREZ", "MORALES", "JUAN")
    assert nombres.casa_curp("MELM800101HDFNPR07", "MENDOZA", "LOPEZ", "MARIO")


def test_codigo_deriva_las_siete_letras() -> None:
    # HERNANDEZ → H + primera vocal interna (E); GARCIA → G; GLORIA → G
    # consonantes internas: HERNANDEZ→R, GARCIA→R, GLORIA→L
    assert nombres.codigo_curp("HERNANDEZ", "GARCIA", "GLORIA") == ("HEGG", "RRL")


def test_nombre_equivocado_se_rechaza() -> None:
    assert not nombres.casa_curp("HEGG560427MVZRRL04", "PEREZ", "LOPEZ", "MARIO")
    # Iniciales correctas pero consonantes internas distintas: no basta con las 4.
    assert not nombres.casa_curp("HEGG560427MVZRRL04", "HUERTA", "GAONA", "GUSTAVO")


def test_regla_jose_maria_salta_al_segundo_nombre() -> None:
    """«José Daniel Ayala Ortiz» ancla en DANIEL, no en JOSÉ (regla RENAPO)."""
    assert nombres.codigo_curp("AYALA", "ORTIZ", "JOSE DANIEL")[0] == "AAOD"
    assert nombres.codigo_curp("AYALA", "ORTIZ", "MARIA GUADALUPE")[0] == "AAOG"
    # Si JOSÉ va solo, sí manda él.
    assert nombres.codigo_curp("AYALA", "ORTIZ", "JOSE")[0] == "AAOJ"


def test_particulas_se_ignoran() -> None:
    """«DE LA CRUZ» ancla en CRUZ: las partículas no cuentan."""
    assert nombres.codigo_curp("DE LA CRUZ", "SANTOS", "ANA") == \
           nombres.codigo_curp("CRUZ", "SANTOS", "ANA")


def test_equis_de_la_curp_actua_de_comodin() -> None:
    """RENAPO pone X donde no deriva letra o donde saldría una palabra malsonante."""
    assert nombres.casa_curp("HXGG560427MVZRRL04", "HERNANDEZ", "GARCIA", "GLORIA")
    assert nombres.casa_curp("HEGG560427MVZXRL04", "HERNANDEZ", "GARCIA", "GLORIA")


def test_un_solo_apellido_usa_equis() -> None:
    assert nombres.codigo_curp("HERNANDEZ", "", "GLORIA") == ("HEXG", "RXL")


def test_sin_apellido_o_sin_pila_no_hay_codigo() -> None:
    assert nombres.codigo_curp("", "GARCIA", "GLORIA") is None
    assert nombres.codigo_curp("HERNANDEZ", "GARCIA", "") is None


def test_plegar_duro_quita_acentos_pero_conserva_la_enye() -> None:
    assert nombres.plegar_duro("José Núñez") == "JOSE NUÑEZ"
    assert nombres.plegar_duro("MUÑOZ") == "MUÑOZ"


# ------------------------------------------------------- extracción del contexto

def test_extrae_con_apellidos_primero() -> None:
    ctx = "REGISTRO 4471 HERNANDEZ GARCIA GLORIA HEGG560427MVZRRL04 ALTA 2019"
    partes = nombres.extraer_de_contexto("curp", "HEGG560427MVZRRL04", ctx)
    assert partes == {"apellido1": "HERNANDEZ", "apellido2": "GARCIA", "nombre1": "GLORIA"}


def test_extrae_con_nombre_primero() -> None:
    ctx = "curp AAMJ850315HDFLRN09 nombre JUAN ALVAREZ MORALES tel 5544332211"
    partes = nombres.extraer_de_contexto("curp", "AAMJ850315HDFLRN09", ctx)
    assert partes["apellido1"] == "ALVAREZ"
    assert partes["apellido2"] == "MORALES"
    assert partes["nombre1"] == "JUAN"


def test_nombre_ajeno_cercano_NO_se_acepta() -> None:
    """El caso que motiva todo esto: volcado de autorrelleno con varias personas.

    Ninguno de los dos nombres pertenece a esa CURP, y el resultado correcto es no
    devolver nada — no «el más cercano».
    """
    ctx = ("txtNombre AUSTREBERTA CRUZ ESPINOZA txtRPU 98974120404 "
           "curp GOGE090430HNENTDA5 txtNombre MARIO MARTINEZ LOPEZ")
    assert nombres.extraer_de_contexto("curp", "GOGE090430HNENTDA5", ctx) is None


def test_no_se_traga_basura_detras_del_ultimo_campo() -> None:
    """Caso real: «MAYRA CASTRO LARA CURPTUT» salía con CURPTUT de apellido materno.

    El ancla comprueba una palabra por campo; lo que va detrás del último no lo mira
    nadie. Lo destapó una medición sobre el corpus, no una prueba escrita a mano.
    """
    ctx = "MAYRA CASTRO LARA CURPTUT CALM860315MMCSRY06"
    partes = nombres.extraer_de_contexto("curp", "CALM860315MMCSRY06", ctx)
    assert partes is not None
    assert partes["nombre1"] == "MAYRA"
    assert partes["apellido1"] == "CASTRO"
    assert partes["apellido2"] == "LARA"       # sin CURPTUT
    assert "CURPTUT" not in " ".join(partes.values())


def test_recorte_conserva_las_particulas_del_apellido() -> None:
    """«DE LA CRUZ» se recorta a «DE LA CRUZ», no a «DE»."""
    assert nombres._recortar_cola("DE LA CRUZ SOBRANTE") == "DE LA CRUZ"
    assert nombres._recortar_cola("LARA CURPTUT") == "LARA"


def test_sin_contexto_no_inventa() -> None:
    assert nombres.extraer_de_contexto("curp", "HEGG560427MVZRRL04", None) is None
    assert nombres.extraer_de_contexto("curp", "HEGG560427MVZRRL04", "") is None


def test_tipo_desconocido_devuelve_nada() -> None:
    ctx = "HERNANDEZ GARCIA GLORIA HEGG560427MVZRRL04"
    assert nombres.extraer_de_contexto("email", "x@y.z", ctx) is None


def test_rfc_exige_cercania() -> None:
    """El RFC solo aporta 4 letras; un nombre lejano no se acepta aunque verifique."""
    cerca = "HEGG560427AB1 GLORIA HERNANDEZ GARCIA"
    assert nombres.extraer_de_contexto("rfc", "HEGG560427AB1", cerca) is not None

    lejos = "HEGG560427AB1" + (" RELLENO" * 30) + " GLORIA HERNANDEZ GARCIA"
    assert nombres.extraer_de_contexto("rfc", "HEGG560427AB1", lejos) is None


def test_prefiere_la_tira_mas_larga() -> None:
    """«JUAN ANTONIO VILLARREAL NUÑEZ» y «ANTONIO VILLARREAL NUÑEZ» verifican las dos."""
    ctx = "JUAN ANTONIO VILLARREAL NUÑEZ VINJ760623HNTLXN09"
    partes = nombres.extraer_de_contexto("curp", "VINJ760623HNTLXN09", ctx)
    assert partes is not None
    assert partes["nombre1"] == "JUAN"
    assert partes.get("nombre2") == "ANTONIO"


# ----------------------------------------------------- integración con las filas

def test_filas_persona_adjunta_el_nombre() -> None:
    a = Ancla(tipo="curp", valor="HEGG560427MVZRRL04", inicio=0,
              contexto="HERNANDEZ GARCIA GLORIA HEGG560427MVZRRL04")
    filas = anclas.filas_persona([a])
    assert filas == [{"curp": "HEGG560427MVZRRL04", "apellido1": "HERNANDEZ",
                      "apellido2": "GARCIA", "nombre1": "GLORIA"}]


def test_filas_persona_sin_nombre_sigue_dando_la_fila() -> None:
    """Sin nombre verificable la entidad se resuelve igual: el ancla basta."""
    a = Ancla(tipo="curp", valor="HEGG560427MVZRRL04", inicio=0,
              contexto="lote 33 expediente 9912 sin datos personales")
    assert anclas.filas_persona([a]) == [{"curp": "HEGG560427MVZRRL04"}]


def test_el_nombre_sobrevive_la_cadena_entera_del_backfill() -> None:
    """De documento indexado a entidad, pasando por la asignación del backfill.

    Es la prueba que faltaba: `_ASIGNACION` es una allowlist, y con solo
    `{"curp", "rfc"}` el nombre se extraía correctamente y `construir_entidad` lo
    tiraba a la basura sin que nada fallara. Un test de la extracción sola no lo ve.
    """
    from normalizacion.entidades.backfill import _ASIGNACION, personas_de_doc
    from normalizacion.entidades.derivados import enriquecer
    from normalizacion.entidades.pipeline import construir_entidad
    from normalizacion.entidades.receta import PERSONA_FZ1

    doc = {
        "archivo_id": "abc123",
        "ruta_original": "/padron/2019.txt",
        "disco_id": "d1",
        "texto_indexable": "ALTA 2019 HERNANDEZ GARCIA GLORIA HEGG560427MVZRRL04 FIN",
    }
    filas, _ = personas_de_doc(doc)
    assert filas and filas[0]["apellido1"] == "HERNANDEZ"

    ent = construir_entidad(PERSONA_FZ1, _ASIGNACION, filas[0])
    assert ent is not None
    assert ent["campos"]["nombre_completo"] == "GLORIA HERNANDEZ GARCIA"

    # Y al LEERLA sigue estando, que es lo que verá quien federa.
    leida = enriquecer(ent["campos"])
    assert leida["nombre_completo"] == "GLORIA HERNANDEZ GARCIA"
    assert leida["normalizados"]["normalized_name"]


def test_nombre_junto_al_rfc_se_valida_contra_la_curp() -> None:
    """Si la fila tiene CURP y RFC, manda la CURP aunque el nombre esté junto al RFC.

    Son la misma persona (comparten los 10 primeros caracteres), así que se puede
    comprobar con las siete letras de la CURP un nombre que solo aparece al lado del
    RFC — más fuerte que aceptarlo por las cuatro del RFC.
    """
    curp = Ancla(tipo="curp", valor="HEGG560427MVZRRL04", inicio=0,
                 contexto="expediente 4471 sin nombre aqui HEGG560427MVZRRL04")
    rfc = Ancla(tipo="rfc", valor="HEGG560427AB1", inicio=400,
                contexto="HEGG560427AB1 GLORIA HERNANDEZ GARCIA")
    filas = anclas.filas_persona([curp, rfc])
    assert len(filas) == 1
    assert filas[0]["curp"] == "HEGG560427MVZRRL04"
    assert filas[0]["rfc"] == "HEGG560427AB1"
    assert filas[0]["apellido1"] == "HERNANDEZ"
    assert filas[0]["nombre1"] == "GLORIA"
