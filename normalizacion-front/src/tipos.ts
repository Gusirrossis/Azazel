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
