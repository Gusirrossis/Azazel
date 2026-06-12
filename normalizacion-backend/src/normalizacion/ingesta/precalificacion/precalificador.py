"""Runner de la precalificación: consume PENDIENTE, puntúa, enruta y EXPLORA contenedores.

Patrón: claim por lotes (SKIP LOCKED + lease) → reglas puras → guardar decisión auditable.
T3: cada entrada interna de un contenedor re-entra a la cola como fila propia con su
path spec (`origen_contenedor`) — el recorrido de cajas-dentro-de-cajas es BFS vía la
cola, nunca recursión. Un guard violado produce COLD con motivo; jamás un crash.
Reanudable: matar el proceso a media corrida no pierde nada (lease vence → otro retoma).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import psycopg

from normalizacion.core import cola
from normalizacion.core.config import Config, PerillasFiltro, PerillasWorker
from normalizacion.core.modelo import Estado, RutaDecision, calcular_archivo_id
from normalizacion.core.observabilidad import obtener_logger

from . import contenedores, reglas
from .reglas import ResultadoPrecalificacion

log = obtener_logger("precalificacion")


@dataclass(frozen=True)
class ResumenPrecalificacion:
    procesados: int
    hot: int
    cold: int
    errores: int
    re_encolados: int
    transitorios: int = 0  # devueltos con backoff (I/O intermitente)


def _filas_de_entradas(
    fila: cola.FilaReclamada,
    exploracion: contenedores.ResultadoExploracion,
    profundidad: int,
    perillas: PerillasFiltro,
) -> list[cola.FilaCatalogo]:
    """Convierte las entradas listadas en filas nuevas con su path spec serializable.

    Las entradas HEREDAN la urgencia del contenedor (prioridad alta): lo que viene
    dentro de comprimidos se precalifica antes que el resto de la cola."""
    from datetime import UTC, datetime

    origen = fila.origen_contenedor
    cadena_base = list(origen["cadena"]) if origen else [fila.ruta]
    nuevas: list[cola.FilaCatalogo] = []
    for entrada in exploracion.entradas:
        ruta_virtual = f"{fila.ruta}!{entrada.ruta_interna}"
        sufijo = Path(entrada.nombre).suffix.lower()
        nuevas.append(
            cola.FilaCatalogo(
                archivo_id=calcular_archivo_id(
                    f"{fila.disco_id}:{ruta_virtual}", entrada.tamano, entrada.mtime_ns
                ),
                disco_id=fila.disco_id,
                ruta=ruta_virtual,
                nombre=entrada.nombre,
                extension=sufijo if sufijo else None,
                tamano=entrada.tamano,
                mtime=datetime.fromtimestamp(entrada.mtime_ns / 1_000_000_000, tz=UTC),
                # Hereda la urgencia del contenedor, salvo que la extensión tenga un
                # orden propio mayor (un .txt interno también va primero).
                prioridad=max(
                    perillas.prioridad_inicial_contenedores,
                    perillas.prioridad_extensiones.get(sufijo, 0),
                ),
                origen_contenedor={
                    "cadena": [*cadena_base, entrada.ruta_interna],
                    "profundidad": profundidad + 1,
                    "contenedor_archivo_id": fila.archivo_id,
                },
            )
        )
    return nuevas


def _procesar_fila(
    perillas: PerillasFiltro,
    worker: PerillasWorker,
    raiz: str,
    fila: cola.FilaReclamada,
) -> tuple[ResultadoPrecalificacion, list[cola.FilaCatalogo]]:
    """Precalifica una fila (archivo del disco o entrada interna) y, si es contenedor,
    aplica T3. Devuelve (decisión, filas nuevas a re-encolar)."""
    origen = fila.origen_contenedor
    profundidad = int(origen["profundidad"]) if origen else 0

    fuente: IO[bytes]
    fuente_fs: Path | None = None
    if origen:
        fuente = contenedores.abrir_entrada(
            Path(raiz),
            list(origen["cadena"]),
            umbral_memoria=worker.umbral_memoria_bytes,
            limite_bytes=perillas.t3_descomprimido_max_bytes,
        )
    else:
        ruta_abs = Path(raiz) / fila.ruta
        if not ruta_abs.is_file():
            raise OSError(f"no existe bajo el punto de montaje: {fila.ruta}")
        fuente_fs = ruta_abs
        fuente = ruta_abs.open("rb")

    with fuente:
        head = fuente.read(perillas.head_t2_bytes)
        resultado = reglas.precalificar_contenido(
            perillas,
            head=head,
            abrible=fuente,
            nombre=fila.nombre,
            extension=fila.extension,
            ruta_relativa=fila.ruta,
            tamano=fila.tamano,
        )
        if not resultado.senales.get("es_contenedor"):
            return resultado, []

        # ---- T3: contenedor detectado ----
        senales: dict[str, Any] = {**resultado.senales, "profundidad": profundidad}
        if profundidad >= perillas.t3_profundidad_max:
            # Tope de anidación (⚙K4): no se explora más hondo. Reversible: el frío
            # se puede re-puntuar con un filtro vN de tope mayor.
            return (
                ResultadoPrecalificacion(
                    10, RutaDecision.COLD, resultado.tipo_real, "profundidad_maxima", senales
                ),
                [],
            )

        exploracion = contenedores.explorar(
            perillas, fuente_fs or fuente, resultado.tipo_real or ""
        )
        senales["formato"] = exploracion.formato
        if not exploracion.ok:
            if exploracion.motivo and exploracion.motivo.startswith("guard_"):
                # Zip-bomb sospechoso: flag + COLD. NUNCA cuelga al worker (riesgo F3/R8).
                senales["guard_violado"] = exploracion.motivo
                return (
                    ResultadoPrecalificacion(
                        5,
                        RutaDecision.COLD,
                        resultado.tipo_real,
                        f"zip_bomb_sospechoso:{exploracion.motivo}",
                        senales,
                    ),
                    [],
                )
            # corrupto / formato aún no soportado → se PRESERVA íntegro (HOT, sin explorar)
            return (
                ResultadoPrecalificacion(
                    perillas.prioridad_contenedores,
                    RutaDecision.HOT,
                    resultado.tipo_real,
                    exploracion.motivo or "contenedor_sin_explorar",
                    senales,
                ),
                [],
            )

        nuevas = _filas_de_entradas(fila, exploracion, profundidad, perillas)
        senales["entradas_re_encoladas"] = len(nuevas)
        return (
            ResultadoPrecalificacion(
                perillas.prioridad_contenedores,
                RutaDecision.HOT,
                resultado.tipo_real,
                "contenedor_explorado",
                senales,
            ),
            nuevas,
        )


def precalificar_pendientes(
    config: Config, worker_id: str = "precalifica-1"
) -> ResumenPrecalificacion:
    """Drena TODOS los PENDIENTE — incluidas las entradas que T3 va re-encolando (BFS)."""
    procesados = hot = cold = errores = re_encolados = transitorios = 0
    perillas = config.filtro
    w = config.worker

    with psycopg.connect(config.postgres_dsn) as conn:
        montajes = cola.montajes(conn)
        while True:
            if cola.sistema_pausado(conn):  # norm pausar: drena el lote y se detiene
                log.warning("sistema_pausado", etapa="precalificacion")
                break
            filas = cola.claim(
                conn,
                worker_id=worker_id,
                estado=Estado.PENDIENTE,
                lote=config.worker.lote_claim,
                lease_segundos=config.worker.lease_segundos,
            )
            conn.commit()  # el lease queda visible para otros workers
            if not filas:
                break

            for fila in filas:
                raiz = montajes.get(fila.disco_id)
                try:
                    if raiz is None:
                        raise OSError(f"disco sin punto de montaje: {fila.disco_id}")
                    resultado, nuevas = _procesar_fila(perillas, config.worker, raiz, fila)
                except contenedores.ContenedorInseguro as exc:  # PERMANENTE
                    cola.marcar_error(conn, fila.archivo_id, Estado.PENDIENTE, f"contenedor: {exc}")
                    errores += 1
                    continue
                except OSError as exc:  # TRANSITORIO con tope (disco intermitente)
                    en_reintento = cola.fallo_transitorio(
                        conn,
                        fila.archivo_id,
                        estado_actual=Estado.PENDIENTE,
                        estado_retorno=None,  # sigue PENDIENTE, con backoff
                        motivo=f"io_ilegible: {exc}",
                        intentos_actuales=fila.intentos,
                        intentos_max=w.intentos_max,
                        backoff_s=w.backoff_transitorio_base_s * (2**fila.intentos),
                    )
                    if en_reintento:
                        transitorios += 1
                        log.warning("fallo_transitorio", archivo=fila.ruta, error=str(exc))
                    else:
                        errores += 1
                    continue
                except Exception as exc:  # ARCHIVO ENVENENADO (decode raro, lib de 7z/rar,
                    # bytes hostiles): a dead-letter y la corrida SIGUE. Jamás un solo
                    # archivo tumba el proceso automático. Reprocesable con reprocesar-errores.
                    # _procesar_fila no toca la BD → la transacción del lote sigue sana.
                    cola.marcar_error(
                        conn,
                        fila.archivo_id,
                        Estado.PENDIENTE,
                        f"precalificacion_fallida:{type(exc).__name__}: {exc}"[:300],
                    )
                    errores += 1
                    log.warning(
                        "archivo_envenenado",
                        etapa="precalificacion",
                        archivo=fila.ruta,
                        error=str(exc)[:200],
                    )
                    continue

                if nuevas:
                    re_encolados += cola.insertar_pendientes(conn, nuevas)
                cola.guardar_precalificacion(
                    conn,
                    fila.archivo_id,
                    puntaje=resultado.puntaje,
                    ruta=resultado.ruta,
                    tipo_real=resultado.tipo_real,
                    senales=resultado.senales,
                    motivo=resultado.motivo,
                    version_filtro=perillas.version_filtro,
                    # El orden por extensión (>100) sobrevive a la transición; para
                    # el resto, prioridad = puntaje (comportamiento de siempre).
                    prioridad=max(
                        resultado.puntaje,
                        perillas.prioridad_extensiones.get(
                            (fila.extension or "").lower(), 0
                        ),
                    ),
                )
                procesados += 1
                if resultado.ruta is RutaDecision.HOT:
                    hot += 1
                else:
                    cold += 1
            conn.commit()  # lote durable: matar aquí no pierde ni repite trabajo
            log.info(
                "avance_precalificacion",
                procesados=procesados,
                hot=hot,
                cold=cold,
                errores=errores,
            )

    log.info(
        "precalificacion_completa",
        procesados=procesados,
        hot=hot,
        cold=cold,
        errores=errores,
        re_encolados=re_encolados,
        transitorios=transitorios,
        version=perillas.version_filtro,
    )
    return ResumenPrecalificacion(procesados, hot, cold, errores, re_encolados, transitorios)


@dataclass(frozen=True)
class PerfilDisco:
    """Caracterización del disco ANTES de gastar el pipeline (lección brunnhilde)."""

    disco_id: str
    total: int
    por_ruta: list[tuple[str, int, int]]  # (HOT/COLD/PENDIENTE…, archivos, bytes)
    top_tipos: list[tuple[str, int]]
    por_motivo: list[tuple[str, int]]


def perfil_disco(config: Config, disco_id: str) -> PerfilDisco:
    """Agregación SQL sobre la cola — costo ~0, no toca los archivos."""
    with psycopg.connect(config.postgres_dsn) as conn:
        total_fila = conn.execute(
            "SELECT COUNT(*) FROM archivos WHERE disco_id = %s", (disco_id,)
        ).fetchone()
        por_ruta = conn.execute(
            "SELECT COALESCE(ruta_decision, estado), COUNT(*), COALESCE(SUM(tamano), 0)"
            " FROM archivos WHERE disco_id = %s GROUP BY 1 ORDER BY 2 DESC",
            (disco_id,),
        ).fetchall()
        tipos = conn.execute(
            "SELECT tipo_real, COUNT(*) FROM archivos"
            " WHERE disco_id = %s AND tipo_real IS NOT NULL"
            " GROUP BY tipo_real ORDER BY COUNT(*) DESC LIMIT 10",
            (disco_id,),
        ).fetchall()
        motivos = conn.execute(
            "SELECT motivo, COUNT(*) FROM archivos"
            " WHERE disco_id = %s AND motivo IS NOT NULL"
            " GROUP BY motivo ORDER BY COUNT(*) DESC",
            (disco_id,),
        ).fetchall()
    return PerfilDisco(
        disco_id=disco_id,
        total=int(total_fila[0]) if total_fila else 0,
        por_ruta=[(f[0], int(f[1]), int(f[2])) for f in por_ruta],
        top_tipos=[(f[0], int(f[1])) for f in tipos],
        por_motivo=[(f[0], int(f[1])) for f in motivos],
    )
