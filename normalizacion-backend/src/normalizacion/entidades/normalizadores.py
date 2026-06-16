"""Normalizadores y validadores reutilizables (no por entidad, por TIPO de dato).

Cada función es PURA: valida + normaliza + (cuando aplica) deriva, y reporta su
confianza. Lo que no valida se conserva crudo + bandera (jamás se descarta) — el
mismo principio del frío reversible de la Fase 1.

La pieza estrella es la CURP: su dígito verificador permite VALIDAR, y de sus
posiciones se DERIVA determinísticamente fecha de nacimiento, sexo y estado.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Normalizado:
    """Resultado de un normalizador: valor canónico, validez y el crudo original."""

    valor: str | None
    valido: bool
    crudo: str
    derivados: dict[str, str] | None = None  # p. ej. {dob, sexo, estado} de la CURP


# ---------------------------------------------------------------- utilidades

def plegar(texto: str) -> str:
    """Minúsculas + plegado de acentos (NFKD) para COMPARAR. Conserva la ñ→n.

    'José MUÑOZ' → 'jose munoz'. Se usa solo para blocking/comparación; el valor
    de presentación conserva sus acentos.
    """
    desc = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(c for c in desc if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sin_acentos).strip().lower()


# ------------------------------------------------------------------- CURP

# Estados de nacimiento (clave de 2 letras de la CURP → nombre). "NE" = nacido
# en el extranjero. "DF" es el código histórico de la Ciudad de México.
ESTADOS_CURP: dict[str, str] = {
    "AS": "Aguascalientes", "BC": "Baja California", "BS": "Baja California Sur",
    "CC": "Campeche", "CL": "Coahuila", "CM": "Colima", "CS": "Chiapas",
    "CH": "Chihuahua", "DF": "CDMX", "DG": "Durango", "GT": "Guanajuato",
    "GR": "Guerrero", "HG": "Hidalgo", "JC": "Jalisco", "MC": "Estado de Mexico",
    "MN": "Michoacan", "MS": "Morelos", "NT": "Nayarit", "NL": "Nuevo Leon",
    "OC": "Oaxaca", "PL": "Puebla", "QT": "Queretaro", "QR": "Quintana Roo",
    "SP": "San Luis Potosi", "SL": "Sinaloa", "SR": "Sonora", "TC": "Tabasco",
    "TS": "Tamaulipas", "TL": "Tlaxcala", "VZ": "Veracruz", "YN": "Yucatan",
    "ZS": "Zacatecas", "NE": "Nacido en el Extranjero",
}

# Alfabeto oficial para el dígito verificador de la CURP (incluye la Ñ).
_DICC_CURP = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
_RE_CURP = re.compile(
    r"^[A-Z][A-Z]{3}\d{6}[HM][A-Z]{2}[A-Z]{3}[A-Z0-9]\d$"
)


def digito_verificador_curp(curp17: str) -> str:
    """Calcula el 18º carácter (dígito verificador) de los 17 primeros.

    Algoritmo oficial RENAPO: suma ponderada por posición (18..2) sobre el
    alfabeto, luego 10 − (suma mod 10), con 10 → 0.
    """
    suma = 0
    for i, c in enumerate(curp17):
        suma += _DICC_CURP.find(c) * (18 - i)
    return str((10 - (suma % 10)) % 10)


def _fecha_valida(aa: str, mm: str, dd: str, siglo: str) -> str | None:
    try:
        d = date(int(siglo + aa), int(mm), int(dd))
    except ValueError:
        return None
    return d.isoformat()


def validar_curp(curp: str) -> Normalizado:
    """Valida formato + dígito verificador y DERIVA fecha/sexo/estado.

    Una CURP que no pasa el dígito verificador es casi seguro un error de captura
    o un número que solo PARECE CURP — se marca inválida (crudo conservado).
    """
    crudo = curp or ""
    c = re.sub(r"\s+", "", crudo).upper()
    if len(c) != 18 or not _RE_CURP.match(c):
        return Normalizado(None, False, crudo)

    # Siglo: el 17º char (índice 16) es la HOMOCLAVE de RENAPO — numérico = nacido
    # en los 1900s, alfabético = en los 2000s (también desambigua homónimos).
    siglo = "19" if c[16].isdigit() else "20"
    dob = _fecha_valida(c[4:6], c[6:8], c[8:10], siglo)
    estado = ESTADOS_CURP.get(c[11:13])
    if dob is None or estado is None:
        return Normalizado(None, False, crudo)
    if digito_verificador_curp(c[:17]) != c[17]:
        return Normalizado(None, False, crudo)

    return Normalizado(
        valor=c,
        valido=True,
        crudo=crudo,
        derivados={"dob": dob, "sexo": c[10], "estado": estado},
    )


# -------------------------------------------------------------------- RFC

_RE_RFC_FISICA = re.compile(r"^[A-ZÑ&]{4}\d{6}[A-Z0-9]{3}$")


def validar_rfc(rfc: str) -> Normalizado:
    """RFC de persona física (13 chars). Valida formato + fecha y deriva la fecha.

    (El dígito verificador del RFC usa otra tabla; se añade en E4. Para el ancla
    basta el formato + fecha válida, que ya descarta casi todo el ruido.)
    """
    crudo = rfc or ""
    r = re.sub(r"\s+", "", crudo).upper()
    if len(r) != 13 or not _RE_RFC_FISICA.match(r):
        return Normalizado(None, False, crudo)
    # El RFC NO lleva bit de siglo (a diferencia de la CURP): heurística de corte en
    # el año 30 (31-99 → 19xx, 00-30 → 20xx). LIMITACIÓN: para nacidos 2031+ habrá
    # que revisar (post-2030) — por eso la CURP es el ancla preferida.
    siglo = "19" if int(r[4:6]) > 30 else "20"
    dob = _fecha_valida(r[4:6], r[6:8], r[8:10], siglo)
    if dob is None:
        return Normalizado(None, False, crudo)
    return Normalizado(valor=r, valido=True, crudo=crudo, derivados={"dob": dob})


# --------------------------------------------------------------- teléfono MX

def normalizar_telefono_mx(tel: str) -> Normalizado:
    """A 10 dígitos nacionales. Quita +52 / 52 / 1 (lada de larga distancia)."""
    crudo = tel or ""
    digitos = re.sub(r"\D", "", crudo)
    # Quita el prefijo de país/larga-distancia y deja 10 dígitos nacionales. Se
    # evalúa de más específico (13) a menos para que '521…' no se confunda con '52…'.
    if len(digitos) == 13 and digitos.startswith("521"):   # +52 1 (móvil, formato antiguo)
        digitos = digitos[3:]
    elif len(digitos) == 12 and digitos.startswith("52"):  # +52
        digitos = digitos[2:]
    elif len(digitos) == 12 and digitos.startswith("01"):  # 01 (lada nacional antigua)
        digitos = digitos[2:]
    elif len(digitos) == 11 and digitos.startswith("1"):   # 1 (larga distancia)
        digitos = digitos[1:]
    if len(digitos) != 10:
        return Normalizado(None, False, crudo)
    return Normalizado(valor=digitos, valido=True, crudo=crudo)


# ------------------------------------------------------------------ email

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalizar_email(email: str) -> Normalizado:
    crudo = email or ""
    e = crudo.strip().lower()
    if not _RE_EMAIL.match(e):
        return Normalizado(None, False, crudo)
    return Normalizado(valor=e, valido=True, crudo=crudo)


# ------------------------------------------------------------------ nombre

def normalizar_nombre(nombre: str) -> Normalizado:
    """Colapsa espacios y conserva el nombre de presentación; el `derivados.plegado`
    (sin acentos, minúsculas) es la clave de comparación/blocking."""
    crudo = nombre or ""
    limpio = re.sub(r"\s+", " ", crudo).strip()
    if not limpio:
        return Normalizado(None, False, crudo)
    return Normalizado(valor=limpio, valido=True, crudo=crudo, derivados={"plegado": plegar(limpio)})


# ---------------------------------------------------------------- edad

def calcular_edad(dob_iso: str, hoy: date | None = None) -> int | None:
    """Edad en años cumplidos a partir de una fecha ISO (YYYY-MM-DD)."""
    try:
        nac = date.fromisoformat(dob_iso)
    except ValueError:
        return None
    ref = hoy or date.today()
    return ref.year - nac.year - ((ref.month, ref.day) < (nac.month, nac.day))
