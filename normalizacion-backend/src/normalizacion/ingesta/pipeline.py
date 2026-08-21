"""Orquestador del pipeline completo: una carpeta entra → todo el ciclo corre.

catálogo → precalificación (T0-T4) → worker (blobs + índice) → mover frío →
verificación → puerta. Cada fase queda registrada en `corridas` con duración y
métricas (la estadística operativa: cuántos, a qué velocidad, cuántos errores).

CARPETA VIVA: se puede re-ejecutar sobre la misma carpeta cuantas veces se quiera
— el catálogo es idempotente (re-scan incremental): solo lo nuevo/cambiado
genera trabajo. Es el modo "continuo + ráfagas" del diseño.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from normalizacion.core.config import Config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("pipeline")

FASES = ("catalogo", "precalificacion", "worker", "mover_frio", "verificacion", "puerta")


def destinos(config: Config) -> dict[str, str]:
    """DÓNDE queda todo lo procesado (visible en el front y en `norm pipeline`)."""
    if config.almacen_backend == "local":
        almacen = str(Path(config.almacen_local_raiz).expanduser().resolve())
        frio = str(Path(config.almacen_frio_local_raiz).expanduser().resolve())
    else:
        almacen = f"minio://{config.minio_endpoint}/{config.minio_bucket}"
        frio = f"minio://{config.minio_endpoint}/{config.minio_bucket_frio}"
    return {
        "originales_hot": almacen,
        "frio_reversible": frio,
        "indice_metadatos": f"{config.opensearch_url} (alias '{config.indice_alias}')",
        "cola_estado": config.postgres_dsn.rsplit("@", 1)[-1],
    }


def destinos_por_disco(config: Config) -> dict[str, Any]:
    """Raíz REAL del almacén (carpeta del sistema) por disco, para que la UI muestre
    dónde quedó cada archivo aunque cada corrida haya elegido una carpeta destino
    distinta. Resuelve el destino de la última corrida de cada disco:
      · con destino elegido → {destino}/almacen y {destino}/frio (carpeta local real)
      · sin destino         → el almacén configurado en el .env (global)

    `global` sirve de respaldo para discos sin corrida registrada."""
    base = destinos(config)
    glob = {"hot": base["originales_hot"], "frio": base["frio_reversible"]}
    with psycopg.connect(config.postgres_dsn) as conn:
        filas = conn.execute(
            "SELECT DISTINCT ON (disco_id) disco_id, destino FROM corridas"
            " ORDER BY disco_id, id DESC"
        ).fetchall()
    por_disco: dict[str, dict[str, str]] = {}
    for disco_id, destino in filas:
        if destino:
            b = Path(destino).expanduser().resolve()
            por_disco[disco_id] = {"hot": str(b / "almacen"), "frio": str(b / "frio")}
        else:
            por_disco[disco_id] = dict(glob)
    return {"global": glob, "por_disco": por_disco}


def config_con_destino(config: Config, destino: str | None) -> Config:
    """Config EFECTIVA de una corrida: con `destino` (carpeta elegida en el front),
    el almacén HOT y el frío viven bajo esa carpeta (backend local); sin él, se usa
    lo configurado (.env: MinIO o carpetas por defecto). El índice de metadatos
    (OpenSearch) y la cola (Postgres) no cambian de lugar."""
    if destino is None:
        return config
    base = Path(destino).expanduser().resolve()
    (base / "almacen").mkdir(parents=True, exist_ok=True)
    (base / "frio").mkdir(parents=True, exist_ok=True)
    return config.model_copy(
        update={
            "almacen_backend": "local",
            "almacen_local_raiz": str(base / "almacen"),
            "almacen_frio_local_raiz": str(base / "frio"),
        }
    )


def workers_auto() -> int:
    """Default automático: núcleos - 2 (deja aire para el filtro, la API y las bases)."""
    import os

    return max(1, (os.cpu_count() or 4) - 2)


def resolver_workers(config: Config, solicitado: int | None) -> int:
    """Cuántos procesos worker correr. El gobernador (K15) decide:

    · modo "adaptativo" (default): dimensiona por la RAM LIBRE en tiempo real —lo
      pedido en el front es un TECHO, no una orden— para no saturar la Mac.
    · modo "fijo": lo pedido (front) > perilla NORM_WORKER__PROCESOS > núcleos−2.
    """
    from normalizacion.core import recursos

    return recursos.presupuesto_workers(config, solicitado)


def _worker_en_proceso(
    config: Config, usar_indice: bool, filtro_terminado: Any, resultados: Any, indice: int
) -> None:
    """Target de cada PROCESO worker. Procesos reales (no hilos): la extracción es
    Python puro y el GIL mataría el paralelismo. Cada proceso abre sus propias
    conexiones; la cola reparte sin duplicar (SKIP LOCKED)."""
    from normalizacion.core.indexador import Sink, SinkNulo
    from normalizacion.ingesta.workers.orquestador import procesar_hot

    sink: Sink
    if usar_indice:
        from normalizacion.core.indexador.opensearch import SinkOpenSearch

        sink = SinkOpenSearch(config)
    else:
        sink = SinkNulo()
    resumen = procesar_hot(
        config,
        worker_id=f"pipeline-w{indice}",
        sink=sink,
        seguir_esperando=lambda: not filtro_terminado.is_set(),
    )
    resultados.put(asdict(resumen))


def _correr_workers_en_paralelo(
    config: Config, usar_indice: bool, n: int, hilo_pre: Any
) -> dict[str, Any]:
    """Lanza N procesos worker, avisa cuando el filtro termina (Event) y AGREGA
    los resúmenes. Un proceso muerto = corrida FALLIDA (jamás silencio)."""
    import multiprocessing as mp
    import queue as queue_mod

    ctx = mp.get_context("spawn")
    filtro_terminado = ctx.Event()
    resultados = ctx.Queue()
    procesos = [
        ctx.Process(
            target=_worker_en_proceso,
            args=(config, usar_indice, filtro_terminado, resultados, i + 1),
            daemon=True,
        )
        for i in range(n)
    ]
    for p in procesos:
        p.start()
    hilo_pre.join()  # el filtro corre en paralelo en el padre (como siempre)
    filtro_terminado.set()  # workers: drenar el barrido final y terminar

    agregado: dict[str, int] = {}
    pendientes = n
    while pendientes:
        try:
            parcial = resultados.get(timeout=2)
        except queue_mod.Empty:
            muertos = [p for p in procesos if p.exitcode not in (None, 0)]
            if muertos:
                raise RuntimeError(
                    f"{len(muertos)} proceso(s) worker murieron (exitcode != 0)"
                ) from None
            if all(p.exitcode == 0 for p in procesos):
                raise RuntimeError("los workers terminaron sin reportar resultados") from None
            continue
        pendientes -= 1
        for clave, valor in parcial.items():
            agregado[clave] = agregado.get(clave, 0) + int(valor)
    for p in procesos:
        p.join()
    return agregado


def _tasa(metricas: dict[str, Any], duracion_s: float) -> float | None:
    base = (
        metricas.get("procesados")
        or metricas.get("archivos_vistos")
        or metricas.get("verificados")
        or metricas.get("movidos")
    )
    if isinstance(base, int) and duracion_s > 0:
        return round(base / duracion_s, 1)
    return None


def marcar_corridas_huerfanas(config: Config) -> int:
    """Al arrancar la API: corridas EN_CURSO de un proceso anterior (el servidor se
    reinició a media corrida) se marcan FALLIDA — si no, el lock queda atorado para
    siempre y la UI muestra 'en curso' eternamente. El trabajo NO se pierde: la cola
    conserva el avance y re-ejecutar retoma donde quedó (incremental)."""
    with psycopg.connect(config.postgres_dsn) as conn:
        cur = conn.execute(
            "UPDATE corridas SET estado = 'FALLIDA',"
            " error = 'interrumpida por reinicio del servidor (re-ejecutar retoma)',"
            " terminada_en = now() WHERE estado = 'EN_CURSO'"
        )
        conn.commit()
        if cur.rowcount:
            log.warning("corridas_huerfanas_marcadas", cuantas=cur.rowcount)
        return cur.rowcount


def iniciar_corrida(
    config: Config, ruta: Path, disco_id: str | None = None, destino: str | None = None
) -> tuple[int, str]:
    """Valida y registra la corrida. Una sola a la vez (lock por tabla)."""
    from normalizacion.core import cola, despliegue

    ruta = ruta.expanduser().resolve()
    if not ruta.is_dir():
        raise ValueError(f"no es una carpeta: {ruta}")
    if disco_id is None and despliegue.exige_disco_id_explicito(config):
        raise ValueError(
            "en modo híbrido el disco_id es obligatorio: derivarlo del nombre de la"
            f" carpeta ('{ruta.name}') provoca colisiones entre nodos"
        )
    id_pedido = disco_id or ruta.name
    with psycopg.connect(config.postgres_dsn) as conn:
        # Se resuelve AQUÍ y se propaga al catálogo, para que `corridas.disco_id` y
        # `archivos.disco_id` no puedan discrepar (la corrida se registra con el id
        # definitivo, no con el pedido).
        id_disco = despliegue.resolver_disco_id(
            config, id_pedido, ya_existe=lambda d: cola.disco_existe(conn, d)
        )
        en_curso = conn.execute("SELECT id FROM corridas WHERE estado = 'EN_CURSO'").fetchone()
        if en_curso:
            raise RuntimeError(f"ya hay una corrida en curso (id {en_curso[0]})")
        fila = conn.execute(
            "INSERT INTO corridas (disco_id, ruta, destino) VALUES (%s, %s, %s) RETURNING id",
            (id_disco, str(ruta), destino),
        ).fetchone()
        conn.commit()
    assert fila is not None
    return int(fila[0]), id_disco


def _actualizar(config: Config, corrida_id: int, **campos: Any) -> None:
    asignaciones = []
    valores: list[Any] = []
    for clave, valor in campos.items():
        asignaciones.append(f"{clave} = %s")
        valores.append(Jsonb(valor) if clave == "fases" else valor)
    with psycopg.connect(config.postgres_dsn) as conn:
        conn.execute(
            f"UPDATE corridas SET {', '.join(asignaciones)} WHERE id = %s",
            (*valores, corrida_id),
        )
        conn.commit()


def ejecutar_corrida(
    config: Config,
    corrida_id: int,
    ruta: Path,
    disco_id: str,
    usar_indice: bool = True,
    workers: int | None = None,
) -> list[dict[str, Any]]:
    """Corre las 6 fases registrando duración + métricas de cada una. Síncrona
    (la API la lanza en un hilo). `usar_indice=False` = SinkNulo (tests sin OS).
    `workers`: nº de PROCESOS worker en paralelo (None = perilla/auto)."""
    from normalizacion.core.indexador import Sink, SinkNulo
    from normalizacion.ingesta.catalogo.walker import catalogar_disco
    from normalizacion.ingesta.precalificacion.precalificador import precalificar_pendientes
    from normalizacion.ingesta.workers.orquestador import procesar_hot
    from normalizacion.ingesta.workers.verificador import (
        evaluar_puerta,
        mover_frio,
        verificar_indexados,
    )

    sink: Sink
    if usar_indice:
        from normalizacion.core.indexador.opensearch import SinkOpenSearch, aplicar_indice

        try:  # idempotente; si el template ya está, sigue
            aplicar_indice(config)
        except Exception as exc:
            log.warning("aplicar_indice_fallo", error=str(exc)[:200])
        sink = SinkOpenSearch(config)
    else:
        sink = SinkNulo()

    fases: list[dict[str, Any]] = []

    def correr(nombre: str, fn: Any) -> Any:
        _actualizar(config, corrida_id, fase_actual=nombre)
        inicio = time.monotonic()
        resumen = fn()
        duracion = round(time.monotonic() - inicio, 2)
        metricas: dict[str, Any] = asdict(resumen)
        entrada = {"fase": nombre, "duracion_s": duracion, "metricas": metricas}
        tasa = _tasa(metricas, duracion)
        if tasa is not None:
            entrada["archivos_por_segundo"] = tasa
        fases.append(entrada)
        _actualizar(config, corrida_id, fases=fases)
        log.info("fase_completa", corrida=corrida_id, fase=nombre, duracion_s=duracion)
        return resumen

    try:
        correr("catalogo", lambda: catalogar_disco(config, ruta, disco_id))

        # FILTRO ∥ WORKER en paralelo (como los procesos continuos de producción):
        # los primeros documentos quedan BUSCABLES a segundos de iniciar, en vez
        # de esperar a que el filtro termine el disco completo.
        import threading

        resultado_pre: dict[str, Any] = {}

        def _precalificar_en_paralelo() -> None:
            inicio_pre = time.monotonic()
            try:
                resumen_pre = precalificar_pendientes(config)
                entrada_pre: dict[str, Any] = {
                    "fase": "precalificacion",
                    "duracion_s": round(time.monotonic() - inicio_pre, 2),
                    "metricas": asdict(resumen_pre),
                }
                tasa_pre = _tasa(entrada_pre["metricas"], entrada_pre["duracion_s"])
                if tasa_pre is not None:
                    entrada_pre["archivos_por_segundo"] = tasa_pre
                resultado_pre["entrada"] = entrada_pre
            except Exception as exc:  # se re-lanza en el hilo principal
                resultado_pre["error"] = exc

        hilo_pre = threading.Thread(
            target=_precalificar_en_paralelo, name=f"precalifica-{corrida_id}", daemon=True
        )
        n_workers = resolver_workers(config, workers)
        _actualizar(config, corrida_id, fase_actual="worker")
        hilo_pre.start()
        inicio_worker = time.monotonic()
        metricas_worker: dict[str, Any]
        if n_workers <= 1:
            resumen_worker = procesar_hot(config, sink=sink, seguir_esperando=hilo_pre.is_alive)
            hilo_pre.join()
            metricas_worker = asdict(resumen_worker)
        else:
            metricas_worker = _correr_workers_en_paralelo(config, usar_indice, n_workers, hilo_pre)
        metricas_worker["procesos"] = n_workers
        if "error" in resultado_pre:
            raise resultado_pre["error"]

        fases.append(resultado_pre["entrada"])  # primero el filtro (orden lógico)
        entrada_worker: dict[str, Any] = {
            "fase": "worker",
            "duracion_s": round(time.monotonic() - inicio_worker, 2),
            "metricas": metricas_worker,
        }
        tasa_worker = _tasa(entrada_worker["metricas"], entrada_worker["duracion_s"])
        if tasa_worker is not None:
            entrada_worker["archivos_por_segundo"] = tasa_worker
        entrada_worker["en_paralelo_con_filtro"] = True
        fases.append(entrada_worker)
        _actualizar(config, corrida_id, fases=fases)
        log.info("fase_completa", corrida=corrida_id, fase="precalificacion+worker")

        correr("mover_frio", lambda: mover_frio(config))
        correr("verificacion", lambda: verificar_indexados(config))
        puerta = correr("puerta", lambda: evaluar_puerta(config, disco_id))
        _actualizar(
            config,
            corrida_id,
            estado="COMPLETADA",
            fase_actual=None,
            seguro_para_desechar=puerta.seguro_para_desechar,
            terminada_en=datetime.now(UTC),
        )
        log.info("corrida_completa", corrida=corrida_id, fases=len(fases))
    except Exception as exc:
        _actualizar(
            config,
            corrida_id,
            estado="FALLIDA",
            error=str(exc)[:500],
            terminada_en=datetime.now(UTC),
        )
        log.error("corrida_fallida", corrida=corrida_id, error=str(exc)[:300])
        raise
    return fases


def _fila_a_dict(fila: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": fila[0],
        "disco_id": fila[1],
        "ruta": fila[2],
        "estado": fila[3],
        "fase_actual": fila[4],
        "fases": fila[5],
        "seguro_para_desechar": fila[6],
        "error": fila[7],
        "iniciada_en": fila[8],
        "terminada_en": fila[9],
        "destino": fila[10],
    }


def consultar_estado(config: Config, historial: int = 10) -> dict[str, Any]:
    """Para GET /pipeline/estado: corrida en curso + historial + destinos."""
    columnas = (
        "id, disco_id, ruta, estado, fase_actual, fases, seguro_para_desechar,"
        " error, iniciada_en, terminada_en, destino"
    )
    with psycopg.connect(config.postgres_dsn) as conn:
        actual = conn.execute(
            f"SELECT {columnas} FROM corridas WHERE estado = 'EN_CURSO' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        pasadas = conn.execute(
            f"SELECT {columnas} FROM corridas WHERE estado != 'EN_CURSO' ORDER BY id DESC LIMIT %s",
            (historial,),
        ).fetchall()
        progreso: dict[str, int] | None = None
        if actual:  # avance EN VIVO de la fase actual: conteos de la cola del disco
            progreso = {
                fila[0]: int(fila[1])
                for fila in conn.execute(
                    "SELECT estado, COUNT(*) FROM archivos WHERE disco_id = %s GROUP BY estado",
                    (actual[1],),
                ).fetchall()
            }
    return {
        "progreso": progreso,
        "en_curso": _fila_a_dict(actual) if actual else None,
        "historial": [_fila_a_dict(f) for f in pasadas],
        "destinos": destinos(config),
    }


def resumen_panel(config: Config, tope_tipos: int = 24) -> dict[str, Any]:
    """Para GET /resumen: agregados de la tabla `archivos` para el panel de control.

    Cuenta archivos y BYTES por estado, por decisión (HOT/COLD/sin decidir) y por
    tipo real. A diferencia del índice, da visibilidad del FRÍO: cuántos datos —y qué
    porcentaje del peso— se están dejando de lado."""
    with psycopg.connect(config.postgres_dsn) as conn:
        # estado + decisión en UNA sola pasada de la tabla (GROUPING SETS)
        filas = conn.execute(
            """
            SELECT CASE WHEN GROUPING(estado) = 0 THEN 'estado' ELSE 'decision' END AS dim,
                   COALESCE(estado, ruta_decision, 'SIN_DECIDIR') AS clave,
                   COUNT(*) AS archivos,
                   COALESCE(SUM(tamano), 0) AS bytes
              FROM archivos
             GROUP BY GROUPING SETS ((estado), (ruta_decision))
            """
        ).fetchall()
        por_estado = [
            {"clave": f[1], "archivos": int(f[2]), "bytes": int(f[3])}
            for f in filas
            if f[0] == "estado"
        ]
        por_decision = [
            {"clave": f[1], "archivos": int(f[2]), "bytes": int(f[3])}
            for f in filas
            if f[0] == "decision"
        ]
        tipos = conn.execute(
            "SELECT tipo_real, COUNT(*), COALESCE(SUM(tamano), 0)"
            " FROM archivos WHERE tipo_real IS NOT NULL"
            " GROUP BY tipo_real ORDER BY 3 DESC LIMIT %s",
            (tope_tipos,),
        ).fetchall()
        por_tipo = [
            {"clave": f[0], "archivos": int(f[1]), "bytes": int(f[2])} for f in tipos
        ]
    return {
        "total_archivos": sum(g["archivos"] for g in por_estado),
        "bytes_totales": sum(g["bytes"] for g in por_estado),
        "por_estado": por_estado,
        "por_decision": por_decision,
        "por_tipo": por_tipo,
        "generado_en": datetime.now(UTC).isoformat(),
    }


_MOTIVOS_PRESERVADO = (
    "formato_no_soportado",  # RAR sin herramienta, imágenes de disco, tar exótico…
    "contenedor_corrupto",  # corrupto O con contraseña (cifrado)
    "contenedor_sin_explorar",
    "profundidad_maxima",  # anidación más honda que el tope K4
)


def preservados_sin_explorar(config: Config, limite: int = 200) -> dict[str, Any]:
    """Inventario de contenedores PRESERVADOS sin explorar (cifrados, corruptos,
    formatos pendientes, guards anti-bomba): visible de un vistazo en el front.

    NADA de esto se pierde: HOT = íntegro en el almacén permanente; COLD = copiado
    al frío reversible. Esta vista existe para que ese inventario no sea invisible."""
    condicion = "(motivo = ANY(%s) OR motivo LIKE 'zip_bomb_sospechoso:%%')"
    with psycopg.connect(config.postgres_dsn) as conn:
        por_motivo = {
            fila[0]: int(fila[1])
            for fila in conn.execute(
                f"SELECT motivo, COUNT(*) FROM archivos WHERE {condicion}"
                " GROUP BY motivo ORDER BY 2 DESC",
                (list(_MOTIVOS_PRESERVADO),),
            ).fetchall()
        }
        filas = conn.execute(
            "SELECT disco_id, ruta, nombre, tamano, tipo_real, motivo, estado"
            f" FROM archivos WHERE {condicion} ORDER BY tamano DESC LIMIT %s",
            (list(_MOTIVOS_PRESERVADO), limite),
        ).fetchall()
    return {
        "total": sum(por_motivo.values()),
        "por_motivo": por_motivo,
        "archivos": [
            {
                "disco_id": f[0],
                "ruta": f[1],
                "nombre": f[2],
                "tamano": int(f[3]),
                "tipo_real": f[4],
                "motivo": f[5],
                "estado": f[6],
            }
            for f in filas
        ],
    }


_COLUMNAS_COLA = (
    "archivo_id, disco_id, ruta, nombre, extension, tamano, mtime, estado, prioridad,"
    " intentos, error_motivo, puntaje, ruta_decision, tipo_real, senales, motivo,"
    " version_filtro, hash_contenido, actualizado_en"
)


def listar_archivos_cola(
    config: Config,
    *,
    estado: str | None = None,
    ruta_decision: str | None = None,
    motivo: str | None = None,
    error_motivo: str | None = None,
    extension: str | None = None,
    nombre: str | None = None,
    disco_id: str | None = None,
    puntaje_min: int | None = None,
    puntaje_max: int | None = None,
    cursor: str | None = None,
    limite: int = 50,
) -> dict[str, Any]:
    """Para GET /cola/archivos: el plano de control fila por fila, con filtros.

    A diferencia de /buscar (índice: solo lo INDEXADO), aquí se ve TODO — COLD,
    ERROR, pendientes — con puntaje, motivo y señales (entropía incluida). Es la
    vista para auditar si el filtro está decidiendo bien.

    `puntaje_min/max` permiten aislar la FRANJA GRIS (entre umbral_cold y
    umbral_hot): la zona donde el filtro decide con menos certeza — el dato de
    calibración por excelencia.

    Devuelve además `resumen` (composición por causa y por tipo con los MISMOS
    filtros): de un vistazo se ve por qué algo está donde está, no solo qué filas.

    Paginación keyset por `archivo_id` (índice ix_archivos_estado_id): el `cursor`
    es el archivo_id de la última fila vista; estable aunque la cola se mueva."""
    from normalizacion.core.modelo import Estado, RutaDecision

    limite = max(1, min(limite, 200))
    if estado is not None and estado not in {e.value for e in Estado}:
        raise ValueError(f"estado desconocido: {estado}")
    if ruta_decision is not None and ruta_decision not in {r.value for r in RutaDecision}:
        raise ValueError(f"ruta_decision desconocida: {ruta_decision}")

    condiciones: list[str] = []
    parametros: list[Any] = []
    for columna, valor in (
        ("estado", estado),
        ("ruta_decision", ruta_decision),
        ("disco_id", disco_id),
        ("extension", (extension or "").lower() or None),
    ):
        if valor is not None:
            condiciones.append(f"{columna} = %s")
            parametros.append(valor)
    if motivo:
        condiciones.append("motivo LIKE %s")
        parametros.append(f"{motivo}%")
    if error_motivo:
        condiciones.append("error_motivo ILIKE %s")
        parametros.append(f"%{error_motivo}%")
    if nombre:
        condiciones.append("nombre ILIKE %s")
        parametros.append(f"%{nombre}%")
    if puntaje_min is not None:
        condiciones.append("puntaje >= %s")
        parametros.append(puntaje_min)
    if puntaje_max is not None:
        condiciones.append("puntaje <= %s")
        parametros.append(puntaje_max)
    where = f" WHERE {' AND '.join(condiciones)}" if condiciones else ""

    with psycopg.connect(config.postgres_dsn) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM archivos{where}", parametros).fetchone()[0])  # type: ignore[index]
        # Composición por CAUSA (prefijo antes de ':' — error_motivo manda en ERROR)
        # y por TIPO REAL, con los mismos filtros. Es lo que permite calibrar.
        por_causa = conn.execute(
            "SELECT split_part(COALESCE(error_motivo, motivo, 'sin_decidir'), ':', 1),"
            f" COUNT(*), COALESCE(SUM(tamano), 0) FROM archivos{where}"
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
            parametros,
        ).fetchall()
        por_tipo = conn.execute(
            "SELECT COALESCE(tipo_real, 'sin_tipificar'), COUNT(*),"
            f" COALESCE(SUM(tamano), 0) FROM archivos{where}"
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
            parametros,
        ).fetchall()
        paginado = f"{where}{' AND' if where else ' WHERE'} archivo_id > %s" if cursor else where
        filas = conn.execute(
            f"SELECT {_COLUMNAS_COLA} FROM archivos{paginado}"
            " ORDER BY archivo_id LIMIT %s",
            [*parametros, *([cursor] if cursor else []), limite],
        ).fetchall()

    archivos = [
        {
            "archivo_id": f[0],
            "disco_id": f[1],
            "ruta": f[2],
            "nombre": f[3],
            "extension": f[4],
            "tamano": int(f[5]),
            "mtime": f[6],
            "estado": f[7],
            "prioridad": int(f[8]),
            "intentos": int(f[9]),
            "error_motivo": f[10],
            "puntaje": f[11],
            "ruta_decision": f[12],
            "tipo_real": f[13],
            "senales": f[14],
            "motivo": f[15],
            "version_filtro": f[16],
            "hash_contenido": f[17],
            "actualizado_en": f[18],
        }
        for f in filas
    ]
    return {
        "total": total,
        "archivos": archivos,
        "cursor": archivos[-1]["archivo_id"] if len(archivos) == limite else None,
        "resumen": {
            "por_causa": [
                {"clave": f[0], "archivos": int(f[1]), "bytes": int(f[2])} for f in por_causa
            ],
            "por_tipo": [
                {"clave": f[0], "archivos": int(f[1]), "bytes": int(f[2])} for f in por_tipo
            ],
        },
    }


# El tablero agrega la tabla `archivos` (millones de filas): recomputarlo cuesta
# segundos y en cada poll competía con la ingesta. Servimos un SNAPSHOT reciente
# con TTL corto y single-flight (un solo hilo recalcula; el resto reusa). El sello
# `generado_en` del payload deja la antigüedad a la vista.
_TABLERO_TTL_S = 15.0
_tablero_lock = threading.Lock()
_tablero_cache: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}


def invalidar_tablero() -> None:
    """Descarta el snapshot cacheado del tablero: la próxima lectura recalcula.

    La caché es un global de proceso con TTL de 15 s. En producción eso es lo
    deseado (el tablero agrega millones de filas y no puede recomputarse en cada
    poll), pero para quien escribe datos y necesita verlos YA —los tests, o una
    acción del operador que deba reflejarse al instante— hace falta una salida
    explícita. Sin ella, un cambio recién hecho parece no haber ocurrido."""
    with _tablero_lock:
        _tablero_cache.clear()


def tablero(config: Config, *, umbral_cold: int, umbral_hot: int) -> dict[str, Any]:
    """Para GET /panel: agregados del tablero, cacheados por TTL corto (single-flight).

    Devuelve un snapshot de como mucho `_TABLERO_TTL_S` segundos. Bajo el lock, si el
    snapshot está fresco se reutiliza al instante; si expiró, UN solo hilo lo recalcula
    y los concurrentes esperan ese resultado en vez de disparar N escaneos en paralelo
    contra `archivos`. Los umbrales forman parte de la clave (cambian la franja gris)."""
    clave = (umbral_cold, umbral_hot)
    with _tablero_lock:
        entrada = _tablero_cache.get(clave)
        if entrada is not None and time.monotonic() - entrada[0] < _TABLERO_TTL_S:
            return entrada[1]
        datos = _tablero_calcular(config, umbral_cold=umbral_cold, umbral_hot=umbral_hot)
        _tablero_cache[clave] = (time.monotonic(), datos)
        return datos


def _tablero_calcular(config: Config, *, umbral_cold: int, umbral_hot: int) -> dict[str, Any]:
    """Calcula TODOS los agregados del tablero de Inicio en una llamada.

    El tablero debe responder de un vistazo: ¿cuánto hay y en qué estado?, ¿qué
    proporción se va a frío y POR QUÉ?, ¿qué falló y de qué familia?, ¿dónde
    duda el filtro (histograma de puntajes vs umbrales)?, ¿cuánto ahorra el
    dedup?, ¿qué discos hay y cómo van?

    Rendimiento: estados, decisión, tipo, histograma, discos y franja gris se
    obtienen en UNA sola pasada de `archivos` con GROUPING SETS (antes eran 5-6
    escaneos independientes de millones de filas). Solo dedup (COUNT DISTINCT) y
    las causas COLD/ERROR —baratas por índice— van aparte.

    Los umbrales llegan ya EFECTIVOS (config base + overrides de la UI) para que
    la franja gris del tablero coincida con lo que el filtro hará en la próxima
    corrida."""

    def grupos(filas: list[Any]) -> list[dict[str, Any]]:
        return [{"clave": f[0], "archivos": int(f[1]), "bytes": int(f[2])} for f in filas]

    with psycopg.connect(config.postgres_dsn) as conn:
        # UNA pasada: estado, decisión, tipo, disco y cubeta de puntaje como GROUPING
        # SETS distintos; la franja gris y los totales salen del set gran-total (()).
        # `cubeta` se precalcula en la subconsulta para poder agruparla.
        combinado = conn.execute(
            """
            SELECT GROUPING(estado)        AS g_estado,
                   GROUPING(ruta_decision) AS g_decision,
                   GROUPING(tipo_real)     AS g_tipo,
                   GROUPING(disco_id)      AS g_disco,
                   GROUPING(cubeta)        AS g_hist,
                   estado, ruta_decision, tipo_real, disco_id, cubeta,
                   COUNT(*)                 AS archivos,
                   COALESCE(SUM(tamano), 0) AS bytes,
                   COUNT(*) FILTER (WHERE estado = 'HECHO') AS hechos,
                   COUNT(*) FILTER (WHERE estado = 'ERROR') AS errores,
                   COUNT(*) FILTER (WHERE puntaje BETWEEN %s AND %s) AS franja
              FROM (
                  SELECT estado, ruta_decision, tipo_real, disco_id, tamano, puntaje,
                         CASE WHEN puntaje IS NOT NULL THEN (LEAST(puntaje, 99) / 10) * 10 END AS cubeta
                    FROM archivos
              ) a
             GROUP BY GROUPING SETS ((estado), (ruta_decision), (tipo_real), (disco_id), (cubeta), ())
            """,
            (umbral_cold, umbral_hot - 1),
        ).fetchall()

        por_estado: list[dict[str, Any]] = []
        por_decision: list[dict[str, Any]] = []
        tipos_raw: list[dict[str, Any]] = []
        histograma_raw: list[dict[str, Any]] = []
        discos_raw: list[dict[str, Any]] = []
        franja_gris = 0
        for f in combinado:
            archivos, bytes_ = int(f[10]), int(f[11])
            if f[0] == 0:  # set (estado)
                por_estado.append(
                    {"clave": f[5] if f[5] is not None else "SIN_DECIDIR", "archivos": archivos, "bytes": bytes_}
                )
            elif f[1] == 0:  # set (ruta_decision)
                por_decision.append(
                    {"clave": f[6] if f[6] is not None else "SIN_DECIDIR", "archivos": archivos, "bytes": bytes_}
                )
            elif f[2] == 0:  # set (tipo_real) — descartamos el grupo NULL (WHERE tipo_real IS NOT NULL)
                if f[7] is not None:
                    tipos_raw.append({"clave": f[7], "archivos": archivos, "bytes": bytes_})
            elif f[3] == 0:  # set (disco_id)
                discos_raw.append(
                    {"disco_id": f[8], "archivos": archivos, "bytes": bytes_, "hechos": int(f[12]), "errores": int(f[13])}
                )
            elif f[4] == 0:  # set (cubeta) — descartamos el grupo NULL (WHERE puntaje IS NOT NULL)
                if f[9] is not None:
                    histograma_raw.append({"desde": int(f[9]), "archivos": archivos})
            else:  # set gran-total () — franja gris y totales
                franja_gris = int(f[14])

        # Orden/límite en Python (equivalen a los ORDER BY / LIMIT originales).
        por_tipo = sorted(tipos_raw, key=lambda g: g["bytes"], reverse=True)[:10]
        histograma = sorted(histograma_raw, key=lambda h: h["desde"])
        discos = sorted(discos_raw, key=lambda d: d["archivos"], reverse=True)[:12]

        causas_cold = grupos(
            conn.execute(
                "SELECT split_part(COALESCE(motivo, 'sin_motivo'), ':', 1), COUNT(*),"
                " COALESCE(SUM(tamano), 0) FROM archivos WHERE estado = 'COLD'"
                " GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
            ).fetchall()
        )
        causas_error = grupos(
            conn.execute(
                "SELECT split_part(COALESCE(error_motivo, 'sin_motivo'), ':', 1), COUNT(*),"
                " COALESCE(SUM(tamano), 0) FROM archivos WHERE estado = 'ERROR'"
                " GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
            ).fetchall()
        )

        # Dedup real: filas con blob vs blobs únicos (lo que el almacén ahorró).
        # COUNT(DISTINCT) no se fusiona con los GROUPING SETS: pasada propia.
        con_hash, hash_unicos = conn.execute(
            "SELECT COUNT(hash_contenido), COUNT(DISTINCT hash_contenido) FROM archivos"
        ).fetchone()  # type: ignore[misc]

        corridas = [
            {
                "id": int(f[0]),
                "ruta": f[1],
                "estado": f[2],
                "iniciada_en": f[3],
                "terminada_en": f[4],
                "duracion_s": (f[4] - f[3]).total_seconds() if f[4] else None,
            }
            for f in conn.execute(
                "SELECT id, ruta, estado, iniciada_en, terminada_en FROM corridas"
                " ORDER BY id DESC LIMIT 8"
            ).fetchall()
        ]

    por = {g["clave"]: g for g in por_estado}

    def n(clave: str) -> int:
        return int(por.get(clave, {"archivos": 0})["archivos"])

    return {
        "totales": {
            "archivos": sum(g["archivos"] for g in por_estado),
            "bytes": sum(g["bytes"] for g in por_estado),
            "hechos": n("HECHO"),
            "errores": n("ERROR"),
            "cold": n("COLD"),
            "en_proceso": n("EN_PROCESO") + n("INDEXADO") + n("VERIFICADO"),
            "pendientes": n("PENDIENTE") + n("PRECALIFICADO"),
            "franja_gris": franja_gris,
            "con_hash": int(con_hash),
            "hash_unicos": int(hash_unicos),
        },
        "por_estado": por_estado,
        "por_decision": por_decision,
        "por_tipo": por_tipo,
        "causas_cold": causas_cold,
        "causas_error": causas_error,
        "histograma_puntaje": histograma,
        "umbral_cold": umbral_cold,
        "umbral_hot": umbral_hot,
        "discos": discos,
        "corridas": corridas,
        "generado_en": datetime.now(UTC).isoformat(),
    }


def validar_dentro_de_raiz(ruta: Path, raiz: str | None) -> Path:
    """Si hay carpeta raíz configurada (Docker: /datos), nada sale de ella."""
    resuelta = ruta.expanduser().resolve()
    if raiz is not None:
        limite = Path(raiz).expanduser().resolve()
        if not resuelta.is_relative_to(limite):
            raise ValueError(f"fuera de la carpeta permitida ({limite})")
    return resuelta


def listar_carpetas(ruta: str | None, raiz: str | None = None) -> dict[str, Any]:
    """Explorador server-side para el selector del front (el navegador no puede
    dar rutas absolutas del sistema — este endpoint navega por él).

    Con `raiz` (Docker: el volumen /datos), la navegación queda CONFINADA a ella."""
    inicio = Path(ruta) if ruta else (Path(raiz) if raiz else Path.home())
    base = validar_dentro_de_raiz(inicio, raiz)
    if not base.is_dir():
        raise ValueError(f"no es una carpeta: {base}")
    try:
        carpetas = sorted(
            e.name for e in base.iterdir() if e.is_dir() and not e.name.startswith(".")
        )[:200]
    except PermissionError as exc:
        raise ValueError(f"sin permiso para leer {base}") from exc
    en_el_tope = raiz is not None and base == Path(raiz).expanduser().resolve()
    return {
        "ruta": str(base),
        "padre": None if en_el_tope or base.parent == base else str(base.parent),
        "carpetas": carpetas,
    }


_NOMBRE_CARPETA_PROHIBIDO = ("/", "\\", "..", ":")


def crear_carpeta(ruta: str, nombre: str, raiz: str | None) -> str:
    """Crea una subcarpeta (para elegir un destino nuevo desde el front).
    Confinada a la raíz de destino; el nombre no puede escapar (sin separadores)."""
    nombre = nombre.strip()
    if not nombre or any(p in nombre for p in _NOMBRE_CARPETA_PROHIBIDO):
        raise ValueError(f"nombre de carpeta inválido: {nombre!r}")
    base = validar_dentro_de_raiz(Path(ruta), raiz)
    if not base.is_dir():
        raise ValueError(f"no es una carpeta: {base}")
    nueva = base / nombre
    try:
        nueva.mkdir(exist_ok=True)
    except PermissionError as exc:
        raise ValueError(f"sin permiso para crear en {base}") from exc
    return str(nueva)
