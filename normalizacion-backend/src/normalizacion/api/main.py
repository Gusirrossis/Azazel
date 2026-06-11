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
    RespuestaAutocompletar,
    ResumenPanel,
    RespuestaBusqueda,
    RespuestaCarpetas,
    RespuestaPreservados,
    SolicitudBusqueda,
    SolicitudCarpetaNueva,
    SolicitudPipeline,
)
from normalizacion.api.seguridad import LimitadorPorMinuto, llave_valida
from normalizacion.core.almacen import Almacen, crear_almacen
from normalizacion.core.config import Config, cargar_config

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
        allow_methods=["GET", "POST"],
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
    def get_pipeline_estado(_: Autorizado, request: Request) -> EstadoPipeline:
        """Corrida en curso (fase + métricas en vivo), historial y DESTINOS."""
        from normalizacion.ingesta.pipeline import consultar_estado, resolver_workers

        cfg = request.app.state.config
        crudo = consultar_estado(cfg)
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

    @aplicacion.on_event("startup")
    def _rescatar_corridas_huerfanas() -> None:
        import contextlib

        from normalizacion.ingesta.pipeline import marcar_corridas_huerfanas

        with contextlib.suppress(Exception):  # Postgres puede tardar en estar listo
            marcar_corridas_huerfanas(config)

    return aplicacion


# Entrypoint para uvicorn (`norm api`): la config sale del entorno NORM_*
app = crear_app(cargar_config())
