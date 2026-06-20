"""Worker de ENVÍO: empuja las entidades resueltas (tabla `entidades`) al backend central
**AEB** por su canal de ingesta `POST /v1/ingest/entidades`, según la config de `destino.py`.

- **Formato del cable: CANÓNICO** (campos Fz1 + identificadores extraídos), NO `fz1_bundle`.
  El AEB recibe el canónico y cada consumidor proyecta a SU forma; mandar la proyección de un
  consumidor acoplaría el AEB (por eso la `receta` de la config de destino NO se aplica aquí).
- **Reanudable e incremental**: cursor keyset por `(actualizado_en, entidad_id)` en `control`.
  Como el upsert de la Fase 2 bumpea `actualizado_en` al fusionar, el cursor capta entidades
  NUEVAS y MODIFICADAS. `reiniciar=True` reenvía todo desde cero (seguro: el AEB es idempotente).
- **Serializado** con advisory try-lock (dos envíos a la par no se pisan).
- HTTP por `urllib` (stdlib): cero dependencias nuevas; `Content-Length` lo pone urllib.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger
from normalizacion.entidades.destino import leer_destino

log = obtener_logger("entidades.envio")

_CLAVE_CURSOR = "envio_aeb_cursor"
_CLAVE_ULTIMO = "envio_aeb_ultimo"  # resumen del último intento (para vigilar el automático)
_LOCK_ID = 0x4145_4256  # "AEBV" — advisory lock del envío
_RUTA_INGESTA = "/v1/ingest/entidades"
# Mapa identificador Azazel (en `campos`) → clave LookupKey del AEB.
_IDS = (("curp", "curp"), ("rfc", "rfc"), ("email", "email"), ("telefono", "phone"))


@dataclass
class ResumenEnvio:
    lotes: int = 0
    entidades: int = 0
    creadas: int = 0
    actualizadas: int = 0
    sin_cambio: int = 0
    fallidas: int = 0
    cursor: str | None = None
    detuvo_en: str | None = None  # motivo si paró antes de drenar (red/HTTP/deshabilitado)
    errores: list[str] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return asdict(self)


def _item_aeb(row: tuple[Any, ...]) -> dict[str, Any]:
    """Mapea una fila de `entidades` al ItemIngesta canónico del AEB (claves EXACTAS)."""
    eid, tipo, ancla_tipo, ancla_valor, campos, confianza, vr, vres, _act = row
    campos = campos or {}
    ids: dict[str, str] = {}
    for az, aeb in _IDS:
        v = campos.get(az)
        if isinstance(v, str) and v.strip():
            ids[aeb] = v.strip()
    cp = (campos.get("direccion") or {}).get("codigo_postal")
    if isinstance(cp, str) and cp.strip():
        ids["cp"] = cp.strip()
    placa = (campos.get("atributos") or {}).get("placa")
    if isinstance(placa, str) and placa.strip():
        ids["placas"] = placa.strip()
    return {
        "external_id": eid,
        "kind": "person" if tipo == "persona" else "unknown",
        "confianza": float(confianza) if confianza is not None else 1.0,
        "version": (f"{vr or ''}/{vres or ''}")[:120],
        "ancla": {"tipo": ancla_tipo, "valor": ancla_valor},
        "campos": campos,
        "identificadores": ids,
        "relaciones": [],  # Fase 2 no tiene grafo (E4/E5 pendientes)
        "evidencias": [],
    }


def _post_json(
    url: str, headers: dict[str, str], cuerpo: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    # La URL viene de la config del operador (panel), no de entrada de usuario.
    datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=datos, method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:  # el orquestador respondió, pero con error (4xx/5xx)
        crudo = exc.read()
        try:
            detalle = json.loads(crudo)
        except Exception:
            detalle = {"detail": crudo.decode("utf-8", "replace")[:300]}
        return exc.code, detalle
    except urllib.error.URLError as exc:  # NO se pudo conectar (red/DNS/TLS/timeout)
        return 0, {"detail": f"no se pudo conectar: {exc.reason}"}
    except Exception as exc:  # cualquier otro fallo del envío (no tumbar el worker)
        return 0, {"detail": f"error de envío: {type(exc).__name__}: {str(exc)[:200]}"}


def _motivo(status: int, resp: dict[str, Any]) -> str:
    """Traduce el resultado del POST a un mensaje claro para la UI (por qué no lo recibió bien)."""
    detalle = str(resp.get("detail") or resp.get("title") or resp)[:300]
    if status == 0:
        return f"No se pudo conectar al orquestador ({detalle})."
    if status in (401, 403):
        return "El orquestador rechazó la clave de ingesta (401/403): revisa el Token."
    if status == 411:
        return "Falta Content-Length (411): problema de proxy/red."
    if status == 413:
        return "Lote o payload demasiado grande (413): baja el valor de Lote."
    if status == 422:
        return f"El orquestador rechazó el lote (422): {detalle}"
    if status == 429:
        return "Límite de tasa del orquestador (429): reintenta en un momento."
    if status == 503:
        return "El orquestador no está listo (503): sin claves cargadas o Postgres caído."
    return f"HTTP {status}: {detalle}"


def _leer_cursor(conn: psycopg.Connection[Any]) -> tuple[str, str] | None:
    f = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CLAVE_CURSOR,)).fetchone()
    if not f:
        return None
    d = json.loads(f[0])
    return (d["ts"], d["id"])


def _guardar_cursor(conn: psycopg.Connection[Any], ts: str, eid: str) -> None:
    conn.execute(
        "INSERT INTO control (clave, valor) VALUES (%s, %s)"
        " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
        (_CLAVE_CURSOR, json.dumps({"ts": ts, "id": eid})),
    )


def _leer_lote(
    conn: psycopg.Connection[Any], cursor: tuple[str, str] | None, lote: int
) -> list[tuple[Any, ...]]:
    cols = ("entidad_id, tipo, ancla_tipo, ancla_valor, campos, confianza,"
            " version_receta, version_resolucion, actualizado_en")
    if cursor is None:
        sql = (f"SELECT {cols} FROM entidades WHERE activo = true"
               " ORDER BY actualizado_en, entidad_id LIMIT %s")
        return list(conn.execute(sql, (lote,)).fetchall())
    sql = (f"SELECT {cols} FROM entidades WHERE activo = true"
           " AND (actualizado_en, entidad_id) > (%s::timestamptz, %s)"
           " ORDER BY actualizado_en, entidad_id LIMIT %s")
    return list(conn.execute(sql, (cursor[0], cursor[1], lote)).fetchall())


def enviar_a_destino(
    config: Config, *, max_lotes: int | None = None, reiniciar: bool = False,
) -> ResumenEnvio:
    """Empuja entidades al AEB en lotes hasta drenar (o `max_lotes`). Reanudable por cursor."""
    destino = leer_destino(config)
    r = ResumenEnvio()
    if not destino.get("habilitado"):
        r.detuvo_en = "destino deshabilitado"
        return r
    url = str(destino.get("url") or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        r.detuvo_en = "URL de destino inválida"
        return r
    endpoint = url + _RUTA_INGESTA
    if not str(destino.get("auth_token") or "").strip():
        r.detuvo_en = "falta la clave de ingesta (Token) en la pestaña Destino"
        return r
    # El orquestador siempre espera la clave en el header X-API-Key.
    headers = {"X-API-Key": str(destino.get("auth_token") or "")}
    lote = max(1, min(int(destino.get("lote") or 500), 5000))

    try:
        with psycopg.connect(config.postgres_dsn) as conn:
            got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,)).fetchone()
            if not got or not got[0]:
                r.detuvo_en = "otro envío en curso"
                return r
            try:
                if reiniciar:
                    conn.execute("DELETE FROM control WHERE clave = %s", (_CLAVE_CURSOR,))
                    conn.commit()
                cursor = _leer_cursor(conn)
                while max_lotes is None or r.lotes < max_lotes:
                    filas = _leer_lote(conn, cursor, lote)
                    if not filas:
                        break
                    cuerpo = {
                        "version_cable": "1", "productor": "azazel",
                        "fuente": "azazel_resolucion",
                        # Azazel es el resolvedor autoritativo: el AEB toma su valor como verdad
                        # (last-write-wins) para que los cambios re-resueltos SÍ se propaguen.
                        "modo_merge": "reemplazar",
                        "entidades": [_item_aeb(f) for f in filas],
                    }
                    status, resp = _post_json(endpoint, headers, cuerpo)
                    if status not in (200, 207):
                        r.detuvo_en = _motivo(status, resp)
                        r.errores.append(r.detuvo_en)
                        log.warning("envio_rechazado", status=status, motivo=r.detuvo_en)
                        break
                    r.lotes += 1
                    r.entidades += int(resp.get("recibidas", len(filas)))
                    r.creadas += int(resp.get("creadas", 0))
                    r.actualizadas += int(resp.get("actualizadas", 0))
                    r.sin_cambio += int(resp.get("sin_cambio", 0))
                    fallidas_lote = int(resp.get("fallidas", 0))
                    r.fallidas += fallidas_lote
                    if fallidas_lote:
                        _anotar_fallos(r, resp, fallidas_lote)
                    ult = filas[-1]
                    ts_iso, eid = ult[8].isoformat(), str(ult[0])
                    cursor = (ts_iso, eid)
                    _guardar_cursor(conn, ts_iso, eid)
                    conn.commit()
                    r.cursor = f"{ts_iso}|{eid}"
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    except psycopg.Error as exc:  # falla la BD de Azazel (no el orquestador)
        r.detuvo_en = f"error de la base de datos de Azazel ({type(exc).__name__})"
        r.errores.append(r.detuvo_en)
        log.warning("envio_db_error", error=str(exc)[:200])
        _guardar_ultimo(config, r)
        return r
    log.info("envio_aeb_completo", entidades=r.entidades, creadas=r.creadas, fallidas=r.fallidas)
    _guardar_ultimo(config, r)
    return r


def _guardar_ultimo(config: Config, r: ResumenEnvio) -> None:
    """Persiste el resumen del último intento (para mostrar 'último intento: hace X — OK/Error').
    Best-effort: si la BD falla no debe romper el envío."""
    valor = {
        "ts": datetime.now(UTC).isoformat(),
        "ok": not r.detuvo_en,
        "detuvo_en": r.detuvo_en,
        "entidades": r.entidades, "creadas": r.creadas,
        "actualizadas": r.actualizadas, "fallidas": r.fallidas,
        "errores": r.errores[:5],
    }
    try:
        with psycopg.connect(config.postgres_dsn) as conn:
            conn.execute(
                "INSERT INTO control (clave, valor) VALUES (%s, %s)"
                " ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado_en = now()",
                (_CLAVE_ULTIMO, json.dumps(valor, ensure_ascii=False)),
            )
            conn.commit()
    except Exception:
        pass


def _anotar_fallos(r: ResumenEnvio, resp: dict[str, Any], fallidas: int) -> None:
    """Anota los códigos de error por-ítem que devolvió el orquestador (sin PII)."""
    codigos = sorted({
        str(x.get("codigo")) for x in resp.get("resultados", [])
        if x.get("estado") == "error" and x.get("codigo")
    })
    nota = f"{fallidas} entidad(es) rechazada(s) por el orquestador"
    if codigos:
        nota += f": {', '.join(codigos)}"
    if nota not in r.errores:
        r.errores.append(nota)
    log.warning("envio_items_fallidos", fallidas=fallidas, codigos=codigos)


# --------------------------------------------------- envío AUTOMÁTICO (daemon)
_HILO: threading.Thread | None = None
_PARAR = threading.Event()


def _pasada(config: Config) -> int:
    """Una iteración del bucle: si el destino está habilitado con intervalo>0, envía lo pendiente.
    Devuelve los segundos a esperar hasta la próxima vuelta (15 si el automático está inactivo)."""
    destino = leer_destino(config)
    intervalo = int(destino.get("intervalo_seg") or 0)
    if destino.get("habilitado") and intervalo > 0:
        r = enviar_a_destino(config)
        if r.detuvo_en and r.detuvo_en != "otro envío en curso":
            log.warning("envio_auto_detenido", motivo=r.detuvo_en, errores=r.errores[:3])
        elif r.entidades or r.fallidas:
            log.info("envio_auto", entidades=r.entidades, creadas=r.creadas,
                     fallidas=r.fallidas, errores=r.errores[:3])
        return intervalo
    return 15  # inactivo: re-checa la config pronto (toma efecto sin reiniciar)


def _bucle(config: Config) -> None:
    while not _PARAR.is_set():
        try:
            espera = _pasada(config)
        except Exception as exc:  # nunca tumbar el hilo: registra y reintenta
            log.warning("bucle_envio_error", error=str(exc)[:200])
            espera = 30
        _PARAR.wait(max(espera, 5))


def iniciar_bucle(config: Config) -> None:
    """Arranca (una sola vez) el hilo de envío automático. Daemon: muere con el proceso.
    El intervalo y el on/off se leen de la config en cada vuelta, así que cambiarlos en la UI
    toma efecto sin reiniciar."""
    global _HILO
    if _HILO is not None and _HILO.is_alive():
        return
    _PARAR.clear()
    _HILO = threading.Thread(target=_bucle, args=(config,), daemon=True, name="envio-aeb")
    _HILO.start()
    log.info("bucle_envio_iniciado")


def estado_envio(config: Config) -> dict[str, Any]:
    """Estado del envío para la UI: si está habilitado, el cursor y cuántas faltan por enviar."""
    destino = leer_destino(config)
    with psycopg.connect(config.postgres_dsn) as conn:
        cursor = _leer_cursor(conn)
        if cursor is None:
            pendientes = conn.execute(
                "SELECT count(*) FROM entidades WHERE activo = true"
            ).fetchone()
        else:
            pendientes = conn.execute(
                "SELECT count(*) FROM entidades WHERE activo = true"
                " AND (actualizado_en, entidad_id) > (%s::timestamptz, %s)",
                (cursor[0], cursor[1]),
            ).fetchone()
        fu = conn.execute("SELECT valor FROM control WHERE clave = %s", (_CLAVE_ULTIMO,)).fetchone()
    ultimo = json.loads(fu[0]) if fu else None
    return {
        "habilitado": bool(destino.get("habilitado")),
        "url": destino.get("url"),
        "cursor": f"{cursor[0]}|{cursor[1]}" if cursor else None,
        "pendientes": int(pendientes[0]) if pendientes else 0,
        "intervalo_seg": int(destino.get("intervalo_seg") or 0),
        "ultimo": ultimo,
    }
