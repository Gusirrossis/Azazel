"""Qué columnas llevan IDENTIDAD, y por qué el orden importa.

Cuando hay que volcar filas a texto con un presupuesto de caracteres, el orden de las
columnas decide si el documento sirve. Una tabla de 40 columnas donde la CURP es la
número 35 y hay tres campos de notas largas gasta el presupuesto en las notas y deja
fuera justo el dato que convierte al documento en una persona encontrable.

Vive en `core/` y no dentro de un extractor porque lo usan varios: el tabular
(CSV/NDJSON), el de SQLite y —cuando existan— los lotes de un contenedor tabular.
Si cada uno tuviera su lista, dos formatos con los mismos datos producirían
documentos distintos según por dónde entraron.
"""

from __future__ import annotations

#: Fragmentos que, en el NOMBRE de una columna, sugieren identidad. Se comparan en
#: minúsculas y por subcadena: `CURP_TITULAR` y `curp` puntúan igual.
PISTAS_IDENTIDAD: tuple[str, ...] = (
    # anclas duras: de aquí salen las entidades
    "curp", "rfc", "nss", "clave_elector", "claveelector", "elector", "ine", "credencial",
    # nombre de la persona
    "nombre", "apellido", "paterno", "materno", "razon_social", "razonsocial",
    # contacto
    "email", "correo", "telefono", "celular", "movil", "whatsapp",
    # domicilio
    "domicilio", "direccion", "calle", "colonia", "municipio", "delegacion", "cp",
    "codigo_postal", "estado", "entidad",
    # otros identificadores frecuentes en padrones
    "fecha_nac", "nacimiento", "folio", "expediente", "cuenta", "cliente", "usuario",
    "placa", "serie", "vin", "matricula",
)


def puntuar(columna: str) -> int:
    """Cuántas pistas de identidad contiene el nombre de la columna."""
    bajo = columna.lower()
    return sum(1 for pista in PISTAS_IDENTIDAD if pista in bajo)


def ordenar_por_identidad(columnas: list[str]) -> list[str]:
    """Las que llevan identidad primero; el resto conserva su orden original.

    Estable a propósito: dos columnas sin pistas salen en el orden en que venían, que
    suele ser el orden en que las escribió quien hizo la tabla.
    """
    return sorted(columnas, key=lambda c: (-puntuar(c), columnas.index(c)))


def tiene_identidad(columnas: list[str]) -> bool:
    return any(puntuar(c) for c in columnas)
