"""Anclas dentro de un texto: dónde están y qué las rodea.

Hasta aquí, encontrar CURPs y RFCs vivía dentro de `backfill.py`, que recorre el
ÍNDICE en una pasada manual. Eso deja dos huecos:

  1. Un documento recién OCR-eado no se convierte en entidad hasta que alguien
     re-lanza el backfill entero.
  2. Solo se guarda el ancla. El nombre y el domicilio que están en la línea de al
     lado se pierden, porque asociarlos a la persona correcta necesita NER (E8).

Este módulo saca la detección a un sitio compartido y añade la **ventana de
contexto**: los ±N caracteres alrededor de cada ancla. Guardarla cuesta unos bytes
por documento y es justo lo que el NER necesitará; sin ella habría que volver a leer
—y a OCR-ear— el corpus entero cuando llegue esa fase.

Las expresiones son LIBERALES a propósito: encuentran candidatos, y el validador con
dígito verificador es el que decide. Es el mismo criterio que ya usaba el backfill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import nombres
from . import normalizadores as N

# Lookarounds para no cortar dentro de una cadena alfanumérica mayor (un hash, un id).
SCAN_CURP = re.compile(r"(?<![0-9A-ZÑ])[A-ZÑ]{4}\d{6}[HM][A-ZÑ]{5}[0-9A-ZÑ]\d(?![0-9A-ZÑ])")
SCAN_RFC = re.compile(r"(?<![0-9A-ZÑ&])[A-ZÑ&]{4}\d{6}[0-9A-ZÑ]{3}(?![0-9A-ZÑ])")

#: Caracteres a cada lado del ancla que se conservan como contexto.
VENTANA = 200

#: Tope de ventanas por documento. Un padrón con 5 000 CURPs no debe inflar el doc
#: del índice con un megabyte de contexto: para esos casos el valor está en la tabla,
#: no en el texto suelto.
MAX_VENTANAS = 40


@dataclass(frozen=True, slots=True)
class Ancla:
    tipo: str  # 'curp' | 'rfc'
    valor: str
    inicio: int
    contexto: str

    def como_dict(self) -> dict[str, Any]:
        return {"tipo": self.tipo, "valor": self.valor, "contexto": self.contexto}


def buscar_en_texto(texto: str | None, *, ventana: int = VENTANA) -> list[Ancla]:
    """Anclas VÁLIDAS de un texto, con su contexto. Únicas y en orden de aparición.

    Se trabaja sobre el texto en mayúsculas para casar, pero el contexto se corta del
    ORIGINAL: un contexto en mayúsculas sería mucho menos útil para el NER, que se
    apoya en la capitalización para reconocer nombres propios.
    """
    if not texto:
        return []
    # `str.upper()` puede CAMBIAR LA LONGITUD (la ß alemana pasa a SS, la ligadura ﬁ
    # a FI…). Basta un carácter así antes del ancla para que los offsets del texto en
    # mayúsculas ya no correspondan al original, y la ventana de contexto se corte en
    # el sitio equivocado — o quede vacía. `str.casefold()` tiene el mismo problema.
    # Se mapea carácter a carácter, conservando la longitud: lo que no tiene mayúscula
    # de un solo carácter se deja como está, y el patrón no lo necesita.
    arriba = "".join(c.upper() if len(c.upper()) == 1 else c for c in texto)
    assert len(arriba) == len(texto)
    encontradas: list[Ancla] = []
    vistos: set[tuple[str, str]] = set()

    for tipo, patron, validar in (
        ("curp", SCAN_CURP, N.validar_curp),
        ("rfc", SCAN_RFC, N.validar_rfc),
    ):
        for m in patron.finditer(arriba):
            n = validar(m.group())
            if not (n.valido and n.valor):
                continue
            clave = (tipo, n.valor)
            if clave in vistos:
                continue
            vistos.add(clave)
            inicio = m.start()
            desde = max(0, inicio - ventana)
            hasta = min(len(texto), m.end() + ventana)
            encontradas.append(
                Ancla(tipo=tipo, valor=n.valor, inicio=inicio, contexto=texto[desde:hasta])
            )

    encontradas.sort(key=lambda a: a.inicio)
    return encontradas


def filas_persona(anclas: list[Ancla]) -> list[dict[str, str]]:
    """Agrupa las anclas en filas-persona, sin duplicar ni perder ninguna.

    Misma regla que venía usando el backfill, ahora en un solo sitio:
      · una fila por CURP;
      · un RFC enriquece una CURP SOLO si comparten los 10 primeros caracteres
        (4 letras del nombre + AAMMDD), que es garantía fuerte de misma persona;
      · un RFC que no se asocia a ninguna CURP ancla su propia persona.
    """
    por_curp = {a.valor: a for a in anclas if a.tipo == "curp"}
    por_rfc = {a.valor: a for a in anclas if a.tipo == "rfc"}
    curps, rfcs = list(por_curp), list(por_rfc)
    filas: list[dict[str, str]] = []
    asociados: set[str] = set()

    for c in curps:
        fila = {"curp": c}
        contextos = [por_curp[c].contexto]
        mismos = [r for r in rfcs if r[:10] == c[:10] and r not in asociados]
        if len(mismos) == 1:
            fila["rfc"] = mismos[0]
            asociados.add(mismos[0])
            # El nombre se comprueba SIEMPRE contra la CURP —siete letras frente a las
            # cuatro del RFC— pero se busca también alrededor del RFC: son la misma
            # persona, y el nombre puede estar junto a uno y no junto al otro.
            contextos.append(por_rfc[mismos[0]].contexto)
        for contexto in contextos:
            partes = nombres.extraer_de_contexto("curp", c, contexto)
            if partes:
                fila.update(partes)
                break
        filas.append(fila)

    for r in rfcs:
        if r not in asociados:
            fila = {"rfc": r}
            partes = nombres.extraer_de_contexto("rfc", r, por_rfc[r].contexto)
            if partes:
                fila.update(partes)
            filas.append(fila)
    return filas


def contexto_para_doc(anclas: list[Ancla], *, maximo: int = MAX_VENTANAS) -> list[dict[str, Any]]:
    """Las ventanas que se guardan en el documento, acotadas (ver MAX_VENTANAS)."""
    return [a.como_dict() for a in anclas[:maximo]]
