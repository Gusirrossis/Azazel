"""Receta = definición DINÁMICA de un tipo de entidad como DATOS (no código).

Una receta declara: qué campos canónicos tiene, qué normalizador usa cada uno,
cuáles son anclas, y los sinónimos de nombres de columna para proponer el mapeo.
Añadir un tipo de entidad nuevo = otra receta, sin tocar el motor.

Fz1 (persona) es la primera receta. Versionada: cambiar la receta = nueva
`version_receta`, reproyectable sin re-leer discos.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CampoReceta:
    nombre: str
    normalizador: str  # 'curp'|'rfc'|'email'|'telefono'|'nombre'|'texto'
    es_ancla: bool = False
    sinonimos: tuple[str, ...] = ()  # nombres de columna candidatos (ya plegados)


@dataclass(frozen=True)
class Receta:
    tipo: str
    version: str
    campos: tuple[CampoReceta, ...]
    # `kind` del AEB al que mapea este tipo (vocabulario del orquestador: person, acceso, …).
    kind: str = "unknown"

    def por_nombre(self, nombre: str) -> CampoReceta | None:
        return next((c for c in self.campos if c.nombre == nombre), None)


# Persona Fz1 — campos escalares de E1-E3 (el grafo de relaciones y NER vienen en E5/E8).
PERSONA_FZ1 = Receta(
    tipo="persona",
    version="fz1-v1",
    kind="person",
    campos=(
        CampoReceta("curp", "curp", es_ancla=True, sinonimos=("curp", "clave curp", "clave unica")),
        CampoReceta("rfc", "rfc", es_ancla=True, sinonimos=("rfc", "registro federal")),
        CampoReceta("email", "email", es_ancla=True,
                    sinonimos=("email", "correo", "correo electronico", "e mail", "mail")),
        CampoReceta("telefono", "telefono", es_ancla=True,
                    sinonimos=("telefono", "tel", "celular", "movil", "telefono movil")),
        CampoReceta("nombre1", "nombre",
                    sinonimos=("nombre", "nombres", "primer nombre", "nombre1")),
        CampoReceta("nombre2", "nombre", sinonimos=("segundo nombre", "nombre2")),
        CampoReceta("apellido1", "nombre",
                    sinonimos=("apellido paterno", "primer apellido", "apellido1", "paterno")),
        CampoReceta("apellido2", "nombre",
                    sinonimos=("apellido materno", "segundo apellido", "apellido2", "materno")),
        CampoReceta("alias", "texto", sinonimos=("alias", "apodo", "nickname")),
        CampoReceta("sexo", "texto", sinonimos=("sexo", "genero", "sex")),
        CampoReceta("calle", "texto", sinonimos=("calle", "domicilio", "direccion")),
        CampoReceta("numero_exterior", "texto",
                    sinonimos=("numero exterior", "num ext", "no exterior", "numero")),
        CampoReceta("numero_interior", "texto",
                    sinonimos=("numero interior", "num int", "interior", "depto")),
        CampoReceta("colonia", "texto", sinonimos=("colonia", "col")),
        CampoReceta("municipio", "texto",
                    sinonimos=("municipio", "alcaldia", "delegacion", "mpio")),
        CampoReceta("codigo_postal", "texto",
                    sinonimos=("codigo postal", "cp", "c p", "codpostal")),
        CampoReceta("estado", "texto", sinonimos=("estado", "entidad", "entidad federativa")),
        CampoReceta("pais", "texto", sinonimos=("pais",)),
        CampoReceta("relacion", "texto", sinonimos=("relacion", "parentesco", "vinculo")),
    ),
)

# Acceso — credencial filtrada (combolists, breaches): usuario/correo/contraseña + origen.
# Ancla por EMAIL (el correo): todos los accesos de un mismo correo resuelven a la misma entidad.
# Limitación v1: la fusión conserva el primer dato no vacío, así que una sola contraseña por correo
# (múltiples credenciales del mismo correo se consolidan; el historial completo es trabajo futuro).
ACCESO = Receta(
    tipo="acceso",
    version="acceso-v1",
    kind="acceso",
    campos=(
        CampoReceta("email", "email", es_ancla=True,
                    sinonimos=("email", "correo", "correo electronico", "e mail", "mail", "usuario correo")),
        CampoReceta("usuario", "texto",
                    sinonimos=("usuario", "username", "user", "cuenta", "login", "nick", "handle")),
        CampoReceta("contrasena", "texto",
                    sinonimos=("contrasena", "contraseña", "password", "pass", "clave", "pwd")),
        CampoReceta("dominio", "texto", sinonimos=("dominio", "domain", "sitio", "site", "host")),
        CampoReceta("url", "texto", sinonimos=("url", "enlace", "link", "direccion url")),
        CampoReceta("fuente", "texto",
                    sinonimos=("fuente", "breach", "origen", "leak", "base", "filtracion", "combo")),
    ),
)

RECETAS: dict[str, Receta] = {PERSONA_FZ1.tipo: PERSONA_FZ1, ACCESO.tipo: ACCESO}


def obtener_receta(tipo: str) -> Receta:
    if tipo not in RECETAS:
        raise ValueError(f"receta desconocida: {tipo}")
    return RECETAS[tipo]
