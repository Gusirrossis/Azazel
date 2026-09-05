"""Sacar el nombre de una persona VERIFICÁNDOLO contra su propia CURP o RFC.

El problema real, medido sobre este corpus: hay 89.652 entidades y ninguna tiene
nombre. Todas se resolvieron desde una CURP o un RFC encontrados en texto, y buscar
«Juan Pérez» desde Lilith devolvía cero personas aunque la persona estuviera.

La tentación es coger el nombre que aparece más cerca del ancla. En este corpus eso
sería falso a menudo: una buena parte del material son volcados de autorrelleno de
navegador, donde los campos van en lista plana y el nombre de al lado pertenece a
otro registro:

    txtNombre AUSTREBERTA CRUZ E … curp GOGE090430HNENTDA5 … txtNombre MARIO MARTINEZ

Lo que hace esto resoluble sin NER es que **la CURP lleva dentro las letras del
nombre**, y por tanto puede COMPROBAR un candidato:

    posición 0  inicial del apellido paterno          GOGE… → G
    posición 1  primera vocal interna del paterno            → O
    posición 2  inicial del apellido materno                 → G
    posición 3  inicial del nombre de pila                    → E
    posición 13 primera consonante interna del paterno
    posición 14 primera consonante interna del materno
    posición 15 primera consonante interna del pila

Son siete letras. Ni «AUSTREBERTA CRUZ» ni «MARIO MARTINEZ» las producen, así que las
dos se rechazan solas: no hace falta saber cuál es la buena, basta con que ninguna
falsa pase. Medido sobre datos reales: 83% de aciertos fuera de los volcados de
navegador, y 0% dentro de ellos — que es el resultado CORRECTO, porque ahí el nombre
de al lado no es de esa persona.

La regla de oro: **ningún nombre es mejor que un nombre equivocado**. Solo se guarda
lo que verifica. Por eso no hace falta marcar la procedencia del nombre en la ficha:
si está, está comprobado contra el ancla.

El RFC de persona física comparte esas cuatro primeras letras con la CURP, así que el
mismo verificador sirve para las 7.244 entidades ancladas en RFC (con tres letras
menos de comprobación, que es más débil y por eso se exige el nombre más cercano).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from itertools import pairwise
from typing import Any

_VOCALES = frozenset("AEIOU")

#: Partículas que la CURP ignora al tomar iniciales: «DE LA CRUZ» ancla en CRUZ.
_PARTICULAS = frozenset({
    "DE", "DEL", "LA", "LAS", "LOS", "MC", "MAC", "VON", "VAN", "DER", "DI",
    "Y", "SAN", "SANTA", "DA", "DAS", "DO", "DOS", "LE", "SAINT", "ST",
})

#: Nombres de pila que se saltan si hay otro detrás (regla RENAPO): «José Daniel»
#: ancla en DANIEL, no en JOSÉ. Sin esto se pierde un porcentaje grande del corpus
#: mexicano, donde José y María encabezan una parte enorme de los nombres compuestos.
_PILA_IGNORADA = frozenset({"MARIA", "MA", "JOSE", "J"})

#: Palabras que aparecen alrededor de las anclas y NO son nombres. La lista es de
#: nombres de campo de formulario y de cabeceras que salen una y otra vez en este
#: material; no pretende ser exhaustiva, solo quitar el ruido más frecuente antes de
#: probar combinaciones.
_NO_ES_NOMBRE = frozenset({
    "CURP", "RFC", "NOMBRE", "NOMBRES", "APELLIDO", "APELLIDOS", "PATERNO",
    "MATERNO", "CALLE", "COLONIA", "EMAIL", "CORREO", "TEL", "TELEFONO",
    "USUARIO", "USER", "PASSWORD", "PASS", "CLAVE", "MAINCONTENT", "TXTNOMBRE",
    "TXTCURP", "TXTCORREO", "CP", "CODIGO", "POSTAL", "NUMERO", "DATOS",
    "FACTURACION", "HTTPS", "HTTP", "WWW", "COM", "MX", "ORG", "NET", "GMAIL",
    "HOTMAIL", "OUTLOOK", "YAHOO", "PDF", "DOC", "DOCX", "TXT", "XLS", "NULL",
    "NONE", "TRUE", "FALSE", "FECHA", "NACIMIENTO", "SEXO", "ESTADO", "MUNICIPIO",
    "DOMICILIO", "ENTIDAD", "FEDERATIVA", "SUPPORT", "CLOUD", "CHANNEL", "LOGS",
})

#: Palabras ENTERAS de solo letras. Los límites `\b` no son adorno: sin ellos, de
#: «HEGG560427MVZRRL04» se extraen «HEGG» y «MVZRRL» como si fueran apellidos, y como
#: salen de la propia CURP verifican contra ella y se cuelan en la ficha. Lo cazó un
#: test, con el resultado `nombre2: "HEGG"`.
_RE_PALABRA = re.compile(r"\b[A-ZÑ]{2,}\b")

#: Cuántas palabras alrededor del ancla se miran. La ventana la corta `anclas.VENTANA`
#: (±200 caracteres); esto acota el trabajo dentro de ella.
_MAX_PALABRAS = 40


def plegar_duro(texto: str) -> str:
    """Mayúsculas sin acentos. La Ñ se conserva: es significativa en los apellidos."""
    descompuesto = unicodedata.normalize("NFD", texto.upper())
    # Se recompone la Ñ (N + tilde) y se tiran el resto de diacríticos.
    sin_marcas = "".join(
        c for c in descompuesto
        if unicodedata.category(c) != "Mn" or c == "̃"
    )
    return unicodedata.normalize("NFC", sin_marcas)


def _vocal_interna(palabra: str) -> str:
    return next((c for c in palabra[1:] if c in _VOCALES), "X")


def _consonante_interna(palabra: str) -> str:
    for c in palabra[1:]:
        if c.isalpha() and c not in _VOCALES:
            # La Ñ no puede ir en esa posición de la CURP: RENAPO pone X.
            return "X" if c == "Ñ" else c
    return "X"


def _partes(texto: str) -> list[str]:
    """Palabras significativas: sin partículas y sin monosílabos sueltos."""
    return [p for p in texto.split() if p not in _PARTICULAS and len(p) > 1]


def codigo_curp(apellido1: str, apellido2: str, pila: str) -> tuple[str, str] | None:
    """Las siete letras que la CURP deriva del nombre: 4 iniciales + 3 consonantes.

    Devuelve `None` si falta lo imprescindible (apellido paterno y nombre de pila):
    sin eso no hay nada que comprobar y fingir un código daría falsos positivos.
    """
    pat, mat, nom = _partes(apellido1), _partes(apellido2), _partes(pila)
    if not pat or not nom:
        return None
    if len(nom) > 1 and nom[0] in _PILA_IGNORADA:
        nom = nom[1:]
    p, n = pat[0], nom[0]
    m = mat[0] if mat else ""
    iniciales = p[0] + _vocal_interna(p) + (m[0] if m else "X") + n[0]
    consonantes = (
        _consonante_interna(p)
        + (_consonante_interna(m) if m else "X")
        + _consonante_interna(n)
    )
    return iniciales, consonantes


def _acepta(esperado: str, derivado: str) -> bool:
    """Compara letra a letra tratando la X de la CURP como comodín.

    RENAPO pone X donde no puede derivar una letra y también donde las cuatro
    iniciales formarían una palabra malsonante (la lista de «palabras
    inconvenientes»). Exigir coincidencia exacta ahí rechazaría nombres correctos.
    """
    return all(a == b or a == "X" for a, b in zip(esperado, derivado, strict=True))


def casa_curp(curp: str, apellido1: str, apellido2: str, pila: str) -> bool:
    """¿Este nombre pudo generar esta CURP? Siete letras de comprobación."""
    codigo = codigo_curp(apellido1, apellido2, pila)
    if codigo is None or len(curp) < 16:
        return False
    iniciales, consonantes = codigo
    return _acepta(curp[:4], iniciales) and _acepta(curp[13:16], consonantes)


def casa_rfc(rfc: str, apellido1: str, apellido2: str, pila: str) -> bool:
    """¿Este nombre pudo generar este RFC? Solo cuatro letras: mucho más débil.

    El RFC de persona física comparte con la CURP las cuatro primeras letras pero no
    tiene las tres consonantes internas. Cuatro letras dejan pasar homónimos parciales
    con bastante facilidad, así que quien llame a esto debe además exigir cercanía —
    ver `extraer_de_contexto`.
    """
    codigo = codigo_curp(apellido1, apellido2, pila)
    if codigo is None or len(rfc) < 4:
        return False
    return _acepta(rfc[:4], codigo[0])


#: Radio en caracteres alrededor del ancla cuando solo hay RFC. Con cuatro letras de
#: comprobación un nombre ajeno verifica por casualidad con demasiada frecuencia en
#: ficheros con cientos de personas; exigir que además esté PEGADO al ancla es lo que
#: hace la diferencia. Para la CURP no hace falta: siete letras ya deciden solas.
_RADIO_RFC = 60


#: Separación máxima, en caracteres, entre dos palabras para seguir considerándolas
#: parte del mismo nombre. Un espacio deja 1; una coma y un espacio, 2. Cualquier cosa
#: mayor significa que en medio había algo que se descartó —un número, la propia CURP,
#: una etiqueta de formulario— y entonces las dos palabras NO son un nombre seguido.
_HUECO_MAXIMO = 3


def _palabras_candidatas(
    contexto: str, valor: str, radio: int | None
) -> list[tuple[str, int, int]]:
    """Palabras del contexto que pueden ser un nombre, CON su posición.

    La posición no es un extra: sin ella, descartar un token deja pegadas dos palabras
    que en el texto estaban lejos. Con «GLORIA HEGG560427MVZRRL04 ALTA», al tirar la
    CURP quedaban «GLORIA ALTA» seguidas y ALTA acababa de segundo nombre.

    Con `radio`, solo las que estén a esa distancia del ancla dentro del contexto.
    """
    plegado = plegar_duro(contexto)
    desplazamiento = 0
    if radio is not None:
        donde = plegado.find(valor.upper())
        if donde >= 0:
            inicio = max(0, donde - radio)
            plegado = plegado[inicio:donde + len(valor) + radio]
            desplazamiento = inicio
    palabras = [
        (m.group(0), m.start() + desplazamiento, m.end() + desplazamiento)
        for m in _RE_PALABRA.finditer(plegado)
        if m.group(0) not in _NO_ES_NOMBRE
    ]
    return palabras[:_MAX_PALABRAS]


def _tiras(palabras: list[tuple[str, int, int]]) -> Iterator[list[str]]:
    """Tiras de 2 a 4 palabras SEGUIDAS en el texto, las más largas primero.

    Las largas van antes porque «JUAN ANTONIO VILLARREAL NUÑEZ» y «ANTONIO VILLARREAL
    NUÑEZ» verifican las dos, y la completa es la buena. «Seguidas» se mide sobre el
    texto original, no sobre la lista: ver `_palabras_candidatas`.
    """
    for largo in (4, 3, 2):
        for i in range(len(palabras) - largo + 1):
            trozo = palabras[i:i + largo]
            if all(b[1] - a[2] <= _HUECO_MAXIMO for a, b in pairwise(trozo)):
                yield [p[0] for p in trozo]


def _recortar_cola(texto: str) -> str:
    """Deja solo la primera palabra significativa (con sus partículas delante).

    El ancla comprueba una palabra por campo: la inicial y las consonantes internas
    del apellido paterno, del materno y del primer nombre. Lo que venga DETRÁS del
    último campo de la tira no lo comprueba nadie, y se cuela: medido sobre datos
    reales salía «MAYRA CASTRO LARA CURPTUT», con el nombre bien y «CURPTUT» pegado
    al apellido materno.

    Se conservan las partículas porque son parte del apellido: «DE LA CRUZ» recorta a
    «DE LA CRUZ», no a «DE».
    """
    palabras = texto.split()
    for i, p in enumerate(palabras):
        if p not in _PARTICULAS:
            return " ".join(palabras[:i + 1])
    return texto


def _descomponer(tira: list[str], valida: Any) -> dict[str, str] | None:
    """Prueba los dos órdenes usuales y devuelve las partes si alguno verifica.

    En documentos mexicanos conviven «APELLIDOS NOMBRE» (lo típico de un padrón) y
    «NOMBRE APELLIDOS» (lo típico de un formulario). No se puede saber cuál es de
    antemano; se prueban los dos y decide el ancla, que es el único árbitro fiable.
    """
    n = len(tira)
    # Orden 1: apellido paterno, materno, y el resto es el nombre de pila. Aquí el
    # campo sin comprobar detrás es el nombre de pila, así que es el que se recorta.
    for corte in range(1, n):
        ap1, ap2 = tira[0], " ".join(tira[1:corte + 1])
        pila = " ".join(tira[corte + 1:])
        if pila and valida(ap1, ap2, pila):
            return _partido(ap1, ap2, _recortar_cola(pila))
    # Orden 2: nombre de pila delante, apellidos detrás. Ahora la cola es el materno.
    for corte in range(1, n):
        pila = " ".join(tira[:corte])
        ap1, ap2 = tira[corte], " ".join(tira[corte + 1:])
        if valida(ap1, ap2, pila):
            return _partido(ap1, _recortar_cola(ap2), pila)
    return None


def _partido(apellido1: str, apellido2: str, pila: str) -> dict[str, str]:
    """Las partes tal y como las espera la receta PERSONA."""
    nombres = pila.split()
    partes = {
        "apellido1": apellido1,
        "apellido2": apellido2,
        "nombre1": nombres[0] if nombres else "",
        "nombre2": " ".join(nombres[1:]),
    }
    return {k: v for k, v in partes.items() if v}


def extraer_de_contexto(tipo: str, valor: str, contexto: str | None) -> dict[str, str] | None:
    """El nombre de la persona de este ancla, o `None` si nada verifica.

    `tipo` es «curp» o «rfc»; `valor` el ancla; `contexto` el texto de alrededor que
    ya guarda `anclas.buscar_en_texto`. Devuelve las partes (`apellido1`, `apellido2`,
    `nombre1`, `nombre2`) listas para la receta PERSONA, o `None`.

    Nunca lanza: un fallo aquí debe costar el nombre, no la entidad.
    """
    if not contexto:
        return None
    try:
        radio: int | None
        if tipo == "curp":
            valida = lambda a1, a2, p: casa_curp(valor, a1, a2, p)  # noqa: E731
            radio = None
        elif tipo == "rfc":
            valida = lambda a1, a2, p: casa_rfc(valor, a1, a2, p)  # noqa: E731
            radio = _RADIO_RFC
        else:
            return None
        palabras = _palabras_candidatas(contexto, valor, radio)
        for tira in _tiras(palabras):
            partes = _descomponer(tira, valida)
            if partes:
                return partes
        return None
    except Exception:
        return None
