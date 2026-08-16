"""Fases B+C+F (rebanada E1-E3): registro mapeado → entidad canónica resuelta.

Flujo por fila: aplicar el mapeo columna→campo → NORMALIZAR cada campo (validando
anclas y DERIVANDO de la CURP) → elegir el ancla fuerte → UPSERT idempotente en
`entidades` (misma CURP en dos fuentes = la misma entidad, sin duplicar; los
campos faltantes se rellenan y las procedencias se acumulan).

El scoring difuso (Splink), el clustering y el grafo de relaciones son E4-E5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

from . import normalizadores as N
from .modelo import AnclaTipo, calcular_entidad_id
from .receta import Receta

log = obtener_logger("entidades")

# Normalizador del campo-ancla → AnclaTipo del AEB. La selección de ancla es DATA-DRIVEN
# (lee los campos `es_ancla` de la receta, en orden de declaración), no cableada a persona.
_NORMALIZADOR_A_ANCLA: dict[str, AnclaTipo] = {
    "curp": AnclaTipo.CURP, "rfc": AnclaTipo.RFC,
    "email": AnclaTipo.EMAIL, "telefono": AnclaTipo.TELEFONO,
}


def _elegir_ancla(receta: Receta, val: Any) -> tuple[AnclaTipo, str] | None:
    """Primer campo `es_ancla` con valor válido, en el orden en que la receta los declara.
    Para persona reproduce CURP > RFC > email > teléfono; para acceso da email."""
    for c in receta.campos:
        if c.es_ancla:
            v = val(c.nombre)
            if v:
                at = _NORMALIZADOR_A_ANCLA.get(c.normalizador)
                if at is not None:
                    return at, v
    return None


@dataclass
class ResumenProyeccion:
    filas: int = 0
    entidades_nuevas: int = 0
    entidades_fusionadas: int = 0
    sin_ancla: int = 0
    errores: int = 0

    def como_dict(self) -> dict[str, int]:
        return {
            "filas": self.filas, "entidades_nuevas": self.entidades_nuevas,
            "entidades_fusionadas": self.entidades_fusionadas,
            "sin_ancla": self.sin_ancla, "errores": self.errores,
        }


def _norm_campo(normalizador: str, crudo: str) -> N.Normalizado:
    match normalizador:
        case "curp": return N.validar_curp(crudo)
        case "rfc": return N.validar_rfc(crudo)
        case "email": return N.normalizar_email(crudo)
        case "telefono": return N.normalizar_telefono_mx(crudo)
        case "nombre": return N.normalizar_nombre(crudo)
        case _:  # 'texto'
            v = (crudo or "").strip()
            return N.Normalizado(v or None, bool(v), crudo)


def construir_entidad(
    receta: Receta, asignacion: dict[str, str], fila: dict[str, Any],
    atributos_declarados: tuple[dict[str, str], ...] = (),
) -> dict[str, Any] | None:
    """Normaliza una fila a la forma Fz1. Devuelve {campos, ancla_tipo, ancla_valor}
    o None si no hay ancla fuerte (esos van a E4: resolución difusa).

    `atributos_declarados` [{nombre, normalizador}] captura datos EXTRA (color_favorito,
    placa…) en `campos.atributos` cuando vienen mapeados; lo no declarado se descarta."""
    # 1) columna → campo canónico (valor crudo)
    crudos: dict[str, str] = {}
    for col, campo in asignacion.items():
        if campo and col in fila and fila[col] not in (None, ""):
            crudos[campo] = str(fila[col])

    # 2) normalizar + validar cada campo
    norm: dict[str, N.Normalizado] = {}
    for campo, crudo in crudos.items():
        cr = receta.por_nombre(campo)
        norm[campo] = _norm_campo(cr.normalizador if cr else "texto", crudo)

    def val(campo: str) -> str | None:
        n = norm.get(campo)
        return n.valor if n and n.valido else None

    # Tipos NO-persona (acceso, …): campos PLANOS directos desde la receta (data-driven).
    # La forma anidada/derivaciones de abajo es exclusiva de persona (las manifests del AEB
    # dependen de ella). Añadir otro tipo = otra receta, sin tocar esta rama.
    if receta.tipo != "persona":
        campos_g = {c.nombre: val(c.nombre) for c in receta.campos}
        ancla_g = _elegir_ancla(receta, val)
        if ancla_g is None:
            return None
        return {"campos": campos_g, "ancla_tipo": ancla_g[0], "ancla_valor": ancla_g[1]}

    # 3) derivaciones desde la CURP (sexo, dob, estado) — el ancla de oro
    deriv = norm["curp"].derivados if norm.get("curp") and norm["curp"].valido else None
    dob = (deriv or {}).get("dob") or (
        norm["rfc"].derivados.get("dob") if norm.get("rfc") and norm["rfc"].valido else None
    )
    sexo = (deriv or {}).get("sexo") or val("sexo")
    # Estado de NACIMIENTO (derivado de la CURP) ≠ estado de RESIDENCIA (de la
    # dirección): son cosas distintas y NO se mezclan. normalized_estado es el de
    # nacimiento (None si no hay CURP válida); direccion.estado es el de residencia.
    estado_nacimiento = (deriv or {}).get("estado")
    edad = N.calcular_edad(dob) if dob else None

    nombre = {k: val(k) or "" for k in ("nombre1", "nombre2", "apellido1", "apellido2")}
    nombre_completo = " ".join(p for p in nombre.values() if p).strip()

    campos = {
        "nombre": nombre,
        "nombre_completo": nombre_completo,
        "alias": val("alias"),
        "curp": val("curp"),
        "rfc": val("rfc"),
        "sexo": sexo,
        "edad": str(edad) if edad is not None else None,
        "direccion": {k: val(k) for k in (
            "calle", "numero_exterior", "numero_interior", "colonia",
            "municipio", "codigo_postal", "estado", "pais")},
        "email": val("email"),
        "telefono": val("telefono"),
        "relacion": val("relacion"),
        "normalizados": {
            "normalized_name": N.plegar(nombre_completo) if nombre_completo else None,
            "normalized_dob": dob,
            "normalized_curp": val("curp"),
            "normalized_sex": sexo,
            "normalized_estado": estado_nacimiento,
            "normalized_mpio": N.plegar(val("municipio")) if val("municipio") else None,
        },
    }

    # 3b) atributos EXTRA declarados (lo demás se descarta): se acumulan en su propia
    # bolsa, que la fusión combina recursivamente entre fuentes.
    atributos: dict[str, Any] = {}
    for attr in atributos_declarados:
        if receta.por_nombre(attr["nombre"]):  # es campo del núcleo: NO duplicar en la bolsa
            continue
        crudo = crudos.get(attr["nombre"])
        if crudo:
            n = _norm_campo(attr.get("normalizador", "texto"), crudo)
            if n.valido and n.valor:
                atributos[attr["nombre"]] = n.valor
    if atributos:
        campos["atributos"] = atributos

    # 4) elegir el ancla fuerte (data-driven: CURP > RFC > email > teléfono para persona)
    ancla = _elegir_ancla(receta, val)
    if ancla is None:
        return None
    return {"campos": campos, "ancla_tipo": ancla[0], "ancla_valor": ancla[1]}


def _clave_procedencia(p: Any) -> str:
    """Clave estable para deduplicar procedencias: por archivo_id si lo trae, si no
    por el contenido serializado."""
    if isinstance(p, dict) and p.get("archivo_id"):
        return f"id:{p['archivo_id']}"
    return "j:" + json.dumps(p, sort_keys=True, ensure_ascii=False)


def _fusionar_campos(viejo: dict[str, Any], nuevo: dict[str, Any]) -> dict[str, Any]:
    """Rellena lo faltante sin pisar lo que ya había (preferimos el primer dato no
    vacío). Recursivo para los sub-objetos (nombre, direccion, normalizados)."""
    out = dict(viejo)
    for k, v in nuevo.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _fusionar_campos(out[k], v)
        elif out.get(k) in (None, "", {}) and v not in (None, ""):
            out[k] = v
    return out


def _upsert(
    conn: psycopg.Connection[Any], entidad_id: str, tipo: str, ancla_tipo: AnclaTipo,
    ancla_valor: str, campos: dict[str, Any], version_receta: str,
    version_resolucion: str, procedencia: dict[str, Any] | None,
) -> str:
    """Idempotente y SIN carrera. Devuelve 'nueva' o 'fusionada'.

    Dos workers con la misma CURP no pueden duplicar ni reventar por PK: el
    INSERT ... ON CONFLICT DO NOTHING resuelve la carrera (uno inserta, el otro
    no-opera); el que pierde toma el camino de fusión, bloqueando la fila con
    FOR UPDATE (que ya existe) antes de combinar. Un SELECT-luego-INSERT NO sería
    seguro: FOR UPDATE no bloquea filas inexistentes.
    """
    procs_nuevas = [procedencia] if procedencia else []
    cur = conn.execute(
        "INSERT INTO entidades (entidad_id, tipo, ancla_tipo, ancla_valor, campos,"
        " version_receta, version_resolucion, procedencias)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (entidad_id) DO NOTHING",
        (entidad_id, tipo, ancla_tipo.value, ancla_valor, Jsonb(campos),
         version_receta, version_resolucion, Jsonb(procs_nuevas)),
    )
    if cur.rowcount == 1:
        return "nueva"
    # Ya existía → bloquear la fila (ahora sí existe) y fusionar de forma serializada.
    fila = conn.execute(
        "SELECT campos, procedencias FROM entidades WHERE entidad_id = %s FOR UPDATE",
        (entidad_id,),
    ).fetchone()
    assert fila is not None  # la fila existe: o la insertó otro, o ya estaba
    fusion = _fusionar_campos(dict(fila[0]), campos)
    # Procedencia idempotente: no re-acumular la MISMA fuente (clave por archivo_id si
    # lo trae —backfill—, si no por el contenido). Re-correr no infla las fuentes.
    procs = list(fila[1])
    vistas = {_clave_procedencia(p) for p in procs}
    for p in procs_nuevas:
        k = _clave_procedencia(p)
        if k not in vistas:
            procs.append(p)
            vistas.add(k)
    conn.execute(
        "UPDATE entidades SET campos = %s, procedencias = %s,"
        " version_resolucion = %s, actualizado_en = now() WHERE entidad_id = %s",
        (Jsonb(fusion), Jsonb(procs), version_resolucion, entidad_id),
    )
    return "fusionada"


def proyectar(
    config: Config, receta: Receta, asignacion: dict[str, str],
    filas: list[dict[str, Any]], version_resolucion: str = "anclas-v1",
    procedencia: dict[str, Any] | None = None,
) -> ResumenProyeccion:
    """Proyecta una lista de filas (de un dataset) a entidades canónicas resueltas
    por ancla fuerte. Idempotente: re-ejecutar no duplica (misma CURP = misma fila)."""
    from .config_entidad import leer_atributos

    r = ResumenProyeccion()
    declarados = tuple(leer_atributos(config))  # atributos EXTRA a capturar
    with psycopg.connect(config.postgres_dsn) as conn:
        for fila in filas:
            r.filas += 1
            try:
                ent = construir_entidad(receta, asignacion, fila, declarados)
            except Exception as exc:  # fila envenenada → dead-letter, la corrida sigue
                r.errores += 1
                log.warning("fila_envenenada", error=str(exc)[:200])
                continue
            if ent is None:
                r.sin_ancla += 1
                continue
            eid = calcular_entidad_id(receta.tipo, ent["ancla_tipo"], ent["ancla_valor"])
            try:
                resultado = _upsert(
                    conn, eid, receta.tipo, ent["ancla_tipo"], ent["ancla_valor"],
                    ent["campos"], receta.version, version_resolucion, procedencia,
                )
                conn.commit()
            except Exception as exc:  # error de BD en ESTA fila → dead-letter, sigue
                conn.rollback()
                r.errores += 1
                log.warning("upsert_fallido", entidad_id=eid, error=str(exc)[:200])
                continue
            if resultado == "nueva":
                r.entidades_nuevas += 1
            else:
                r.entidades_fusionadas += 1
    log.info("proyeccion_completa", **r.como_dict())
    return r
