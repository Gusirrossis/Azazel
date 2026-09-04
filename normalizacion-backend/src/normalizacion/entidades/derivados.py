"""Lo que NO se guarda porque se puede recalcular, y dónde se vuelve a poner.

Una entidad resuelta desde una CURP encontrada en texto —el caso que produce el
backfill— guardaba 27 claves de las que 20 estaban vacías, y de las 7 con valor,
tres eran copias: `curp` ≡ `normalizados.normalized_curp`, `sexo` ≡
`normalized_sex`. 597 bytes para transportar 18 caracteres de información.

La regla que aplica este módulo:

  * Se GUARDA el mínimo canónico: las anclas y lo que de verdad se capturó de una
    fuente. Las claves vacías no se escriben.
  * Se DERIVA al leer todo lo que sale de las anclas. La CURP ya contiene el sexo,
    la fecha y el estado de nacimiento; recalcularlos cuesta microsegundos y evita
    almacenar el mismo dato dos veces en cientos de miles de filas.

El contrato NO cambia: `enriquecer` reconstruye la forma completa, así que la
proyección al AEB (`proyeccion.py`, que lee `normalizados.normalized_dob` por ruta)
y el panel siguen viendo exactamente lo mismo que antes.
"""

from __future__ import annotations

from typing import Any

from . import normalizadores as N

#: Bloques que se derivan y por tanto no se almacenan.
DERIVADOS: tuple[str, ...] = ("normalizados", "edad")


def podar(campos: dict[str, Any]) -> dict[str, Any]:
    """Quita lo derivable y lo vacío. Es lo que se escribe en la base.

    Recursivo, porque los sub-objetos (`nombre`, `direccion`) son justo donde se
    acumulan los huecos: una persona anclada solo en CURP los tiene todos vacíos.
    Un sub-objeto que se queda sin claves desaparece entero.
    """
    limpio: dict[str, Any] = {}
    for clave, valor in campos.items():
        if clave in DERIVADOS:
            continue
        if isinstance(valor, dict):
            dentro = podar(valor)
            if dentro:
                limpio[clave] = dentro
        elif valor not in (None, "", [], {}):
            limpio[clave] = valor
    return limpio


def enriquecer(campos: dict[str, Any]) -> dict[str, Any]:
    """Devuelve la ficha COMPLETA a partir de lo guardado. No muta la entrada.

    Es la inversa de `podar`: recalcula desde las anclas lo que se dejó de escribir.
    Todo lector debe pasar por aquí — la base guarda una forma reducida y el resto
    del sistema espera la de siempre.
    """
    lleno = dict(campos)

    curp = lleno.get("curp")
    rfc = lleno.get("rfc")
    deriv: dict[str, Any] = {}
    if curp:
        n = N.validar_curp(str(curp))
        if n.valido:
            deriv = dict(n.derivados or {})
    if not deriv.get("dob") and rfc:
        n = N.validar_rfc(str(rfc))
        if n.valido:
            deriv["dob"] = (n.derivados or {}).get("dob")

    dob = deriv.get("dob")
    sexo = lleno.get("sexo") or deriv.get("sexo")
    if sexo and not lleno.get("sexo"):
        lleno["sexo"] = sexo

    # La edad cambia con el tiempo: guardarla es garantizar que envejece mal. Se
    # calcula al leer, que además la hace correcta el día que se lee.
    edad = N.calcular_edad(dob) if dob else None
    lleno["edad"] = str(edad) if edad is not None else None

    nombre_completo = lleno.get("nombre_completo") or ""
    municipio = (lleno.get("direccion") or {}).get("municipio")
    lleno["normalizados"] = {
        "normalized_name": N.plegar(nombre_completo) if nombre_completo else None,
        "normalized_dob": dob,
        "normalized_curp": curp,
        # Estado de NACIMIENTO (de la CURP), que NO es el de residencia de la
        # dirección. Mezclarlos fue una tentación desde el principio.
        "normalized_sex": sexo,
        "normalized_estado": deriv.get("estado"),
        "normalized_mpio": N.plegar(str(municipio)) if municipio else None,
    }
    return lleno


def ficha_breve(campos: dict[str, Any]) -> dict[str, Any]:
    """La ficha mínima que se manda a un consumidor externo (Lilith).

    Deliberadamente NO lleva `procedencias`: pueden ser cientos de rutas de archivo
    por entidad, y quien federa quiere saber QUIÉN es y cuántas veces aparece, no la
    lista completa de dónde. Si le hace falta el detalle, pide la entidad por su id.
    """
    lleno = enriquecer(campos)
    breve = {
        "nombre_completo": lleno.get("nombre_completo") or None,
        "curp": lleno.get("curp"),
        "rfc": lleno.get("rfc"),
        "sexo": lleno.get("sexo"),
        "edad": lleno.get("edad"),
        "email": lleno.get("email"),
        "telefono": lleno.get("telefono"),
    }
    return {k: v for k, v in breve.items() if v}
