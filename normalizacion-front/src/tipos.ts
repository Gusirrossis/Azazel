// Espejo del contrato OpenAPI del backend (api/esquemas.py — `norm openapi`).

export interface SolicitudBusqueda {
  texto?: string;
  tipo_real?: string;
  extension?: string;
  disco_id?: string;
  puntaje_min?: number;
  facetas?: boolean;
  tamano_pagina?: number;
  cursor?: unknown[] | null;
}

export interface DocumentoArchivo {
  archivo_id: string;
  disco_id: string;
  nombre: string;
  ruta_original: string;
  extension: string | null;
  tamano: number;
  mtime: string;
  tipo_real: string | null;
  puntaje: number | null;
  ruta_decision: string | null;
  senales: Record<string, unknown>;
  motivo: string | null;
  version_filtro: string | null;
  hash_contenido: string | null;
  campos_extraidos: Record<string, unknown>;
  texto_indexable: string | null;
  perfil_calidad: Record<string, unknown> | null;
  limites_alcanzados: string[];
  procedencias: string[];
  // Fragmentos del contenido donde aparece lo buscado (marcadores ⟪…⟫, sin HTML)
  _resaltado?: string[];
}

export interface RespuestaBusqueda {
  total: number;
  documentos: DocumentoArchivo[];
  cursor: unknown[] | null;
  facetas: Record<string, Record<string, number>> | null;
  pit_id: string | null;
}

export interface Estadisticas {
  total_documentos: number;
  bytes_totales: number;
  por_tipo: Record<string, number>;
  por_disco: Record<string, number>;
}

export interface GrupoResumen {
  clave: string;
  archivos: number;
  bytes: number;
}

export interface ResumenPanel {
  total_archivos: number;
  bytes_totales: number;
  por_estado: GrupoResumen[];
  por_decision: GrupoResumen[]; // HOT, COLD, SIN_DECIDIR
  por_tipo: GrupoResumen[];
  generado_en: string;
}

// ----- pipeline de ingesta -----

export interface FaseEjecutada {
  fase: string;
  duracion_s: number;
  metricas: Record<string, unknown>;
  archivos_por_segundo: number | null;
}

export interface Corrida {
  id: number;
  disco_id: string;
  ruta: string;
  estado: "EN_CURSO" | "COMPLETADA" | "FALLIDA";
  fase_actual: string | null;
  fases: FaseEjecutada[];
  seguro_para_desechar: boolean | null;
  error: string | null;
  iniciada_en: string;
  terminada_en: string | null;
  destino: string | null; // carpeta elegida; null = destino configurado en .env
}

export interface EstadoPipeline {
  en_curso: Corrida | null;
  historial: Corrida[];
  destinos: Record<string, string>;
  progreso: Record<string, number> | null;
  destino_eligible: boolean; // ¿este despliegue permite elegir carpeta de destino?
  workers_auto: number; // workers que usaría el modo automático (núcleos - 2)
}

export interface RespuestaCarpetas {
  ruta: string;
  padre: string | null;
  carpetas: string[];
}

// ----- tablero de Inicio (GET /panel: todos los agregados en una llamada) -----

export interface TotalesTablero {
  archivos: number;
  bytes: number;
  hechos: number;
  errores: number;
  cold: number;
  en_proceso: number;
  pendientes: number;
  franja_gris: number;
  con_hash: number;
  hash_unicos: number;
}

export interface BucketPuntaje {
  desde: number; // cubeta de 10: 0, 10, … 90
  archivos: number;
}

export interface DiscoTablero {
  disco_id: string;
  archivos: number;
  bytes: number;
  hechos: number;
  errores: number;
}

export interface CorridaMini {
  id: number;
  ruta: string;
  estado: string;
  iniciada_en: string;
  terminada_en: string | null;
  duracion_s: number | null;
}

export interface RespuestaTablero {
  totales: TotalesTablero;
  por_estado: GrupoResumen[];
  por_decision: GrupoResumen[];
  por_tipo: GrupoResumen[];
  causas_cold: GrupoResumen[];
  causas_error: GrupoResumen[];
  histograma_puntaje: BucketPuntaje[];
  umbral_cold: number; // umbrales EFECTIVOS (base + overrides)
  umbral_hot: number;
  discos: DiscoTablero[];
  corridas: CorridaMini[];
  generado_en: string;
}

// ----- explorador de cola (Postgres: TODO lo catalogado, no solo lo indexado) -----

export interface ArchivoCola {
  archivo_id: string;
  disco_id: string;
  ruta: string;
  nombre: string;
  extension: string | null;
  tamano: number;
  mtime: string;
  estado: string;
  prioridad: number;
  intentos: number;
  error_motivo: string | null;
  puntaje: number | null;
  ruta_decision: string | null;
  tipo_real: string | null;
  senales: Record<string, unknown> | null;
  motivo: string | null;
  version_filtro: string | null;
  hash_contenido: string | null;
  actualizado_en: string;
}

export interface ResumenCola {
  por_causa: GrupoResumen[]; // POR QUÉ está ahí (prefijo del motivo/error)
  por_tipo: GrupoResumen[];
}

export interface RespuestaColaArchivos {
  total: number;
  archivos: ArchivoCola[];
  cursor: string | null; // pásalo de vuelta para la siguiente página; null = no hay más
  resumen: ResumenCola;
}

export interface RespuestaReprocesar {
  total: number;
  destinos: Record<string, number>; // a qué estado volvió cada cuántas filas
}

// ----- filtro editable (lista blanca, umbrales) -----

export interface FiltroVisible {
  modo_lista: string;
  tipos_interes: string[];
  tipos_interes_prefijos: string[];
  tipos_excluidos: string[];
  entropia_texto_max: number;
  entropia_comprimido_min: number;
  ratio_imprimibles_min: number;
  umbral_hot: number;
  umbral_cold: number;
  prioridad_contenedores: number;
  prioridad_extensiones: Record<string, number>;
  version_filtro: string;
}

export interface RespuestaFiltro {
  efectivo: FiltroVisible; // lo que usará la SIGUIENTE corrida
  overrides: Record<string, unknown>; // solo lo editado
  hay_overrides: boolean;
}

export type SolicitudFiltro = Partial<FiltroVisible>;

export interface ArchivoPreservado {
  disco_id: string;
  ruta: string;
  nombre: string;
  tamano: number;
  tipo_real: string | null;
  motivo: string;
  estado: string;
}

export interface RespuestaPreservados {
  total: number;
  por_motivo: Record<string, number>;
  archivos: ArchivoPreservado[];
}
