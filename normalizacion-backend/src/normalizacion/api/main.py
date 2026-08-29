"""API de búsqueda (Fase 5 → M5): la capa delgada entre el front y OpenSearch.

PROPUESTA §9: OpenSearch JAMÁS se expone directo. El front consume este contrato
OpenAPI; el original se descarga del ALMACÉN por hash (el disco físico ya no existe).

(Sin `from __future__ import annotations`: FastAPI necesita evaluar los Annotated
locales — con annotations diferidas el Depends se degrada a query param.)
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from normalizacion import __version__
from normalizacion.api import busqueda, claves_busqueda, roles, sesiones, usuarios
from normalizacion.api import contrasena as pwd
from normalizacion.api.esquemas import (
    ClaveBusqueda,
    Corrida,
    Entidad,
    Estadisticas,
    EstadisticasEntidades,
    EstadoPipeline,
    FiltroVisible,
    Identidad,
    RespuestaAutocompletar,
    RespuestaBusqueda,
    RespuestaCarpetas,
    RespuestaClaveGenerada,
    RespuestaColaArchivos,
    RespuestaEntidades,
    RespuestaFiltro,
    RespuestaPreservados,
    RespuestaReprocesar,
    RespuestaTablero,
    ResumenPanel,
    Sesion,
    SolicitudAtributos,
    SolicitudBusqueda,
    SolicitudCambioContrasena,
    SolicitudCarpetaNueva,
    SolicitudClaveBusqueda,
    SolicitudDestino,
    SolicitudFiltro,
    SolicitudLogin,
    SolicitudPipeline,
    SolicitudProponerMapeo,
    SolicitudProyectar,
    SolicitudReceta,
    SolicitudRecursos,
    SolicitudReprocesar,
    SolicitudUsuarioCambio,
    SolicitudUsuarioNuevo,
    UsuarioPanel,
)
from normalizacion.api.roles import Rol
from normalizacion.api.seguridad import FrenoDeIntentos, LimitadorPorMinuto
from normalizacion.core.almacen import Almacen, crear_almacen
from normalizacion.core.config import Config, PerillasFiltro, cargar_config
from normalizacion.core.observabilidad import obtener_logger

log = obtener_logger("api")

#: Traza de eventos de seguridad. La 0008 justifica desactivar en vez de borrar
#: usuarios "para no perder la traza de quién hizo qué" — pero sin estos logs esa
#: traza no existía en ninguna parte: ni el login, ni un alta, ni un cambio de rol
#: dejaban rastro. Nunca se registra la contraseña ni el token, solo QUIÉN y QUÉ.
def _auditar(evento: str, request: Request, **datos: Any) -> None:
    log.info(evento, ip=_ip_cliente(request), **datos)


_BLOQUE_DESCARGA = 1024 * 1024


@dataclass(frozen=True, slots=True)
class QuienEs:
    """Identidad ya resuelta de un request, venga de una cookie o de una API key.

    Antes esta dependencia devolvía un `str` que servía a la vez de identidad y de
    cubeta del rate-limit. Con roles hace falta algo más: quién es, con qué rol y
    por qué vía entró.
    """

    #: 'sesion' (persona) | 'clave' (máquina) | 'abierta' (instalación sin configurar)
    tipo: str
    usuario: str
    nombre: str
    rol: str
    #: Cubeta del rate-limit. Nunca es la clave en claro: ver `de_clave_nombrada`.
    identidad: str
    usuario_id: int | None = None
    debe_cambiar: bool = False

    @classmethod
    def de_sesion(cls, sesion: Any) -> "QuienEs":
        return cls(
            tipo="sesion",
            usuario=sesion.usuario,
            nombre=sesion.nombre,
            rol=sesion.rol,
            identidad=f"u:{sesion.usuario_id}",
            usuario_id=sesion.usuario_id,
            debe_cambiar=sesion.debe_cambiar,
        )

    @classmethod
    def de_clave_nombrada(cls, clave: str) -> "QuienEs":
        """Clave de consumidor: entra como `lector` y nada más. Se llama "clave de
        acceso al buscador" — buscar y descargar es exactamente su alcance."""
        return cls(
            tipo="clave", usuario="(clave)", nombre="Consumidor externo",
            rol="lector", identidad=f"k:{_cubeta(clave)}",
        )

    @classmethod
    def de_clave_estatica(cls, clave: str) -> "QuienEs":
        """`NORM_API_KEYS` del `.env.prod`: acceso de emergencia con rol `admin`. La
        pone quien ya controla el servidor, así que no concede nada que no tuviera."""
        return cls(
            tipo="clave", usuario="(clave-estatica)", nombre="Acceso de emergencia",
            rol="admin", identidad=f"k:{_cubeta(clave)}",
        )

    @classmethod
    def anonima(cls, request: Request) -> "QuienEs":
        """Instalación sin usuarios ni claves: hay que poder crear al primer admin."""
        ip = _ip_cliente(request)
        return cls(
            tipo="abierta", usuario="(sin configurar)", nombre="",
            rol="admin", identidad=f"ip:{ip}",
        )


def _cubeta(clave: str) -> str:
    """Identificador estable y corto de una clave, para agrupar su rate-limit sin
    quedarse el secreto en memoria ni arriesgarlo en un log."""
    import hashlib

    return hashlib.sha256(clave.encode("utf-8")).hexdigest()[:16]


def _ip_cliente(request: Request) -> str:
    """IP real de quien llama, mirando `X-Forwarded-For` si viene.

    En producción la API solo se alcanza a través de Caddy y del nginx del front,
    así que `request.client.host` sería siempre la IP del contenedor de al lado —
    la misma para todos. El freno del login cuenta fallos por IP: sin esto, cinco
    intentos de un atacante bloquearían el acceso de todo el mundo.

    Se usa `X-Real-IP`, que el nginx del front reescribe SIEMPRE con `$remote_addr`,
    y no la entrada izquierda de `X-Forwarded-For`. Esa izquierda la escribe el
    CLIENTE: `$proxy_add_x_forwarded_for` añade la IP real por detrás, así que un
    atacante que mande su propia cabecera controla al 100% lo que veríamos como
    "su" IP — y con eso el freno del login por IP se esquiva mandando una distinta
    en cada intento.

    Como respaldo se toma la entrada de MÁS A LA DERECHA de `X-Forwarded-For`, que
    es la que añadió nuestro propio proxy y el cliente no puede empujar.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real[:60]
    reenviada = request.headers.get("x-forwarded-for", "")
    if reenviada:
        ultima = reenviada.split(",")[-1].strip()
        if ultima:
            return ultima[:60]
    return request.client.host if request.client else "anonimo"


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

    # `*` + `allow_credentials` es una combinación explosiva: Starlette refleja el
    # Origin de quien pregunte, y con la cookie de sesión eso convierte cualquier web
    # del mundo en un cliente autenticado del panel. `NORM_API_CORS_ORIGENES` es libre
    # en el `.env.prod`, así que el comentario de "nunca *" no bastaba: se impone aquí.
    origenes = [o for o in config.api_cors_origenes if o != "*"]
    if len(origenes) != len(config.api_cors_origenes):
        log.warning(
            "cors_comodin_descartado",
            detalle="'*' es incompatible con la cookie de sesión; enumera los orígenes",
        )
    aplicacion.add_middleware(
        CORSMiddleware,
        allow_origins=origenes,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key"],
        # El front de dev corre en otro puerto (5173) que la API: sin esto el
        # navegador no manda la cookie de sesión y el login "funciona" pero ningún
        # request posterior va autenticado. Exige orígenes explícitos, nunca `*`.
        allow_credentials=True,
    )
    # Política de recursos persistida (K15): se mergea al arrancar para que el
    # gobernador y los daemons (envío) usen lo que el operador dejó configurado en la
    # UI, sin reiniciar. Best-effort: si la BD aún no responde, se usa la base.
    try:
        from normalizacion.core.config_overrides import aplicar_recursos

        config = aplicar_recursos(config)
    except Exception:
        pass

    aplicacion.state.config = config
    aplicacion.state.limitador = LimitadorPorMinuto(config.api_solicitudes_por_minuto)
    aplicacion.state.freno_login = FrenoDeIntentos(
        max_intentos=config.login_max_intentos, bloqueo_seg=float(config.login_bloqueo_seg)
    )
    aplicacion.state.cliente = None
    aplicacion.state.almacen = None

    # Higiene de la tabla de sesiones y aviso de arranque, en un hilo aparte.
    #
    # En un HILO y no en línea porque `crear_app` NO puede depender de que Postgres
    # responda: cada intento de conexión gasta hasta `connect_timeout` segundos, y
    # construir la app es algo que hacen también los tests y el generador de OpenAPI,
    # donde no hay ninguna base de datos. En línea, esto convertía cada construcción
    # de la app en varios segundos de espera contra un puerto cerrado.
    #
    # Best-effort además: si la BD no responde, la API arranca igual y las sesiones
    # funcionan en cuanto responda — un panel que no levanta es peor que uno sin barrer.
    def _higiene_arranque() -> None:
        try:
            sesiones.barrer(config)
            if not usuarios.hay_alguno(config):
                import structlog

                structlog.get_logger(__name__).warning(
                    "no hay usuarios: la API acepta cualquier petición hasta que crees el "
                    "primero con `norm usuarios crear <usuario> --rol admin`",
                )
        except Exception:
            pass

    import threading

    threading.Thread(target=_higiene_arranque, name="higiene-sesiones", daemon=True).start()

    # ⚙K16 — topología de este nodo. Las capacidades se derivan UNA vez al construir
    # la app; los endpoints preguntan por ellas, nunca por el perfil.
    from normalizacion.core import despliegue

    topologia = despliegue.derivar(config.despliegue)
    aplicacion.state.topologia = topologia

    # Envío automático al AEB: hilo daemon que manda lo nuevo/cambiado cada N segundos
    # (intervalo configurable en la pestaña Destino; 0 = solo manual).
    #
    # SÓLO en el nodo que resuelve entidades. Antes arrancaba en TODA instancia de la
    # API; con dos nodos eso son dos procesos empujando al AEB, y como el cable manda
    # `modo_merge: "reemplazar"` (last-write-wins) cada uno sobrescribiría al otro con
    # su versión PARCIAL de la misma persona — cada nodo resolvió sobre su trozo del
    # índice. No corrompe (entidad_id es determinista y el AEB idempotente), pero se
    # pisan en bucle indefinidamente.
    if topologia.corre_entidades:
        from normalizacion.entidades.envio import iniciar_bucle

        iniciar_bucle(config)

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
    ) -> QuienEs:
        """Resuelve QUIÉN hace el request por dos vías, en este orden:

        1. **Cookie de sesión** — una persona con usuario y contraseña. Trae su rol.
        2. **`X-API-Key`** — un consumidor máquina (reddoor, el AEB), que no tiene
           navegador ni cookies.

        Las dos vías no son intercambiables en permisos. Las claves CON NOMBRE son,
        literalmente, "claves de acceso al buscador": entran como `lector` y no
        pueden administrar nada. Las estáticas de `NORM_API_KEYS` sí son `admin`:
        las pone quien controla el `.env.prod` del servidor, y son el acceso de
        emergencia para cuando no se puede entrar por el panel.
        """
        cfg: Config = request.app.state.config

        token = request.cookies.get(sesiones.COOKIE)
        if token:
            sesion = sesiones.validar(cfg, token)
            if sesion is not None:
                return _con_limite(request, QuienEs.de_sesion(sesion))

        if x_api_key:
            if x_api_key in tuple(cfg.api_keys):
                return _con_limite(request, QuienEs.de_clave_estatica(x_api_key))
            # `coincide`, NO `autorizada`: esta última devuelve True cuando no hay
            # NINGUNA clave configurada (su modo abierto de dev), y usarla aquí
            # convertía cualquier cabecera inventada en un acceso válido de rol
            # lector — es decir, el índice entero y la descarga de todos los
            # originales, aunque ya existieran usuarios. El modo abierto se decide
            # abajo y en un solo sitio.
            if claves_busqueda.coincide(cfg, x_api_key):
                return _con_limite(request, QuienEs.de_clave_nombrada(x_api_key))
            raise HTTPException(status_code=401, detail="API key inválida")

        # Instalación recién creada: sin usuarios NI claves —ni estáticas ni con
        # nombre— no hay forma de entrar y tampoco hay nada que proteger. Se deja
        # abierto para poder dar de alta al primer admin. En cuanto existe un usuario
        # o una clave, esta puerta se cierra sola.
        if not claves_busqueda.hay_alguna(cfg) and not usuarios.hay_alguno_cacheado(cfg):
            return _con_limite(request, QuienEs.anonima(request))

        raise HTTPException(status_code=401, detail="Sesión requerida")

    def _con_limite(request: Request, quien: QuienEs) -> QuienEs:
        if not request.app.state.limitador.permitir(quien.identidad):
            raise HTTPException(status_code=429, detail="Límite de solicitudes excedido")
        return quien

    def _exige(minimo: Rol) -> Any:
        """Dependencia que exige un rol mínimo. 403, no 401: el usuario está bien
        identificado, simplemente no le alcanza — decirle "inicia sesión" cuando ya
        la tiene abierta lo manda a un bucle sin salida."""

        def verificar(quien: Annotated[QuienEs, Depends(_autorizar)]) -> QuienEs:
            # Contraseña temporal puesta por un admin: hasta que el dueño la cambie,
            # la cuenta solo sirve para cambiarla. Esconder el panel en el front no
            # bastaba — la contraseña la conoce quien la creó, así que sin este corte
            # servía indefinidamente contra la API con curl.
            if quien.debe_cambiar:
                raise HTTPException(
                    status_code=403,
                    detail="Debes cambiar tu contraseña antes de usar el panel",
                )
            if not roles.alcanza(quien.rol, minimo):
                raise HTTPException(
                    status_code=403,
                    detail=f"Requiere rol '{minimo}'; el tuyo es '{quien.rol}'",
                )
            return quien

        return verificar

    # `Autorizado` = autenticado, con rol `lector` como mínimo. Es el suelo de toda
    # la API y lo que ya usaban los endpoints existentes, así que no hay que tocarlos.
    Autorizado = Annotated[QuienEs, Depends(_exige("lector"))]
    Operador = Annotated[QuienEs, Depends(_exige("operador"))]
    Admin = Annotated[QuienEs, Depends(_exige("admin"))]
    # Autenticado pero SIN el corte por `debe_cambiar`: es el mínimo que necesitan los
    # tres endpoints con los que una cuenta recién creada sale de ese estado. Si
    # exigieran `Autorizado`, la cuenta quedaría encerrada sin forma de arreglarse.
    Identificado = Annotated[QuienEs, Depends(_autorizar)]

    def _sesion_actual(request: Request) -> tuple[str | None, Any]:
        token = request.cookies.get(sesiones.COOKIE)
        return token, (sesiones.validar(request.app.state.config, token) if token else None)

    def _poner_cookie(respuesta: Response, token: str, cfg: Config) -> None:
        respuesta.set_cookie(
            sesiones.COOKIE,
            token,
            max_age=cfg.sesion_duracion_min * 60,
            httponly=True,  # fuera del alcance de cualquier JS inyectado
            secure=cfg.sesion_cookie_secure,
            samesite=cfg.sesion_cookie_samesite,  # type: ignore[arg-type]
            path="/",
        )

    # ---------------- Sesión (login de personas) ----------------

    @aplicacion.post("/auth/login", response_model=Identidad)
    def post_login(
        solicitud: SolicitudLogin, request: Request, respuesta: Response
    ) -> Identidad:
        """Abre sesión y deja la cookie. Es el único endpoint sin autenticar."""
        cfg: Config = request.app.state.config
        ip = _ip_cliente(request)
        usuario_norm = usuarios.normalizar(solicitud.usuario)
        freno = request.app.state.freno_login

        # Caudal ANTES de tocar argon2. `post_login` es el único endpoint sin
        # `Autorizado`, así que no pasa por `_con_limite`: sin esto, una petición
        # anónima con un usuario inventado cuesta ~80 ms y 64 MiB de argon2 (el hash
        # señuelo se verifica igual), y basta un bucle para llevarse el VPS por
        # delante — los endpoints síncronos corren en el threadpool de Starlette.
        if not request.app.state.limitador.permitir(f"login:{ip}"):
            raise HTTPException(status_code=429, detail="Demasiadas peticiones de login")

        # Se frena por usuario Y por IP: ver `FrenoDeIntentos` para el porqué.
        espera = freno.bloqueado(f"u:{usuario_norm}", f"ip:{ip}")
        if espera > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Demasiados intentos fallidos. Reintenta en {int(espera) + 1} s.",
                headers={"Retry-After": str(int(espera) + 1)},
            )

        encontrado = usuarios.verificar_credenciales(cfg, solicitud.usuario, solicitud.contrasena)
        if encontrado is None:
            freno.registrar_fallo(f"u:{usuario_norm}", f"ip:{ip}")
            # Mismo mensaje para "no existe", "contraseña mala" y "cuenta desactivada":
            # distinguirlos deja averiguar qué cuentas hay a fuerza de probar.
            _auditar("login_fallido", request, usuario=usuario_norm)
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

        freno.registrar_exito(f"u:{usuario_norm}", f"ip:{ip}")
        token, _expira = sesiones.crear(
            cfg, encontrado, ip=ip, agente=request.headers.get("user-agent", "")
        )
        usuarios.marcar_acceso(cfg, encontrado.id)
        _poner_cookie(respuesta, token, cfg)
        _auditar("login_ok", request, usuario=encontrado.usuario, rol=encontrado.rol)
        return Identidad(
            usuario=encontrado.usuario,
            nombre=encontrado.nombre,
            rol=encontrado.rol,
            debe_cambiar=encontrado.debe_cambiar,
        )

    @aplicacion.post("/auth/logout")
    def post_logout(request: Request, respuesta: Response) -> dict[str, Any]:
        """Cierra la sesión en el servidor y borra la cookie. Sin autenticar a
        propósito: cerrar sesión con una cookie ya vencida debe funcionar igual.

        La cookie se borra PRIMERO y la revocación va en su propio try. Al revés, un
        Postgres que no responde hacía subir la excepción antes del `delete_cookie`:
        el front pinta el login igualmente (su `salir()` limpia en un `finally`), el
        usuario se va convencido de haber salido, y la cookie sigue viva en el
        navegador para quien use esa máquina después.
        """
        token = request.cookies.get(sesiones.COOKIE)
        respuesta.delete_cookie(sesiones.COOKIE, path="/")
        try:
            cerrada = sesiones.revocar(request.app.state.config, token)
        except Exception as exc:
            log.warning("logout_sin_revocar", error=str(exc)[:150])
            cerrada = False
        return {"cerrada": cerrada}

    @aplicacion.get("/auth/yo", response_model=Identidad)
    def get_yo(quien: Identificado) -> Identidad:
        """Con qué identidad estoy entrando. El front lo llama al cargar para decidir
        entre pintar el login o el panel."""
        return Identidad(
            usuario=quien.usuario,
            nombre=quien.nombre,
            rol=quien.rol,
            debe_cambiar=quien.debe_cambiar,
        )

    @aplicacion.post("/auth/contrasena")
    def post_contrasena(
        solicitud: SolicitudCambioContrasena, quien: Identificado, request: Request
    ) -> dict[str, Any]:
        """Cambio de la propia contraseña."""
        cfg: Config = request.app.state.config
        if quien.usuario_id is None:
            raise HTTPException(
                status_code=400, detail="Solo una sesión de usuario puede cambiar contraseña"
            )
        if usuarios.verificar_credenciales(cfg, quien.usuario, solicitud.actual) is None:
            raise HTTPException(status_code=403, detail="La contraseña actual no es correcta")
        try:
            usuarios.cambiar_contrasena(cfg, quien.usuario_id, solicitud.nueva)
        except pwd.ContrasenaDebil as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Cambiar la contraseña echa al resto de sesiones: si se cambia porque se
        # sospecha que alguien entró, dejarle la sesión abierta no arregla nada.
        _token, sesion = _sesion_actual(request)
        sesiones.revocar_todas(
            cfg, quien.usuario_id, excepto=sesion.sesion_id if sesion else None
        )
        _auditar("contrasena_cambiada", request, usuario=quien.usuario)
        return {"cambiada": True}

    @aplicacion.get("/auth/sesiones", response_model=list[Sesion])
    def get_sesiones(quien: Identificado, request: Request) -> list[dict[str, Any]]:
        if quien.usuario_id is None:
            return []
        return sesiones.listar(request.app.state.config, quien.usuario_id)

    @aplicacion.delete("/auth/sesiones")
    def delete_sesiones(quien: Identificado, request: Request) -> dict[str, Any]:
        """Cierra las demás sesiones, conservando la actual."""
        if quien.usuario_id is None:
            return {"cerradas": 0}
        _token, sesion = _sesion_actual(request)
        n = sesiones.revocar_todas(
            request.app.state.config,
            quien.usuario_id,
            excepto=sesion.sesion_id if sesion else None,
        )
        return {"cerradas": n}

    # ---------------- Usuarios (solo admin) ----------------

    @aplicacion.get("/auth/usuarios", response_model=list[UsuarioPanel])
    def get_usuarios(_: Admin, request: Request) -> list[dict[str, Any]]:
        return usuarios.listar(request.app.state.config)

    @aplicacion.post("/auth/usuarios", response_model=UsuarioPanel)
    def post_usuario(
        solicitud: SolicitudUsuarioNuevo, quien: Admin, request: Request
    ) -> dict[str, Any]:
        """Alta por un admin. Nace con `debe_cambiar`: la contraseña inicial la sabe
        quien la creó, así que no sirve como secreto hasta que el dueño la cambie."""
        cfg: Config = request.app.state.config
        try:
            creado = usuarios.crear(
                cfg,
                solicitud.usuario,
                solicitud.contrasena,
                rol=solicitud.rol,  # type: ignore[arg-type]
                nombre=solicitud.nombre,
                debe_cambiar=True,
            )
        except usuarios.UsuarioExiste as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (pwd.ContrasenaDebil, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _auditar(
            "usuario_creado", request,
            actor=quien.usuario, nuevo=creado.usuario, rol=creado.rol,
        )
        return {
            "id": creado.id, "usuario": creado.usuario, "nombre": creado.nombre,
            "rol": creado.rol, "activo": creado.activo, "debe_cambiar": creado.debe_cambiar,
            "creado_en": None, "ultimo_acceso": None,
        }

    @aplicacion.put("/auth/usuarios/{usuario_id}", response_model=UsuarioPanel)
    def put_usuario(
        usuario_id: int, solicitud: SolicitudUsuarioCambio, quien: Admin, request: Request
    ) -> dict[str, Any]:
        cfg: Config = request.app.state.config
        objetivo = usuarios.por_id(cfg, usuario_id)
        if objetivo is None:
            raise HTTPException(status_code=404, detail="usuario no encontrado")

        # La contraseña se valida ANTES de tocar nada. Al revés, el rol se commiteaba
        # y solo después saltaba el 400 por contraseña débil: el llamador recibía un
        # error creyendo que no se aplicó nada, y el cambio de rol ya estaba hecho.
        if solicitud.contrasena is not None:
            try:
                pwd.exigir_politica(solicitud.contrasena)
            except pwd.ContrasenaDebil as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        # No dejar el panel sin administrador. La comprobación y el UPDATE van en la
        # MISMA transacción con la fila bloqueada: separados eran un TOCTOU — dos PUT
        # concurrentes leían "queda otro admin", los dos pasaban el guard y el panel
        # se quedaba sin ninguno. De ahí solo se sale entrando a la BD a mano.
        pierde_admin = (solicitud.rol is not None and solicitud.rol != "admin") or (
            solicitud.activo is False
        )
        try:
            actualizado = usuarios.actualizar_protegiendo_admins(
                cfg, usuario_id,
                rol=solicitud.rol, nombre=solicitud.nombre, activo=solicitud.activo,
                exigir_otro_admin=pierde_admin,
            )
            if solicitud.contrasena is not None:
                usuarios.cambiar_contrasena(
                    cfg, usuario_id, solicitud.contrasena, debe_cambiar=True
                )
        except usuarios.UltimoAdmin as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (pwd.ContrasenaDebil, usuarios.RolInvalido, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _auditar(
            "usuario_modificado", request, actor=quien.usuario, objetivo=objetivo.usuario,
            rol=solicitud.rol, activo=solicitud.activo,
            contrasena_reseteada=solicitud.contrasena is not None,
        )

        # Desactivar o resetear la contraseña tiene que echarlo YA de donde esté.
        if solicitud.activo is False or solicitud.contrasena is not None:
            sesiones.revocar_todas(cfg, usuario_id)

        assert actualizado is not None
        return {
            "id": actualizado.id, "usuario": actualizado.usuario, "nombre": actualizado.nombre,
            "rol": actualizado.rol, "activo": actualizado.activo,
            "debe_cambiar": actualizado.debe_cambiar or solicitud.contrasena is not None,
            "creado_en": None, "ultimo_acceso": None,
        }

    @aplicacion.post("/buscar", response_model=RespuestaBusqueda)
    def post_buscar(
        solicitud: SolicitudBusqueda, _: Autorizado, request: Request
    ) -> RespuestaBusqueda:
        """Búsqueda con filtros, facetas y paginación profunda (pasa `cursor` de vuelta)."""
        return busqueda.buscar(_cliente(request), request.app.state.config, solicitud)

    @aplicacion.get("/seguridad/claves-busqueda", response_model=list[ClaveBusqueda])
    def get_claves_busqueda(_: Admin, request: Request) -> list[dict[str, Any]]:
        """Claves de búsqueda con nombre (solo nombre y fecha; nunca el secreto)."""
        return claves_busqueda.listar_claves(request.app.state.config)

    @aplicacion.post("/seguridad/claves-busqueda", response_model=RespuestaClaveGenerada)
    def post_clave_busqueda(
        solicitud: SolicitudClaveBusqueda, quien: Admin, request: Request
    ) -> RespuestaClaveGenerada:
        """Genera (o rota) la clave de un consumidor. Devuelve el secreto UNA sola vez;
        el servidor solo guarda su hash. Al crear la primera, el endpoint queda cerrado."""
        try:
            clave = claves_busqueda.generar_clave(request.app.state.config, solicitud.nombre)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _auditar(
            "clave_generada", request,
            actor=quien.usuario, consumidor=solicitud.nombre.strip(),
        )
        return RespuestaClaveGenerada(nombre=solicitud.nombre.strip(), clave=clave)

    @aplicacion.delete("/seguridad/claves-busqueda/{nombre}")
    def delete_clave_busqueda(nombre: str, quien: Admin, request: Request) -> dict[str, Any]:
        """Revoca la clave de un consumidor (deja de poder consultar; los demás siguen)."""
        if not claves_busqueda.revocar_clave(request.app.state.config, nombre):
            raise HTTPException(status_code=404, detail="clave no encontrada")
        _auditar("clave_revocada", request, actor=quien.usuario, consumidor=nombre)
        return {"revocada": True, "nombre": nombre}

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
        # ⚙K16: el nodo que REPLICA sus blobs al archivo maestro necesita un almacén
        # único y direccionable. Con el selector, cada corrida puede dejar el almacén
        # en una carpeta distinta (`config_con_destino` conmuta a backend `local`),
        # y entonces no hay "el bucket" que replicar.
        if not topologia.destino_eligible:
            return False
        # En Docker confinado SIN volumen de destino, elegir carpeta escribiría
        # dentro del contenedor (efímero) → no se ofrece. Dev nativo: siempre.
        return cfg.api_carpeta_destino_raiz is not None or cfg.api_carpeta_raiz is None

    def _exigir(capacidad: bool, mensaje: str) -> None:
        """409 explícito cuando este nodo no tiene la capacidad. Nunca un resultado
        inventado: un nodo que no resuelve entidades no debe devolver 'ninguna'."""
        if not capacidad:
            raise HTTPException(status_code=409, detail=mensaje)

    _SIN_ENTIDADES = (
        "este nodo no resuelve entidades (⚙K16: la resolución vive en un solo nodo"
        " para que dos resolvedores no se pisen en el AEB)"
    )
    _SIN_INGESTA = "este nodo no ingiere archivos (⚙K16)"

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
        solicitud: SolicitudCarpetaNueva, _: Operador, request: Request
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
        solicitud: SolicitudPipeline, _: Operador, request: Request
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

        _exigir(topologia.corre_ingesta, _SIN_INGESTA)
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

    @aplicacion.get("/sistema/topologia")
    def get_topologia(_: Autorizado, request: Request) -> dict[str, Any]:
        """⚙K16 — qué ES este nodo y qué sabe hacer.

        El front lo usa para OCULTAR lo que este nodo no tiene (entidades, discos)
        en vez de mostrarlo vacío: una sección vacía se lee como "no hay datos",
        y eso sería mentira — lo que pasa es que esa capacidad vive en otro nodo."""
        cfg: Config = request.app.state.config
        return {
            "perfil": cfg.despliegue.perfil,
            "nodo_id": cfg.despliegue.nodo_id,
            "capacidades": {
                "ingesta": topologia.corre_ingesta,
                "entidades": topologia.corre_entidades,
                "publico": topologia.sirve_publico,
                "archivo_maestro": topologia.es_archivo_maestro,
                "destino_eligible": topologia.destino_eligible,
            },
        }

    @aplicacion.get("/sistema/recursos")
    def get_recursos(_: Autorizado, request: Request) -> dict[str, Any]:
        """Estado del gobernador (K15): política activa, RAM libre, presión y cuántos
        workers sugiere AHORA. Da visibilidad de 'cuándo el sistema puede o no'."""
        from normalizacion.core import recursos

        return recursos.estado(request.app.state.config)

    @aplicacion.put("/sistema/recursos")
    def put_recursos(
        solicitud: SolicitudRecursos, _: Admin, request: Request
    ) -> dict[str, Any]:
        """Cambia la política de recursos (modo/política) sin reiniciar. Persiste el
        override y lo aplica EN VIVO al config compartido (daemons incluidos)."""
        import psycopg

        from normalizacion.core import recursos
        from normalizacion.core.config_overrides import (
            SECCION_RECURSOS,
            guardar_overrides,
            leer_overrides,
            recursos_efectivo,
        )

        cfg: Config = request.app.state.config
        cambios = solicitud.model_dump(exclude_none=True)
        try:
            with psycopg.connect(cfg.postgres_dsn) as conn:
                guardar_overrides(conn, cambios, SECCION_RECURSOS)
                conn.commit()
                overrides = leer_overrides(conn, SECCION_RECURSOS)
            # Aplica EN VIVO: el config raíz es el mismo objeto que comparten los
            # daemons (envío), así que reasignar .recursos los actualiza sin reiniciar.
            cfg.recursos = recursos_efectivo(cfg, overrides)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return recursos.estado(cfg)

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
        solicitud: SolicitudReprocesar, _: Operador, request: Request
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
    def post_rescore_frio(_: Operador, request: Request) -> dict[str, int]:
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
    def post_reexplorar_preservados(_: Operador, request: Request) -> dict[str, int]:
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
        clave: str, solicitud: SolicitudReceta, _: Admin, request: Request
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
    def delete_receta(clave: str, _: Admin, request: Request) -> dict[str, bool]:
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

    @aplicacion.get("/entidades/config/atributos")
    def get_config_atributos(_: Autorizado, request: Request) -> list[dict[str, str]]:
        """Atributos EXTRA que la entidad captura además del núcleo fijo (color_favorito…).
        Lo declarado aquí se guarda en `campos.atributos`; lo no declarado se descarta."""
        from normalizacion.entidades.config_entidad import leer_atributos

        return leer_atributos(request.app.state.config)

    @aplicacion.get("/entidades/config/nucleo")
    def get_config_nucleo(_: Autorizado, request: Request) -> dict[str, Any]:
        """El esquema FIJO de la persona: campos del núcleo (de la receta) + los
        derivados de la CURP. Solo lectura — los EXTRA editables van por /config/atributos."""
        from normalizacion.entidades.receta import obtener_receta

        r = obtener_receta("persona")
        campos = [
            {"nombre": c.nombre, "normalizador": c.normalizador, "ancla": c.es_ancla}
            for c in r.campos
        ]
        derivados = [
            "nombre_completo", "edad", "normalized_dob (de CURP)",
            "normalized_sex (de CURP)", "normalized_estado (de CURP)", "normalized_mpio",
        ]
        return {"campos": campos, "derivados": derivados}

    @aplicacion.put("/entidades/config/atributos")
    def put_config_atributos(
        solicitud: SolicitudAtributos, _: Admin, request: Request
    ) -> list[dict[str, str]]:
        """Reemplaza la lista de atributos declarados (valida nombres/normalizadores)."""
        from normalizacion.entidades.config_entidad import guardar_atributos

        try:
            return guardar_atributos(
                request.app.state.config, [a.model_dump() for a in solicitud.atributos]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @aplicacion.get("/entidades/config/destino")
    def get_config_destino(_: Admin, request: Request) -> dict[str, Any]:
        """Config del destino de envío de entidades al backend central (AEB)."""
        from normalizacion.entidades.destino import leer_destino

        return leer_destino(request.app.state.config)

    @aplicacion.put("/entidades/config/destino")
    def put_config_destino(
        solicitud: SolicitudDestino, _: Admin, request: Request
    ) -> dict[str, Any]:
        """Configura a qué endpoint/webhook se mandan las entidades (cuando esté hosteado)."""
        from normalizacion.entidades.destino import guardar_destino

        try:
            return guardar_destino(request.app.state.config, solicitud.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @aplicacion.post("/entidades/enviar")
    def post_entidades_enviar(
        _: Operador, request: Request, max_lotes: int = 20, reiniciar: bool = False
    ) -> dict[str, Any]:
        """Empuja al AEB las entidades nuevas/modificadas (formato canónico), en lotes.
        Reanudable por cursor; `reiniciar=true` reenvía todo desde cero."""
        _exigir(topologia.corre_entidades, _SIN_ENTIDADES)
        from normalizacion.entidades.envio import enviar_a_destino

        r = enviar_a_destino(
            request.app.state.config, max_lotes=max(1, max_lotes), reiniciar=reiniciar
        )
        return r.como_dict()

    @aplicacion.get("/entidades/enviar/estado")
    def get_entidades_enviar_estado(_: Autorizado, request: Request) -> dict[str, Any]:
        """Estado del envío al AEB: habilitado, cursor y cuántas entidades faltan por enviar."""
        from normalizacion.entidades.envio import estado_envio

        return estado_envio(request.app.state.config)

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
        entidad_id: str, _: Operador, request: Request, activo: bool = True
    ) -> dict[str, bool]:
        """Contingencia LFPDPPP: desactiva (soft-delete) o reactiva una entidad."""
        from normalizacion.entidades.consultas import fijar_activo

        if not fijar_activo(request.app.state.config, entidad_id, activo):
            raise HTTPException(status_code=404, detail="entidad no encontrada")
        return {"activo": activo}

    @aplicacion.post("/entidades/mapeo/proponer")
    def post_proponer_mapeo(
        solicitud: SolicitudProponerMapeo, _: Operador, request: Request
    ) -> dict[str, Any]:
        """E2: propone columna→campo (sinónimos + contenido) para que el operador
        confirme. No persiste — eso es el paso de aprobación."""
        from normalizacion.entidades import mapeo
        from normalizacion.entidades.config_entidad import leer_atributos
        from normalizacion.entidades.receta import obtener_receta

        try:
            receta = obtener_receta(solicitud.tipo)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        declarados = leer_atributos(request.app.state.config)
        prop = mapeo.proponer_mapeo(receta, solicitud.columnas, solicitud.muestras, declarados)
        return {"huella": mapeo.huella_columnas(solicitud.columnas), "propuestas": prop}

    @aplicacion.post("/entidades/proyectar")
    def post_proyectar(
        solicitud: SolicitudProyectar, _: Operador, request: Request
    ) -> dict[str, int]:
        """E3: proyecta filas ya mapeadas a entidades (resolución por ancla,
        idempotente). Útil para CLI/integraciones; la proyección desde los blobs
        indexados es el camino a escala (E4)."""
        _exigir(topologia.corre_entidades, _SIN_ENTIDADES)
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
        _: Operador, request: Request, lote: int = 500, max_docs: int = 2000,
        reiniciar: bool = False,
    ) -> dict[str, Any]:
        """E4 (1er paso): resuelve entidades de los registros YA INDEXADOS que traen
        CURP/RFC. Se lanza en SEGUNDO PLANO (gobernado por K15) y vuelve de inmediato
        —correrlo dentro de la petición engordaba la API y tumbaba el panel—. El
        avance se consulta en GET /entidades/backfill/estado. Reanudable por cursor."""
        _exigir(topologia.corre_entidades, _SIN_ENTIDADES)
        from normalizacion.entidades.backfill import lanzar_en_fondo

        return lanzar_en_fondo(
            request.app.state.config, lote=lote, max_docs=max_docs, reiniciar=reiniciar,
        )

    @aplicacion.get("/entidades/backfill/estado")
    def get_backfill_estado(_: Autorizado, request: Request) -> dict[str, Any]:
        """Avance del backfill en curso (o el último resumen): para que la UI muestre
        progreso sin bloquear."""
        from normalizacion.entidades.backfill import estado_backfill

        return estado_backfill(request.app.state.config)

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
        solicitud: SolicitudFiltro, _: Operador, request: Request
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
    def delete_filtro(_: Operador, request: Request) -> RespuestaFiltro:
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

        # `connect_timeout` por la misma razón que en `aplicar_recursos`: sin él, un
        # Postgres que no responde deja la API sin arrancar en vez de arrancar sin
        # recetas sembradas. El `suppress` no ayuda si la conexión nunca vuelve.
        with (
            contextlib.suppress(Exception),  # la tabla puede no existir aún (migración)
            psycopg.connect(config.postgres_dsn, connect_timeout=5) as conn,
        ):
            seed_recetas(conn)

    return aplicacion


# Entrypoint para uvicorn (`norm api`): la config sale del entorno NORM_*
app = crear_app(cargar_config())
