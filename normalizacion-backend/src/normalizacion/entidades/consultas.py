"""Lecturas sobre la tabla `entidades` para la API y la UI."""

from __future__ import annotations

from typing import Any

import psycopg

from normalizacion.core.config import Config
from normalizacion.entidades import derivados

_COLS = ("entidad_id, tipo, ancla_tipo, ancla_valor, campos, confianza,"
         " version_receta, version_resolucion, activo, procedencias,"
         " creado_en, actualizado_en")


def _a_dict(f: tuple[Any, ...]) -> dict[str, Any]:
    # `enriquecer` reconstruye lo que no se guarda (normalizados, edad). La base
    # almacena la forma reducida; TODO lector debe pasar por aqui o vera una ficha
    # incompleta — y la proyeccion al AEB lee `normalizados.normalized_dob` por ruta.
    return {
        "entidad_id": f[0], "tipo": f[1], "ancla_tipo": f[2], "ancla_valor": f[3],
        "campos": derivados.enriquecer(dict(f[4] or {})), "confianza": f[5], "version_receta": f[6],
        "version_resolucion": f[7], "activo": f[8], "procedencias": f[9],
        "creado_en": f[10], "actualizado_en": f[11],
    }


def listar_entidades(
    config: Config, *, tipo: str | None = None, curp: str | None = None,
    nombre: str | None = None, cursor: str | None = None, limite: int = 50,
) -> dict[str, Any]:
    """Lista entidades ACTIVAS con filtros; keyset por entidad_id."""
    limite = max(1, min(limite, 200))
    cond = ["activo = true"]
    params: list[Any] = []
    if tipo:
        cond.append("tipo = %s"); params.append(tipo)
    if curp:
        cond.append("campos->>'curp' = %s"); params.append(curp.strip().upper())
    if nombre:
        # Escapa los comodines de LIKE (% _ \) para que se busque el literal.
        esc = nombre.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cond.append("campos->>'nombre_completo' ILIKE %s"); params.append(f"%{esc}%")
    where = " WHERE " + " AND ".join(cond)
    with psycopg.connect(config.postgres_dsn) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM entidades{where}", params).fetchone()[0])  # type: ignore[index]
        pag = f"{where} AND entidad_id > %s" if cursor else where
        filas = conn.execute(
            f"SELECT {_COLS} FROM entidades{pag} ORDER BY entidad_id LIMIT %s",
            [*params, *([cursor] if cursor else []), limite],
        ).fetchall()
    ents = [_a_dict(f) for f in filas]
    return {"total": total, "entidades": ents,
            "cursor": ents[-1]["entidad_id"] if len(ents) == limite else None}


def obtener_entidad(config: Config, entidad_id: str) -> dict[str, Any] | None:
    with psycopg.connect(config.postgres_dsn) as conn:
        f = conn.execute(
            f"SELECT {_COLS} FROM entidades WHERE entidad_id = %s AND activo = true",
            (entidad_id,),
        ).fetchone()
    return _a_dict(f) if f else None


def fijar_activo(config: Config, entidad_id: str, activo: bool) -> bool:
    """Contingencia LFPDPPP: desactiva (soft-delete) o reactiva una entidad. NUNCA
    borra la fila — la deja invisible para consultas y auditable."""
    with psycopg.connect(config.postgres_dsn) as conn:
        cur = conn.execute(
            "UPDATE entidades SET activo = %s, actualizado_en = now() WHERE entidad_id = %s",
            (activo, entidad_id),
        )
        conn.commit()
    return cur.rowcount == 1


def exportar(config: Config, definicion: dict[str, Any], *, limite: int = 10000) -> Any:
    """Carga las personas ACTIVAS y arma el ARCHIVO de salida con la receta dada.

    Con una receta de colección (p.ej. fz1_bundle) devuelve el archivo completo
    (sobre + arreglo de personas); con una receta por-ítem, el arreglo plano."""
    from .proyeccion import exportar_coleccion

    limite = max(1, min(limite, 100_000))
    with psycopg.connect(config.postgres_dsn) as conn:
        filas = conn.execute(
            "SELECT campos FROM entidades WHERE activo = true ORDER BY entidad_id LIMIT %s",
            (limite,),
        ).fetchall()
    return exportar_coleccion([f[0] for f in filas], definicion)


def estadisticas(config: Config) -> dict[str, Any]:
    with psycopg.connect(config.postgres_dsn) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM entidades WHERE activo = true").fetchone()[0])  # type: ignore[index]
        con_curp = int(conn.execute(
            "SELECT COUNT(*) FROM entidades WHERE activo = true AND campos->>'curp' IS NOT NULL"
        ).fetchone()[0])  # type: ignore[index]
        por_ancla = {
            f[0]: int(f[1]) for f in conn.execute(
                "SELECT ancla_tipo, COUNT(*) FROM entidades WHERE activo = true"
                " GROUP BY ancla_tipo ORDER BY 2 DESC"
            ).fetchall()
        }
    return {"total": total, "con_curp": con_curp, "por_ancla": por_ancla}
