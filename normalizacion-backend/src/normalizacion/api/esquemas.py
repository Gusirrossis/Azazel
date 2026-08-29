"""Contrato de la API (Pydantic → OpenAPI): lo ÚNICO que el front puede pedir.

Seguridad por diseño: el cliente jamás manda DSL — solo estos campos tipados;
el servidor construye la consulta (allowlist implícita, PROPUESTA §9).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SolicitudBusqueda(BaseModel):
    """POST /buscar — texto + filtros + paginación profunda (search_after + PIT)."""

    # Seguridad: campos desconocidos se RECHAZAN (no hay forma de colar DSL)
    model_config = ConfigDict(extra="forbid")

    texto: str | None = Field(
        default=None, max_length=200, description="Búsqueda parcial por nombre (wildcard)"
    )
    tipo_real: str | None = Field(default=None, max_length=120)
    extension: str | None = Field(default=None, max_length=20)
    disco_id: str | None = Field(default=None, max_length=120)
    puntaje_min: int | None = Field(default=None, ge=0, le=100)
    tamano_min: int | None = Field(default=None, ge=0)
    tamano_max: int | None = Field(default=None, ge=0)
    facetas: bool = Field(default=False, description="Incluir conteos por tipo/extensión/disco")
    tamano_pagina: int = Field(default=20, ge=1, description="Se acota al máximo del servidor")
    cursor: list[Any] | None = Field(
        default=None, description="search_after de la página anterior (paginación profunda)"
    )
    abrir_pit: bool = Field(
        default=False, description="Abrir un Point-In-Time (vista estable para paginar)"
    )
    pit_id: str | None = Field(default=None, description="PIT de una búsqueda anterior")


class RespuestaBusqueda(BaseModel):
    total: int
    documentos: list[dict[str, Any]]
    cursor: list[Any] | None = None  # pasa esto como `cursor` para la siguiente página
    facetas: dict[str, dict[str, int]] | None = None
    pit_id: str | None = None


class SolicitudLogin(BaseModel):
    """Credenciales del panel. `extra="forbid"` para que un campo de más sea un 422
    ruidoso y no algo que el servidor ignora en silencio."""

    model_config = ConfigDict(extra="forbid")
    usuario: str = Field(min_length=1, max_length=60)
    contrasena: str = Field(min_length=1, max_length=200)


class Identidad(BaseModel):
    """Quién está usando el panel. Es lo que el front pinta en la cabecera y lo que
    usa para decidir qué pestañas mostrar."""

    usuario: str
    nombre: str
    rol: str
    # True tras un alta o un reseteo: el front obliga a cambiarla antes de seguir.
    debe_cambiar: bool = False


class SolicitudCambioContrasena(BaseModel):
    """Cambio de la propia contraseña. Exige la actual: con la sesión ya abierta,
    sin este paso a cualquiera que encuentre la pantalla desbloqueada le basta un
    clic para quedarse con la cuenta."""

    model_config = ConfigDict(extra="forbid")
    actual: str = Field(min_length=1, max_length=200)
    nueva: str = Field(min_length=1, max_length=200)


class Sesion(BaseModel):
    """Una sesión abierta, para que el usuario reconozca las suyas."""

    id: int
    creada_en: str | None = None
    vista_en: str | None = None
    expira_en: str | None = None
    ip: str = ""
    agente: str = ""


class SolicitudUsuarioNuevo(BaseModel):
    """Alta de usuario por un admin."""

    model_config = ConfigDict(extra="forbid")
    usuario: str = Field(min_length=1, max_length=60)
    contrasena: str = Field(min_length=1, max_length=200)
    rol: str = Field(default="lector", pattern="^(lector|operador|admin)$")
    nombre: str = Field(default="", max_length=120)


class SolicitudUsuarioCambio(BaseModel):
    """Cambios parciales sobre un usuario. Todo opcional: se aplica lo que venga."""

    model_config = ConfigDict(extra="forbid")
    rol: str | None = Field(default=None, pattern="^(lector|operador|admin)$")
    nombre: str | None = Field(default=None, max_length=120)
    activo: bool | None = None
    # Reseteo por un admin: deja la cuenta con `debe_cambiar`, así el dueño la
    # cambia al entrar y el admin no se queda sabiendo la contraseña ajena.
    contrasena: str | None = Field(default=None, max_length=200)


class UsuarioPanel(BaseModel):
    """Usuario tal como lo ve el panel de administración. Nunca lleva el hash."""

    id: int
    usuario: str
    nombre: str
    rol: str
    activo: bool
    debe_cambiar: bool
    creado_en: str | None = None
    ultimo_acceso: str | None = None


class SolicitudClaveBusqueda(BaseModel):
    """Alta de una clave de búsqueda CON NOMBRE (una por consumidor, revocable aparte)."""

    model_config = ConfigDict(extra="forbid")
    nombre: str = Field(min_length=1, max_length=60, description="Consumidor dueño de la clave")


class ClaveBusqueda(BaseModel):
    """Metadatos de una clave (nunca el secreto ni el hash)."""

    nombre: str
    creada_en: str | None = None


class RespuestaClaveGenerada(BaseModel):
    """El secreto en claro — se muestra UNA sola vez; luego solo queda su hash."""

    nombre: str
    clave: str


class RespuestaAutocompletar(BaseModel):
    sugerencias: list[str]


class Estadisticas(BaseModel):
    total_documentos: int
    bytes_totales: int
    por_tipo: dict[str, int]
    por_disco: dict[str, int]


class GrupoResumen(BaseModel):
    """Un corte del panel: cuántos archivos y cuántos bytes caen en `clave`."""

    clave: str
    archivos: int
    bytes: int


class ResumenPanel(BaseModel):
    """GET /resumen — agregados de la cola (Postgres), no del índice.

    A diferencia de /estadisticas (solo lo INDEXADO en OpenSearch), aquí se ve TODO
    lo catalogado: el frío incluido. Sirve para saber cuántos datos se dejan de lado.
    """

    total_archivos: int
    bytes_totales: int
    por_estado: list[GrupoResumen]  # PENDIENTE, PRECALIFICADO, COLD, EN_PROCESO, INDEXADO, ERROR
    por_decision: list[GrupoResumen]  # HOT, COLD, SIN_DECIDIR (aún sin precalificar)
    por_tipo: list[GrupoResumen]  # tipos reales con más peso (los ya tipificados)
    generado_en: str


# ------------------------------------------------------------------ pipeline


class SolicitudPipeline(BaseModel):
    """POST /pipeline/ejecutar — indexar una carpeta del sistema (del servidor)."""

    model_config = ConfigDict(extra="forbid")

    ruta: str = Field(max_length=1000, description="Carpeta a procesar (ruta del servidor)")
    disco_id: str | None = Field(default=None, max_length=120)
    # Carpeta de DESTINO elegida en el front (almacén HOT + frío bajo ella).
    # None = destino configurado en .env (MinIO o carpetas por defecto).
    destino: str | None = Field(default=None, max_length=1000)
    # Nº de PROCESOS worker en paralelo. None = automático (núcleos - 2).
    workers: int | None = Field(default=None, ge=1, le=64)


class SolicitudCarpetaNueva(BaseModel):
    """POST /sistema/carpetas — crear una subcarpeta de destino desde el front."""

    model_config = ConfigDict(extra="forbid")

    ruta: str = Field(max_length=1000, description="Carpeta padre (dentro de la raíz de destino)")
    nombre: str = Field(min_length=1, max_length=120)


class FaseEjecutada(BaseModel):
    fase: str
    duracion_s: float
    metricas: dict[str, Any]
    archivos_por_segundo: float | None = None


class Corrida(BaseModel):
    id: int
    disco_id: str
    ruta: str
    estado: str  # EN_CURSO | COMPLETADA | FALLIDA
    fase_actual: str | None
    fases: list[FaseEjecutada]
    seguro_para_desechar: bool | None
    error: str | None
    iniciada_en: Any
    terminada_en: Any | None
    destino: str | None = None  # carpeta elegida en el front; None = destino del .env


class EstadoPipeline(BaseModel):
    en_curso: Corrida | None
    historial: list[Corrida]
    destinos: dict[str, str]
    # Avance EN VIVO de la corrida actual: conteos de la cola por estado
    progreso: dict[str, int] | None = None
    # ¿El front puede ofrecer "elegir carpeta de destino"? (raíz escribible montada)
    destino_eligible: bool = False
    # Workers que usaría el modo automático (núcleos - 2) — el front lo muestra
    workers_auto: int = 1


class RespuestaCarpetas(BaseModel):
    ruta: str
    padre: str | None
    carpetas: list[str]


# --------------------------------------------------------- entidades (Fase 2)


class Entidad(BaseModel):
    """Persona canónica resuelta (esquema Fz1 en `campos`)."""

    entidad_id: str
    tipo: str
    ancla_tipo: str
    ancla_valor: str
    campos: dict[str, Any]
    confianza: float
    version_receta: str
    version_resolucion: str
    activo: bool
    procedencias: list[dict[str, Any]]
    creado_en: Any
    actualizado_en: Any


class RespuestaEntidades(BaseModel):
    total: int
    entidades: list[Entidad]
    cursor: str | None


class EstadisticasEntidades(BaseModel):
    total: int
    con_curp: int
    por_ancla: dict[str, int]


class SolicitudProponerMapeo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: str = "persona"
    columnas: list[str]
    muestras: dict[str, list[str]] | None = None


class SolicitudProyectar(BaseModel):
    """Proyecta filas ya mapeadas a entidades (resolución por ancla)."""

    model_config = ConfigDict(extra="forbid")

    tipo: str = "persona"
    asignacion: dict[str, str]  # {columna: campo}
    filas: list[dict[str, Any]]


class SolicitudReceta(BaseModel):
    """Crear/editar una receta de PROYECCIÓN (esquema de salida por sistema)."""

    model_config = ConfigDict(extra="forbid")

    clave: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    nombre: str = Field(min_length=1, max_length=120)
    descripcion: str = ""
    definicion: dict[str, Any]  # {passthrough:true} | {salida:[{path,de|constante,mapa?}]}
    version: str = "v1"
    tipo: str = "persona"
    clase: str = "proyeccion"


class AtributoDeclarado(BaseModel):
    """Un atributo EXTRA que la entidad captura además del núcleo fijo."""

    model_config = ConfigDict(extra="forbid")

    nombre: str = Field(min_length=2, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    normalizador: Literal["texto", "curp", "rfc", "email", "telefono", "nombre"] = "texto"


class SolicitudAtributos(BaseModel):
    """La lista completa de atributos declarados (reemplaza la anterior)."""

    model_config = ConfigDict(extra="forbid")

    atributos: list[AtributoDeclarado] = Field(default_factory=list, max_length=100)


class SolicitudDestino(BaseModel):
    """Destino de envío al orquestador (AEB): a dónde y cada cuánto. Azazel manda el canónico
    completo; el orquestador proyecta por formato. Por eso aquí no hay receta ni modo."""

    model_config = ConfigDict(extra="forbid")

    habilitado: bool = False
    url: str = Field(default="", max_length=500)
    auth_token: str = Field(default="", max_length=500)
    lote: int = Field(default=500, ge=1, le=5000)
    intervalo_seg: int = Field(default=0, ge=0, le=86400)  # 0 = solo manual; >0 = envío automático


class SolicitudRecursos(BaseModel):
    """PUT /sistema/recursos — política del gobernador K15 (sin reiniciar).

    Todo opcional: se manda solo lo que cambia. `modo` adaptativo dimensiona por RAM
    libre en tiempo real; `politica` decide cuánta RAM se reserva para el SO/otros."""

    model_config = ConfigDict(extra="forbid")

    modo: Literal["adaptativo", "fijo"] | None = None
    politica: Literal["conservador", "balanceado", "maximo"] | None = None
    reserva_ram_pct: float | None = Field(default=None, ge=0.05, le=0.9)
    mem_por_worker_mb: int | None = Field(default=None, ge=128)
    workers_max: int | None = Field(default=None, ge=0, le=64)


# ------------------------------------------------------------------- tablero


class TotalesTablero(BaseModel):
    archivos: int
    bytes: int
    hechos: int
    errores: int
    cold: int
    en_proceso: int  # EN_PROCESO + INDEXADO + VERIFICADO
    pendientes: int  # PENDIENTE + PRECALIFICADO
    franja_gris: int  # puntaje entre umbrales: donde el filtro duda
    con_hash: int  # filas con blob en el almacén
    hash_unicos: int  # blobs únicos (la diferencia = ahorro del dedup)


class BucketPuntaje(BaseModel):
    desde: int  # cubeta de 10: 0, 10, … 90
    archivos: int


class DiscoTablero(BaseModel):
    disco_id: str
    archivos: int
    bytes: int
    hechos: int
    errores: int


class CorridaMini(BaseModel):
    id: int
    ruta: str
    estado: str
    iniciada_en: Any
    terminada_en: Any | None
    duracion_s: float | None


class RespuestaTablero(BaseModel):
    """GET /panel — todos los agregados del tablero de Inicio en una llamada."""

    totales: TotalesTablero
    por_estado: list[GrupoResumen]
    por_decision: list[GrupoResumen]
    por_tipo: list[GrupoResumen]
    causas_cold: list[GrupoResumen]
    causas_error: list[GrupoResumen]
    histograma_puntaje: list[BucketPuntaje]
    umbral_cold: int  # umbrales EFECTIVOS (base + overrides de la UI)
    umbral_hot: int
    discos: list[DiscoTablero]
    corridas: list[CorridaMini]
    generado_en: str


# ------------------------------------------------------------ filtro editable


class FiltroVisible(BaseModel):
    """El filtro EFECTIVO que usará la siguiente corrida (base + overrides)."""

    modo_lista: str
    tipos_interes: list[str]
    tipos_interes_prefijos: list[str]
    tipos_excluidos: list[str]
    entropia_texto_max: float
    entropia_comprimido_min: float
    ratio_imprimibles_min: float
    umbral_hot: int
    umbral_cold: int
    prioridad_contenedores: int
    prioridad_extensiones: dict[str, int]
    version_filtro: str


class RespuestaFiltro(BaseModel):
    efectivo: FiltroVisible
    overrides: dict[str, Any]  # solo lo editado (vacío = config base intacta)
    hay_overrides: bool


class SolicitudFiltro(BaseModel):
    """PUT /filtro — editar perillas desde la UI. None = no tocar ese campo.

    Aplica a la SIGUIENTE corrida (la config de procesos vivos no cambia);
    `rescore-frio` re-evalúa lo ya enviado a frío con el filtro nuevo."""

    model_config = ConfigDict(extra="forbid")

    modo_lista: str | None = Field(default=None, pattern="^(blanca|negra)$")
    tipos_interes: list[str] | None = None
    tipos_interes_prefijos: list[str] | None = None
    tipos_excluidos: list[str] | None = None
    entropia_texto_max: float | None = Field(default=None, ge=0, le=8)
    entropia_comprimido_min: float | None = Field(default=None, ge=0, le=8)
    ratio_imprimibles_min: float | None = Field(default=None, ge=0, le=1)
    umbral_hot: int | None = Field(default=None, ge=1, le=100)
    umbral_cold: int | None = Field(default=None, ge=0, le=99)
    prioridad_contenedores: int | None = Field(default=None, ge=0, le=100)
    prioridad_extensiones: dict[str, int] | None = None
    # Si no se manda, el servidor deriva una versión auditada (+ov-<huella>)
    version_filtro: str | None = Field(default=None, max_length=120)


# --------------------------------------------------------- explorador de cola


class ArchivoCola(BaseModel):
    """Una fila del plano de control (Postgres) — incluye lo que el índice no ve:
    COLD, ERROR, pendientes, con sus señales (entropía) y motivos."""

    archivo_id: str
    disco_id: str
    ruta: str
    nombre: str
    extension: str | None
    tamano: int
    mtime: Any
    estado: str
    prioridad: int
    intentos: int
    error_motivo: str | None
    puntaje: int | None
    ruta_decision: str | None
    tipo_real: str | None
    senales: dict[str, Any] | None
    motivo: str | None
    version_filtro: str | None
    hash_contenido: str | None
    actualizado_en: Any


class ResumenCola(BaseModel):
    """Composición del subconjunto filtrado: POR QUÉ está ahí y de qué tipos es.
    `por_causa` agrupa por el prefijo del motivo (error_motivo manda en ERROR)."""

    por_causa: list[GrupoResumen]
    por_tipo: list[GrupoResumen]


class RespuestaColaArchivos(BaseModel):
    total: int
    archivos: list[ArchivoCola]
    cursor: str | None  # archivo_id de la última fila; None = no hay más páginas
    resumen: ResumenCola


class SolicitudReprocesar(BaseModel):
    """POST /cola/reprocesar-errores — devolver dead-letter a su etapa de origen.
    Las filas se procesan en la SIGUIENTE corrida (re-indexar la carpeta)."""

    model_config = ConfigDict(extra="forbid")

    # Patrón LIKE sobre error_motivo (p. ej. "agotado:%"). None = todos.
    motivo_como: str | None = Field(default=None, max_length=300)


class RespuestaReprocesar(BaseModel):
    total: int
    destinos: dict[str, int]  # a qué estado volvió cada cuántas filas


class ArchivoPreservado(BaseModel):
    """Un contenedor preservado SIN explorar (cifrado/corrupto/formato pendiente)."""

    disco_id: str
    ruta: str
    nombre: str
    tamano: int
    tipo_real: str | None
    motivo: str
    estado: str


class RespuestaPreservados(BaseModel):
    total: int
    por_motivo: dict[str, int]
    archivos: list[ArchivoPreservado]  # los más grandes primero (tope del servidor)
