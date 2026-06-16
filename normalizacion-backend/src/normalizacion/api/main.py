"""API de búsqueda (Fase 5 → M5): la capa delgada entre el front y OpenSearch.

PROPUESTA §9: OpenSearch JAMÁS se expone directo. El front consume este contrato
OpenAPI; el original se descarga del ALMACÉN por hash (el disco físico ya no existe).

(Sin `from __future__ import annotations`: FastAPI necesita evaluar los Annotated
locales — con annotations diferidas el Depends se degrada a query param.)
"""

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from normalizacion import __version__
from normalizacion.api import busqueda
from normalizacion.api.esquemas import (
    Corrida,
    Estadisticas,
    EstadoPipeline,
    FiltroVisible,
    RespuestaAutocompletar,
    EstadisticasEntidades,
    Entidad,
    RespuestaColaArchivos,
    RespuestaEntidades,
    RespuestaFiltro,
    RespuestaReprocesar,
    RespuestaTablero,
    SolicitudProponerMapeo,
    SolicitudProyectar,
    SolicitudReceta,
    ResumenPanel,
    RespuestaBusqueda,
    RespuestaCarpetas,
    RespuestaPreservados,
    SolicitudBusqueda,
    SolicitudCarpetaNueva,
    SolicitudFiltro,
    SolicitudPipeline,
    SolicitudReprocesar,
)
from normalizacion.api.seguridad import LimitadorPorMinuto, llave_valida
from normalizacion.core.almacen import Almacen, crear_almacen
from normalizacion.core.config import Config, PerillasFiltro, cargar_config

_BLOQUE_DESCARGA = 1024 * 1024


def crear_app(config: Config) -> FastAPI:
    aplicacion = FastAPI(
        title="Normalización masiva — API de búsqueda",
        version=__version__,
        description=(
            "Búsqueda por nombre/tipo sobre el índice y descarga de originales desde "
            "el almacén permanente. Paginación profunda con search_after + PIT."
        ),
    )
    from fastapi.middleware.cors import CORSMiddleware

    aplicacion.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.api_cors_origenes),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key"],
    )
    aplicacion.state.config = config
    aplicacion.state.limitador = LimitadorPorMinuto(config.api_solicitudes_por_minuto)
    aplicacion.state.cliente = None
    aplicacion.state.almacen = None

    def _cliente(request: Request) -> Any:
        if request.app.state.cliente is None:
            from normalizacion.core.indexador.opensearch import crear_cliente

            request.app.state.cliente = crear_cliente(request.app.state.config)
        return request.app.state.cliente

    def _almacen(request: Request) -> Almacen:
        if request.app.state.almacen is None:
            request.app.state.almacen = crear_almacen(request.app.state.config)
        almacen: Almacen = request.app.state.almacen
        return almacen

    def _autorizar(
        request: Request,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> str:
        cfg: Config = request.app.state.config
        if not llave_valida(cfg.api_keys, x_api_key):
            raise HTTPException(status_code=401, detail="API key inválida o ausente")
        identidad = x_api_key or (request.client.host if request.client else "anonimo")
        if not request.app.state.limitador.permitir(identidad):
            raise HTTPException(status_code=429, detail="Límite de solicitudes excedido")
        return identidad

    Autorizado = Annotated[str, Depends(_autorizar)]

    @aplicacion.post("/buscar", response_model=RespuestaBusqueda)
    def post_buscar(
        solicitud: SolicitudBusqueda, _: Autorizado, request: Request
    ) -> RespuestaBusqueda:
        """Búsqueda con filtros, facetas y paginación profunda (pasa `cursor` de vuelta)."""
        return busqueda.buscar(_cliente(request), request.app.state.config, solicitud)

    @aplicacion.get("/autocompletar", response_model=RespuestaAutocompletar)
    def get_autocompletar(
        q: str, _: Autorizado, request: Request, limite: int = 10
    ) -> RespuestaAutocompletar:
        if not q.strip():
            return RespuestaAutocompletar(sugerencias=[])
        sugerencias = busqueda.autocompletar(
            _cliente(request), request.app.state.config, q.strip(), limite
        )
        return RespuestaAutocompletar(sugerencias=sugerencias)

    @aplicacion.get("/archivo/{archivo_id}")
    def get_archivo(archivo_id: str, _: Autorizado, request: Request) -> dict[str, Any]:
        doc = busqueda.doc_por_id(_cliente(request), request.app.state.config, archivo_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="archivo no encontrado")
        return doc

    @aplicacion.get("/archivo/{archivo_id}/contenido")
    def get_contenido(archivo_id: str, _: Autorizado, request: Request) -> StreamingResponse:
        """El ORIGINAL, en streaming desde el almacén por hash — el disco ya no existe."""
        doc = busqueda.doc_por_id(_cliente(request), request.app.state.config, archivo_id)
        if doc is None or not doc.get("hash_contenido"):
            raise HTTPException(status_code=404, detail="archivo no encontrado")
        almacen = _almacen(request)
        try:
            blob = almacen.leer(doc["hash_contenido"])
        except Exception as exc:
            raise HTTPException(status_code=503, detail="almacén no disponible") from exc

        def _stream() -> Iterator[bytes]:
            try:
                while bloque := blob.read(_BLOQUE_DESCARGA):
                    yield bloque
            finally:
                blob.close()

        nombre = doc.get("nombre", archivo_id)
        return StreamingResponse(
            _stream(),
            media_type=doc.get("tipo_real") or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
        )

    @aplicacion.get("/estadisticas", response_model=Estadisticas)
    def get_estadisticas(_: Autorizado, request: Request) -> Estadisticas:
        return busqueda.estadisticas(_cliente(request), request.app.state.config)

    @aplicacion.get("/resumen", response_model=ResumenPanel)
    def get_resumen(_: Autorizado, request: Request) -> ResumenPanel:
        """Panel: archivos y BYTES por estado, decisión (HOT/COLD) y tipo, desde la
        cola (Postgres). Muestra el FRÍO que el índice no ve — cuántos datos se dejan."""
        from normalizacion.ingesta.pipeline import resumen_panel

        return ResumenPanel.model_validate(resumen_panel(request.app.state.config))

    # ------------------------------------------------------- pipeline de ingesta

    def _raiz_de_ambito(cfg: Config, ambito: str) -> str | None:
        """`datos` = carpeta a observar (Docker: /datos, solo lectura);
        `destino` = dónde guardar lo indexado (Docker: /destino, escribible)."""
        return cfg.api_carpeta_destino_raiz if ambito == "destino" else cfg.api_carpeta_raiz

    def _destino_eligible(cfg: Config) -> bool:
        # En Docker confinado SIN volumen de destino, elegir carpeta escribiría
        # dentro del contenedor (efímero) → no se ofrece. Dev nativo: siempre.
        return cfg.api_carpeta_destino_raiz is not None or cfg.api_carpeta_raiz is None

    @aplicacion.get("/sistema/carpetas", response_model=RespuestaCarpetas)
    def get_carpetas(
        _: Autorizado, request: Request, ruta: str | None = None, ambito: str = "datos"
    ) -> RespuestaCarpetas:
        """Explorador del filesystem del SERVIDOR (para los selectores de carpeta)."""
        from normalizacion.ingesta.pipeline import listar_carpetas

        try:
            return RespuestaCarpetas.model_validate(
                listar_carpetas(ruta, _raiz_de_ambito(request.app.state.config, ambito))
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @aplicacion.get("/sistema/destinos-disco")
    def get_destinos_disco(_: Autorizado, request: Request) -> dict[str, Any]:
        """Raíz real del almacén (carpeta del sistema) por disco — para mostrar la
        ubicación física del original aunque cada corrida haya elegido su carpeta."""
        from normalizacion.ingesta.pipeline import destinos_por_disco

        return destinos_por_disco(request.app.state.config)

    @aplicacion.post("/sistema/carpetas", response_model=RespuestaCarpetas)
    def post_carpetas(
        solicitud: SolicitudCarpetaNueva, _: Autorizado, request: Request
    ) -> RespuestaCarpetas:
        """Crea una subcarpeta de DESTINO (confinada a su raíz) y devuelve su listado."""
        from normalizacion.ingesta.pipeline import crear_carpeta, listar_carpetas

        cfg: Config = request.app.state.config
        try:
            nueva = crear_carpeta(solicitud.ruta, solicitud.nombre, cfg.api_carpeta_destino_raiz)
            return RespuestaCarpetas.model_validate(
                listar_carpetas(nueva, cfg.api_carpeta_destino_raiz)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @aplicacion.post("/pipeline/ejecutar")
    def post_pipeline(
        solicitud: SolicitudPipeline, _: Autorizado, request: Request
    ) -> dict[str, Any]:
        """Indexa una carpeta de punta a punta (en segundo plano). Una a la vez.

        Re-ejecutar sobre la misma carpeta es seguro e INCREMENTAL: solo lo
        nuevo/cambiado genera trabajo (carpeta viva). `destino` (opcional) =
        carpeta elegida en el front donde vivirán el almacén HOT y el frío."""
        import threading
        from pathlib import Path as RutaFs

        from normalizacion.ingesta.pipeline import (
            config_con_destino,
            ejecutar_corrida,
            iniciar_corrida,
            validar_dentro_de_raiz,
        )

        cfg: Config = request.app.state.config
        try:
            validar_dentro_de_raiz(RutaFs(solicitud.ruta), cfg.api_carpeta_raiz)
            if solicitud.destino is not None:
                if not _destino_eligible(cfg):
                    raise ValueError("este despliegue no tiene carpeta de destino montada")
                validar_dentro_de_raiz(RutaFs(solicitud.destino), cfg.api_carpeta_destino_raiz)
            cfg_corrida = config_con_destino(cfg, solicitud.destino)
            # Perillas editadas desde la UI (lista blanca, umbrales…): aplican a
            # ESTA corrida — el thread y sus workers reciben la config mergeada.
            from normalizacion.core.config_overrides import aplicar_overrides

            cfg_corrida = aplicar_overrides(cfg_corrida)
            corrida_id, disco_id = iniciar_corrida(
                cfg_corrida, RutaFs(solicitud.ruta), solicitud.disco_id, destino=solicitud.destino
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        def _en_fondo() -> None:
            import contextlib

            # La excepción ya quedó registrada (FALLIDA + log) dentro de ejecutar_corrida
            with contextlib.suppress(Exception):
                ejecutar_corrida(
                    cfg_corrida,
                    corrida_id,
                    RutaFs(solicitud.ruta).expanduser().resolve(),
                    disco_id,
                    workers=solicitud.workers,
                )

        threading.Thread(target=_en_fondo, name=f"pipeline-{corrida_id}", daemon=True).start()
        return {"corrida_id": corrida_id, "disco_id": disco_id}

    @aplicacion.get("/pipeline/estado", response_model=EstadoPipeline)
    def get_pipeline_estado(
        _: Autorizado, request: Request, historial: int = 10
    ) -> EstadoPipeline:
        """Corrida en curso (fase + métricas en vivo), historial y DESTINOS."""
        from normalizacion.ingesta.pipeline import consultar_estado, resolver_workers

        cfg = request.app.state.config
        crudo = consultar_estado(cfg, historial=max(0, min(historial, 200)))
        return EstadoPipeline(
            en_curso=Corrida.model_validate(crudo["en_curso"]) if crudo["en_curso"] else None,
            historial=[Corrida.model_validate(c) for c in crudo["historial"]],
            destinos=crudo["destinos"],
            progreso=crudo.get("progreso"),
            destino_eligible=_destino_eligible(cfg),
            workers_auto=resolver_workers(cfg, None),
        )

    @aplicacion.get("/pipeline/preservados", response_model=RespuestaPreservados)
    def get_preservados(_: Autorizado, request: Request) -> RespuestaPreservados:
        """Contenedores PRESERVADOS sin explorar (cifrados, corruptos, formatos
        pendientes, guards anti-bomba). Nada de esto se pierde — esta vista hace
        visible el inventario para revisión."""
        from normalizacion.ingesta.pipeline import preservados_sin_explorar

        return RespuestaPreservados.model_validate(
            preservados_sin_explorar(request.app.state.config)
        )

    @aplicacion.get("/panel", response_model=RespuestaTablero)
    def get_panel(_: Autorizado, request: Request) -> RespuestaTablero:
        """El tablero de Inicio completo: estados, frío/caliente, causas, errores
        por familia, histograma de puntajes vs umbrales EFECTIVOS, dedup, discos
        y corridas recientes — una sola llamada, una sola conexión."""
        import psycopg

        from normalizacion.core.config_overrides import filtro_efectivo, leer_overrides
        from normalizacion.ingesta.pipeline import tablero

        cfg: Config = request.app.state.config
        with psycopg.connect(cfg.postgres_dsn) as conn:
            overrides = leer_overrides(conn)
        filtro = filtro_efectivo(cfg, overrides)
        return RespuestaTablero.model_validate(
            tablero(cfg, umbral_cold=filtro.umbral_cold, umbral_hot=filtro.umbral_hot)
        )

    # ------------------------------------------------------ explorador de cola

    @aplicacion.get("/cola/archivos", response_model=RespuestaColaArchivos)
    def get_cola_archivos(
        _: Autorizado,
        request: Request,
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
    ) -> RespuestaColaArchivos:
        """El plano de control fila por fila: TODO lo catalogado (COLD, ERROR,
        pendientes…) con puntaje, motivo y señales — lo que el índice no ve.
        Es la vista para auditar si el filtro (entropía, lista blanca) decide bien."""
        from normalizacion.ingesta.pipeline import listar_archivos_cola

        try:
            return RespuestaColaArchivos.model_validate(
                listar_archivos_cola(
                    request.app.state.config,
                    estado=estado,
                    ruta_decision=ruta_decision,
                    motivo=motivo,
                    error_motivo=error_motivo,
                    extension=extension,
                    nombre=nombre,
                    disco_id=disco_id,
                    puntaje_min=puntaje_min,
                    puntaje_max=puntaje_max,
                    cursor=cursor,
                    limite=limite,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @aplicacion.post("/cola/reprocesar-errores", response_model=RespuestaReprocesar)
    def post_reprocesar_errores(
        solicitud: SolicitudReprocesar, _: Autorizado, request: Request
    ) -> RespuestaReprocesar:
        """Dead-letter → de vuelta a su etapa de origen. Las filas devueltas se
        procesan en la SIGUIENTE corrida (re-indexar la carpeta las drena)."""
        import psycopg

        from normalizacion.core import cola

        cfg: Config = request.app.state.config
        with psycopg.connect(cfg.postgres_dsn) as conn:
            destinos_reproceso = cola.reprocesar_errores(conn, solicitud.motivo_como)
            conn.commit()
        return RespuestaReprocesar(
            total=sum(destinos_reproceso.values()), destinos=destinos_reproceso
        )

    @aplicacion.post("/cola/rescore-frio")
    def post_rescore_frio(_: Autorizado, request: Request) -> dict[str, int]:
        """COLD → PENDIENTE: re-evaluar el frío con el filtro vigente (p. ej. tras
        ampliar la lista blanca). Se puntúa de nuevo en la siguiente corrida."""
        import psycopg

        from normalizacion.core import cola

        cfg: Config = request.app.state.config
        with psycopg.connect(cfg.postgres_dsn) as conn:
            re_encolados = cola.rescore_frio(conn)
            conn.commit()
        return {"re_encolados": re_encolados}

    @aplicacion.post("/cola/reexplorar-preservados")
    def post_reexplorar_preservados(_: Autorizado, request: Request) -> dict[str, int]:
        """Contenedores preservados sin explorar (RAR sin herramienta, formatos
        pendientes…) → PENDIENTE, para re-precalificarlos con las herramientas
        ya instaladas. rescore-frío NO sirve para esto: los preservados viven en
        HOT, no en COLD. Se exploran en la siguiente corrida."""
        import psycopg

        from normalizacion.core import cola

        cfg: Config = request.app.state.config
        with psycopg.connect(cfg.postgres_dsn) as conn:
            re_encolados = cola.reexplorar_preservados(conn)
            conn.commit()
        return {"re_encolados": re_encolados}

    # ------------------------------------------------------ entidades (Fase 2)

    @aplicacion.get("/entidades", response_model=RespuestaEntidades)
    def get_entidades(
        _: Autorizado, request: Request, tipo: str | None = None,
        curp: str | None = None, nombre: str | None = None,
        cursor: str | None = None, limite: int = 50,
    ) -> RespuestaEntidades:
        """Lista las entidades canónicas resueltas (esquema Fz1 en `campos`)."""
        from normalizacion.entidades.consultas import listar_entidades

        return RespuestaEntidades.model_validate(
            listar_entidades(
                request.app.state.config, tipo=tipo, curp=curp, nombre=nombre,
                cursor=cursor, limite=limite,
            )
        )

    @aplicacion.get("/entidades/estadisticas", response_model=EstadisticasEntidades)
    def get_entidades_stats(_: Autorizado, request: Request) -> EstadisticasEntidades:
        from normalizacion.entidades.consultas import estadisticas

        return EstadisticasEntidades.model_validate(estadisticas(request.app.state.config))

    # --- recetas de proyección (DINÁMICAS): antes de /entidades/{id} para no chocar ---

    @aplicacion.get("/entidades/recetas")
    def get_recetas(
        _: Autorizado, request: Request, clase: str | None = None
    ) -> list[dict[str, Any]]:
        from normalizacion.entidades.recetas_db import listar_recetas

        return listar_recetas(request.app.state.config, clase)

    @aplicacion.put("/entidades/recetas/{clave}")
    def put_receta(
        clave: str, solicitud: SolicitudReceta, _: Autorizado, request: Request
    ) -> dict[str, Any]:
        """Crea o edita una receta de proyección (esquema de salida de un sistema)."""
        from normalizacion.entidades.recetas_db import guardar_receta

        if solicitud.clave != clave:
            raise HTTPException(status_code=400, detail="la clave del cuerpo y la ruta difieren")
        try:
            return guardar_receta(request.app.state.config, solicitud.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @aplicacion.delete("/entidades/recetas/{clave}")
    def delete_receta(clave: str, _: Autorizado, request: Request) -> dict[str, bool]:
        from normalizacion.entidades.recetas_db import borrar_receta

        try:
            ok = borrar_receta(request.app.state.config, clave)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="receta no encontrada")
        return {"borrada": True}

    @aplicacion.get("/entidades/exportar")
    def get_entidades_exportar(
        receta: str, _: Autorizado, request: Request, limite: int = 10000
    ) -> Any:
        """Exporta TODAS las personas activas a un SOLO archivo con la receta indicada.
        Con una receta de colección (p.ej. fz1_bundle) produce el archivo Fz1 completo
        (sobre _metadata + personas[] + _mapeo). Devuelve el archivo tal cual."""
        from normalizacion.entidades.consultas import exportar
        from normalizacion.entidades.recetas_db import leer_receta

        rec = leer_receta(request.app.state.config, receta)
        if rec is None:
            raise HTTPException(status_code=404, detail="receta no encontrada")
        return exportar(request.app.state.config, rec["definicion"], limite=limite)

    @aplicacion.get("/entidades/{entidad_id}", response_model=Entidad)
    def get_entidad(entidad_id: str, _: Autorizado, request: Request) -> Entidad:
        from normalizacion.entidades.consultas import obtener_entidad

        doc = obtener_entidad(request.app.state.config, entidad_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="entidad no encontrada")
        return Entidad.model_validate(doc)

    @aplicacion.get("/entidades/{entidad_id}/proyectar")
    def get_entidad_proyectada(
        entidad_id: str, receta: str, _: Autorizado, request: Request
    ) -> dict[str, Any]:
        """La MISMA persona canónica, proyectada al esquema de la receta indicada —
        la prueba visible del dinamismo: distintos sistemas, distintas estructuras."""
        from normalizacion.entidades.consultas import obtener_entidad
        from normalizacion.entidades.proyeccion import aplicar_proyeccion, es_coleccion
        from normalizacion.entidades.recetas_db import leer_receta

        ent = obtener_entidad(request.app.state.config, entidad_id)
        if ent is None:
            raise HTTPException(status_code=404, detail="entidad no encontrada")
        rec = leer_receta(request.app.state.config, receta)
        if rec is None:
            raise HTTPException(status_code=404, detail="receta no encontrada")
        if es_coleccion(rec["definicion"]):
            raise HTTPException(
                status_code=400,
                detail="esa receta arma el ARCHIVO completo (colección); para una persona "
                       "usa una receta por-ítem, o exporta con GET /entidades/exportar",
            )
        return {
            "receta": receta,
            "salida": aplicar_proyeccion(ent["campos"], rec["definicion"]),
        }

    @aplicacion.post("/entidades/{entidad_id}/activo")
    def post_activo(
        entidad_id: str, _: Autorizado, request: Request, activo: bool = True
    ) -> dict[str, bool]:
        """Contingencia LFPDPPP: desactiva (soft-delete) o reactiva una entidad."""
        from normalizacion.entidades.consultas import fijar_activo

        if not fijar_activo(request.app.state.config, entidad_id, activo):
            raise HTTPException(status_code=404, detail="entidad no encontrada")
        return {"activo": activo}

    @aplicacion.post("/entidades/mapeo/proponer")
    def post_proponer_mapeo(
        solicitud: SolicitudProponerMapeo, _: Autorizado, request: Request
    ) -> dict[str, Any]:
        """E2: propone columna→campo (sinónimos + contenido) para que el operador
        confirme. No persiste — eso es el paso de aprobación."""
        from normalizacion.entidades import mapeo
        from normalizacion.entidades.receta import obtener_receta

        try:
            receta = obtener_receta(solicitud.tipo)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prop = mapeo.proponer_mapeo(receta, solicitud.columnas, solicitud.muestras)
        return {"huella": mapeo.huella_columnas(solicitud.columnas), "propuestas": prop}

    @aplicacion.post("/entidades/proyectar")
    def post_proyectar(
        solicitud: SolicitudProyectar, _: Autorizado, request: Request
    ) -> dict[str, int]:
        """E3: proyecta filas ya mapeadas a entidades (resolución por ancla,
        idempotente). Útil para CLI/integraciones; la proyección desde los blobs
        indexados es el camino a escala (E4)."""
        from normalizacion.entidades.pipeline import proyectar
        from normalizacion.entidades.receta import obtener_receta

        try:
            receta = obtener_receta(solicitud.tipo)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        r = proyectar(request.app.state.config, receta, solicitud.asignacion, solicitud.filas)
        return r.como_dict()

    @aplicacion.post("/entidades/backfill")
    def post_backfill(
        _: Autorizado, request: Request, lote: int = 500, max_docs: int = 2000,
        reiniciar: bool = False,
    ) -> dict[str, Any]:
        """E4 (1er paso): resuelve entidades de los registros YA INDEXADOS que traen
        CURP/RFC. Acotado por max_docs para no bloquear la API; reanudable (el cursor
        de avance vive en `control`, así que re-llamar continúa donde quedó)."""
        from normalizacion.entidades.backfill import backfill_desde_indice

        r = backfill_desde_indice(
            request.app.state.config, lote=lote, max_docs=max_docs, reiniciar=reiniciar,
        )
        return r.como_dict()

    # --------------------------------------------------------- filtro editable

    def _filtro_visible(filtro: PerillasFiltro) -> FiltroVisible:
        return FiltroVisible(
            modo_lista=filtro.modo_lista,
            tipos_interes=sorted(filtro.tipos_interes),
            tipos_interes_prefijos=list(filtro.tipos_interes_prefijos),
            tipos_excluidos=sorted(filtro.tipos_excluidos),
            entropia_texto_max=filtro.entropia_texto_max,
            entropia_comprimido_min=filtro.entropia_comprimido_min,
            ratio_imprimibles_min=filtro.ratio_imprimibles_min,
            umbral_hot=filtro.umbral_hot,
            umbral_cold=filtro.umbral_cold,
            prioridad_contenedores=filtro.prioridad_contenedores,
            prioridad_extensiones=dict(filtro.prioridad_extensiones),
            version_filtro=filtro.version_filtro,
        )

    def _respuesta_filtro(cfg: Config, overrides: dict[str, Any]) -> RespuestaFiltro:
        from normalizacion.core.config_overrides import filtro_efectivo

        return RespuestaFiltro(
            efectivo=_filtro_visible(filtro_efectivo(cfg, overrides)),
            overrides=overrides,
            hay_overrides=bool(overrides),
        )

    @aplicacion.get("/filtro", response_model=RespuestaFiltro)
    def get_filtro(_: Autorizado, request: Request) -> RespuestaFiltro:
        """El filtro EFECTIVO de la siguiente corrida (config base + overrides).
        Siempre se lee de la BD — nunca de la config del proceso."""
        import psycopg

        from normalizacion.core.config_overrides import leer_overrides

        cfg: Config = request.app.state.config
        with psycopg.connect(cfg.postgres_dsn) as conn:
            overrides = leer_overrides(conn)
        return _respuesta_filtro(cfg, overrides)

    @aplicacion.put("/filtro", response_model=RespuestaFiltro)
    def put_filtro(
        solicitud: SolicitudFiltro, _: Autorizado, request: Request
    ) -> RespuestaFiltro:
        """Edita perillas del filtro (merge sobre lo ya editado). Aplica a la
        SIGUIENTE corrida; el frío existente se re-evalúa con /cola/rescore-frio.
        Si no se manda version_filtro, se deriva una auditada (+ov-<huella>)."""
        import psycopg

        from normalizacion.core.config_overrides import (
            derivar_version,
            filtro_efectivo,
            guardar_overrides,
            leer_overrides,
        )

        cfg: Config = request.app.state.config
        nuevos = solicitud.model_dump(exclude_none=True)
        if not nuevos:
            raise HTTPException(status_code=400, detail="nada que editar")
        with psycopg.connect(cfg.postgres_dsn) as conn:
            overrides = leer_overrides(conn)
            overrides.update(nuevos)
            if "version_filtro" not in nuevos:
                overrides["version_filtro"] = derivar_version(
                    cfg.filtro.version_filtro, overrides
                )
            try:
                filtro_efectivo(cfg, overrides)  # re-valida ANTES de persistir
                guardar_overrides(conn, overrides)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            conn.commit()
        return _respuesta_filtro(cfg, overrides)

    @aplicacion.delete("/filtro", response_model=RespuestaFiltro)
    def delete_filtro(_: Autorizado, request: Request) -> RespuestaFiltro:
        """Restablece el filtro a la config base (borra todos los overrides)."""
        import psycopg

        from normalizacion.core.config_overrides import borrar_overrides

        cfg: Config = request.app.state.config
        with psycopg.connect(cfg.postgres_dsn) as conn:
            borrar_overrides(conn)
            conn.commit()
        return _respuesta_filtro(cfg, {})

    @aplicacion.on_event("startup")
    def _rescatar_corridas_huerfanas() -> None:
        import contextlib

        from normalizacion.ingesta.pipeline import marcar_corridas_huerfanas

        with contextlib.suppress(Exception):  # Postgres puede tardar en estar listo
            marcar_corridas_huerfanas(config)

    @aplicacion.on_event("startup")
    def _sembrar_recetas() -> None:
        import contextlib

        import psycopg

        from normalizacion.entidades.recetas_db import seed_recetas

        with contextlib.suppress(Exception):  # la tabla puede no existir aún (migración)
            with psycopg.connect(config.postgres_dsn) as conn:
                seed_recetas(conn)

    return aplicacion


# Entrypoint para uvicorn (`norm api`): la config sale del entorno NORM_*
app = crear_app(cargar_config())
