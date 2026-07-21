"""Configuración tipada — el ÚNICO punto de verdad de las perillas de ajuste (K1-K14).

Ningún número mágico vive en el código: cambiar comportamiento del sistema = cambiar
config versionada aquí o vía variables de entorno (prefijo NORM_, anidado con __,
p. ej. NORM_FILTRO__UMBRAL_HOT=70).

La numeración K# corresponde al árbol de decisiones de PLAN_IMPLEMENTACION.html §10.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

GIB = 1024 * 1024 * 1024


class PerillasFiltro(BaseModel):
    """Perillas de la precalificación T0-T4 (Fase 1.5)."""

    # ⚙ K1 — kill-rules T0 (sin abrir el archivo)
    kill_nombres: frozenset[str] = frozenset({"thumbs.db", ".ds_store", "desktop.ini"})
    kill_extensiones: frozenset[str] = frozenset({".tmp", ".lnk", ".pyc", ".log~"})
    kill_rutas_patrones: tuple[str, ...] = (
        r"[/\\]cache[/\\]",
        r"[/\\]node_modules[/\\]",
        r"[/\\]\.git[/\\]",
        r"appdata[/\\].*[/\\]code cache[/\\]",
        r"[/\\]library[/\\]caches[/\\]",
    )

    # K2 - bytes leidos en T1 (libmagic pierde precision con menos de 2048)
    bytes_t1: int = Field(default=4096, ge=2048)

    # ⚙ K3 — alcance del negocio: QUÉ entra al embudo caro.
    # modo_lista="blanca" (default): SOLO los tipos de interés siguen; todo lo demás
    # va a frío (reversible — ampliar la lista + rescore-frio rescata lo excluido).
    # modo_lista="negra": el comportamiento anterior (bloquear tipos no objetivo).
    modo_lista: str = "blanca"

    # LISTA BLANCA — tipos de interés (decisión sobre el TIPO REAL, jamás la extensión):
    tipos_interes_prefijos: tuple[str, ...] = ("text/",)  # txt, csv, tsv, logs, vcf…
    # Excepciones a los prefijos (decisión del usuario 2026-06-10: el HTML rara vez
    # trae información orgánica — markup de páginas, no datos):
    tipos_excluidos: frozenset[str] = frozenset({"text/html"})
    tipos_interes: frozenset[str] = frozenset(
        {
            # Documentos con texto
            "application/pdf",
            "application/rtf",
            "application/x-ole-storage",  # Office legado (DOC/XLS/PPT/MSG)
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation",
            "application/epub+zip",
            "application/vnd.wordperfect",  # .wpd (gobierno/jurídico legado)
            "image/vnd.djvu",  # escaneos masivos (texto vía OCR/L2 futuro)
            "application/vnd.ms-xpsdocument",
            "application/x-iwork",  # Pages/Numbers/Keynote
            # Correos (propuesta aceptada 2026-06-10; .msg ya entra por x-ole-storage)
            "message/rfc822",  # .eml y mbox
            "application/vnd.ms-outlook-pst",  # .pst/.ost (buzones exportados)
            "application/x-dbx",  # Outlook Express
            "application/x-nsf",  # Lotus Notes
            # Datos estructurados
            "application/json",
            "application/x-ndjson",
            "application/xml",
            "application/sql",  # dumps SQL: texto con información tabular valiosa
            "application/vnd.sqlite3",  # .db SQLite
            "application/x-msaccess",  # .mdb/.accdb (Access)
            "application/vnd.apache.parquet",  # datos columnares
            "application/x-dbf",  # dBase/FoxPro: padrones y sistemas legados
            "application/x-pgdump",  # pg_dump formato custom
            "application/x-mssql-backup",  # .bak (MTF) de SQL Server
            "application/avro",
            "application/orc",
        }
    )
    # (los CONTENEDORES — zip/rar/7z — no van en esta lista: siempre se exploran o
    #  preservan; lo que decide es su contenido interno, pieza por pieza)

    # LISTA NEGRA (modo legado):
    tipos_no_objetivo_prefijos: tuple[str, ...] = ("image/", "video/", "audio/", "font/")
    tipos_no_objetivo: frozenset[str] = frozenset(
        {
            "application/x-dosexec",
            "application/x-executable",
            "application/x-sharedlib",
            "application/x-mach-binary",
            "application/octet-stream",
        }
    )

    # ⚙ OCR — interruptor único. Con False (default) las imágenes van a COLD como
    # siempre (comportamiento intacto). Con True, `image/*` se rutea a HOT y el
    # extractor de imágenes les saca texto por OCR (→ texto_indexable, buscable).
    ocr_activo: bool = False

    # PRIORIDAD a comprimidos (decisión del usuario: la mayoría de lo útil viene
    # dentro). Se procesan PRIMERO y sus entradas internas heredan la urgencia.
    prioridad_contenedores: int = Field(default=90, ge=0, le=100)
    prioridad_inicial_contenedores: int = Field(default=50, ge=0, le=100)
    extensiones_contenedor_hint: tuple[str, ...] = (
        ".zip",
        ".rar",
        ".7z",
        ".gz",
        ".tgz",
        ".tar",
        ".bz2",
        ".xz",
        ".txz",
        ".iso",
        ".vhd",
        ".vhdx",
        ".vmdk",
        ".qcow2",
        ".e01",
    )

    # ORDEN de procesamiento por EXTENSIÓN (decisión del usuario 2026-06-11:
    # .txt primero, luego .7z, .rar, .zip, después el resto). Solo ordena la cola,
    # JAMÁS decide el tipo ni la ruta HOT/COLD. Valores > 100 para ganarle a
    # prioridad=puntaje (0-100) y a prioridad_contenedores (90). Las filas que ya
    # estaban encoladas conservan su prioridad previa (el catálogo es incremental).
    prioridad_extensiones: dict[str, int] = Field(
        default_factory=lambda: {".txt": 140, ".7z": 130, ".rar": 120, ".zip": 110}
    )

    def prioridad_para_extension(self, extension: str | None) -> int:
        """Prioridad inicial al catalogar: perilla por extensión > hint de contenedor > 0."""
        ext = (extension or "").lower()
        if ext in self.prioridad_extensiones:
            return self.prioridad_extensiones[ext]
        if ext in self.extensiones_contenedor_hint:
            return self.prioridad_inicial_contenedores
        return 0

    # ⚙ K4 — guards anti zip-bomb de T3 (Tika: ratio; plaso: BFS con tope).
    # Decisión del usuario (2026-06-10): los 7z reales del servidor rondan 15 GB y
    # abren a 200 GB+ — deben explorarse POR COMPLETO aunque tarden. Estos topes son
    # de SEGURIDAD, no de capacidad: una violación va a COLD reversible (el archivo
    # se preserva íntegro en frío; subir la perilla + `norm rescore-frio` lo explora).
    t3_profundidad_max: int = 10
    t3_ratio_compresion_max: float = 300.0  # bombas reales ≈1000+; texto legítimo <150
    t3_descomprimido_max_bytes: int = 1024 * GIB  # 1 TiB
    t3_entradas_max: int = 1_000_000
    t3_timeout_s: float = 1800.0  # listar un 7z sólido de 15 GB puede tardar minutos

    # K5 - tamano del head en T2 (mas = mejores senales, mas I/O por miles de millones)
    head_t2_bytes: int = Field(default=65_536, ge=8_192)

    # ⚙ K6 — umbrales de señales de T2 (entropía: binwalk; imprimibles: Tika)
    entropia_texto_max: float = 3.5
    entropia_comprimido_min: float = 7.5
    ratio_imprimibles_min: float = 0.9
    lineas_consistencia_csv: int = 10

    # ⚙ K7 — pesos del puntaje (señal → puntos). CUALQUIER cambio = nueva version_filtro.
    version_filtro: str = "reglas-v3-lista-blanca"
    pesos: dict[str, int] = Field(
        default_factory=lambda: {
            "tabular": 35,
            "estructurado": 30,
            "documento": 30,
            "texto_legible": 20,
            "tamano_contextual": 10,
            "extension_coincide": 5,
            "comprimido_cifrado": -35,
            "ruido_binario": -25,
            "minusculo_para_dato": -10,
        }
    )
    puntaje_base: int = 35

    # Mientras no exista el T4 (ML, Fase 4): la franja gris va a HOT — calibrado a
    # recall (un falso positivo es ruido recuperable; un falso negativo retrasa).
    gris_sin_t4_a_hot: bool = True

    # ⚙ K8 — umbrales del router (acercarlos = menos franja gris = menos ML)
    umbral_hot: int = Field(default=65, ge=1, le=100)
    umbral_cold: int = Field(default=35, ge=0, le=99)

    # ⚙ K9 — Filtro 2 (ML): umbral por tipo + banda de revisión humana (active learning)
    ml_umbral_default: float = 0.5
    ml_umbral_por_tipo: dict[str, float] = Field(default_factory=dict)
    ml_banda_revision: float = 0.1
    ml_version_modelo: str = "sin-modelo"  # se versiona en Fase 4 (modelo_vN/)


class PerillasWorker(BaseModel):
    """Perillas del worker HOT y la cola (Fase 2)."""

    # Workers en PARALELO del pipeline (procesos reales — el GIL de Python no
    # limita): 0 = automático (núcleos - 2). El front lo puede fijar por corrida.
    procesos: int = Field(default=0, ge=0, le=64)

    # ⚙ K10 — claim y leases (plaso: abandono por inactividad)
    lote_claim: int = Field(default=500, ge=1)
    lease_segundos: int = Field(default=300, ge=10)
    lote_insercion: int = Field(default=1000, ge=1)  # lotes del walker hacia la cola

    # ⚙ K14 (lado worker) — fallos TRANSITORIOS (dependencia caída, I/O intermitente):
    # la fila se devuelve con backoff (el lease actúa como "no reclamar hasta");
    # agotado el tope → ERROR dead-letter con motivo "agotado:". Permanentes
    # (corrupto, hash no coincide) van a ERROR directo.
    intentos_max: int = Field(default=3, ge=1)
    backoff_transitorio_base_s: float = 5.0  # 5s, 10s, 20s…

    # ⚙ K11 — límites de extractores (fscrawler/Tika; al exceder → flag, no excepción)
    extractor_timeout_s: float = 300.0
    extractor_max_chars: int = 100_000
    extractor_max_embebidos: int = 10
    extractor_max_profundidad_xml: int = 100
    umbral_memoria_bytes: int = 65_536  # < 64 KB en RAM; mayor → archivo temporal

    # ⚙ OCR — parámetros del extractor de imágenes (solo se usan si filtro.ocr_activo).
    # El binario tesseract y sus idiomas se instalan aparte; si faltan, se degrada con
    # un flag `ocr_no_disponible` (nunca rompe). El downscale acota RAM/tiempo.
    ocr_idiomas: str = "spa+eng"        # códigos de idioma de tesseract (spa, eng, …)
    ocr_max_lado: int = 2600            # px: se reduce la imagen si el lado mayor lo supera
    ocr_min_lado: int = 64              # px: se saltan íconos/miniaturas (no valen OCR)

    # ⚙ K12 — perfil de calidad tabular (chequeos estilo great_expectations sobre polars)
    calidad_chequeos: tuple[str, ...] = (
        "filas_totales",
        "columnas_totales",
        "nulos_por_columna",
        "tipo_inferido_por_columna",
        "unicos_por_columna",
        "filas_malformadas",
    )
    calidad_max_bytes: int = 10 * 1024 * 1024  # tope de lectura para perfilar tabulares

    bloque_lectura_bytes: int = 1024 * 1024  # streaming: nunca el archivo entero en RAM


class PerillasRecursos(BaseModel):
    """⚙ K15 — gobernador de recursos (memoria). El sistema decide cuánto trabajar
    según la RAM LIBRE en TIEMPO REAL, no por un número fijo de núcleos.

    Razón de ser: la misma Mac puede tener OTRO sistema corriendo, y la resolución
    de entidades pesa al mismo tiempo que la ingesta. Un `núcleos − 2` estático
    ignora todo eso y satura la RAM (macOS mata al proceso → se cae el panel). En
    modo "adaptativo" (default) los workers y las entidades se dimensionan contra
    un PRESUPUESTO de memoria y se pausan solos cuando la RAM aprieta.
    """

    # "adaptativo": dimensiona y throttlea por RAM libre. "fijo": respeta núcleos/perilla
    # sin mirar la memoria (comportamiento anterior; para entornos con RAM dedicada).
    modo: str = "adaptativo"

    # Política → cuánta RAM se RESERVA siempre para el SO y otros programas:
    #   conservador 40 % · balanceado 30 % · maximo 20 %. (decisión del usuario
    #   2026-06-24: conservador por defecto, la Mac comparte con otro sistema).
    politica: str = "conservador"
    # Override explícito del % de reserva; si es None se deriva de `politica`.
    reserva_ram_pct: float | None = Field(default=None, ge=0.05, le=0.9)
    # Piso ABSOLUTO de RAM libre (MiB): nunca dejar al SO con menos que esto, sin
    # importar el %. Protege equipos chicos donde el % daría un número minúsculo.
    ram_minima_libre_mb: int = Field(default=1536, ge=256)

    # Working-set ESTIMADO por proceso worker (MiB): cubre el buffer del sink
    # (flush_bytes), el pico de extracción de un archivo y el intérprete. De aquí
    # sale cuántos workers caben en el presupuesto. Subir si se ven OOM; bajar si
    # sobra RAM y se quiere más paralelismo.
    mem_por_worker_mb: int = Field(default=700, ge=128)
    # Costo estimado de una pasada de resolución de entidades (backfill/envío) que
    # corre DENTRO de la API: si no cabe en el presupuesto, se pospone.
    mem_entidades_mb: int = Field(default=512, ge=64)

    # Tope explícito de workers (0 = sin tope: manda el presupuesto y los núcleos).
    workers_max: int = Field(default=0, ge=0, le=64)
    # Cada cuánto se vuelve a muestrear la memoria (s) — barato, pero no en bucle apretado.
    intervalo_muestreo_s: float = Field(default=2.0, ge=0.2)
    # Tope de espera cuando hay presión (s): pasado esto se sigue igual, para JAMÁS
    # colgar un lote por memoria (la presión se registra; el trabajo no se pierde).
    espera_max_presion_s: float = Field(default=120.0, ge=0.0)

    _RESERVA_POR_POLITICA = {"conservador": 0.40, "balanceado": 0.30, "maximo": 0.20}

    def fraccion_reserva(self) -> float:
        """% de RAM que se mantiene libre. Override explícito > política > conservador."""
        if self.reserva_ram_pct is not None:
            return self.reserva_ram_pct
        return self._RESERVA_POR_POLITICA.get(self.politica, 0.40)


class PerillasIndexador(BaseModel):
    """Perillas del sink a OpenSearch (Fase 2)."""

    # ⚙ K13 — triple trigger de flush (patrón fscrawler: acciones + bytes + timer)
    flush_acciones: int = 500
    flush_bytes: int = 50 * 1024 * 1024
    flush_segundos: float = 5.0

    # ⚙ K14 — política de reintentos ante 429/5xx
    reintentos_max: int = 3
    backoff_base_s: float = 1.0  # 1x, 2x, 4x

    cola_acotada_max: int = 10_000  # backpressure: el productor se frena (bulk_extractor)


class Config(BaseSettings):
    """Configuración raíz. Sobreescribible por entorno: NORM_SECCION__CAMPO."""

    model_config = SettingsConfigDict(
        env_prefix="NORM_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Conexiones (dev por defecto; prod las inyecta por entorno)
    postgres_dsn: str = "postgresql://norm:norm@localhost:5432/normalizacion"
    opensearch_url: str = "http://localhost:9200"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "norm"
    minio_secret_key: str = "norm-secreto"
    minio_bucket: str = "almacen"

    # Backend del almacén permanente tras la interfaz agnóstica (PROPUESTA §7.4):
    # "minio" en despliegues reales; "local" (directorio) para dev/tests sin Docker.
    almacen_backend: str = "minio"
    almacen_local_raiz: str = "./_almacen_dev"

    # Almacén FRÍO (barato, reversible): los COLD se mueven aquí ANTES de que la
    # puerta permita desechar el disco — el frío también es dato que sobrevive.
    minio_bucket_frio: str = "frio"
    almacen_frio_local_raiz: str = "./_frio_dev"

    indice_alias: str = "archivos"

    # API de búsqueda (Fase 5). Llaves vacías = auth deshabilitada (solo dev).
    api_keys: tuple[str, ...] = ()
    api_cors_origenes: tuple[str, ...] = ("http://localhost:5173",)  # el front en dev
    # Si se define, el explorador/pipeline SOLO puede operar dentro de esta carpeta
    # (en Docker: el volumen montado /datos). None = sin confinar (dev nativo).
    api_carpeta_raiz: str | None = None
    # Raíz para elegir la CARPETA DE DESTINO desde el front (en Docker: el volumen
    # /destino, montado con escritura). None = sin confinar (dev nativo).
    api_carpeta_destino_raiz: str | None = None
    api_pagina_max: int = 100  # límite DURO de tamaño de página
    api_autocompletar_max: int = 20
    api_solicitudes_por_minuto: int = 120  # rate-limit por llave/cliente

    filtro: PerillasFiltro = Field(default_factory=PerillasFiltro)
    worker: PerillasWorker = Field(default_factory=PerillasWorker)
    indexador: PerillasIndexador = Field(default_factory=PerillasIndexador)
    recursos: PerillasRecursos = Field(default_factory=PerillasRecursos)


def cargar_config() -> Config:
    """Carga la configuración desde defaults + .env + variables de entorno."""
    return Config()
