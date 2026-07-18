// Cliente de la API (vía el proxy /api de Vite — sin CORS en dev).

import type {
  Estadisticas,
  EstadoPipeline,
  RespuestaBusqueda,
  RespuestaCarpetas,
  RespuestaColaArchivos,
  RespuestaFiltro,
  RespuestaPreservados,
  RespuestaReprocesar,
  RespuestaTablero,
  ResumenPanel,
  SolicitudBusqueda,
  SolicitudFiltro,
  DestinosDisco,
  Entidad,
  RespuestaEntidades,
  EstadisticasEntidades,
  Receta,
  ClaveBusqueda,
  RespuestaClaveGenerada,
} from "./tipos";

const BASE = "/api";

function cabeceras(): HeadersInit {
  const llave = localStorage.getItem("norm_api_key");
  return llave
    ? { "Content-Type": "application/json", "X-API-Key": llave }
    : { "Content-Type": "application/json" };
}

async function pedir<T>(ruta: string, init?: RequestInit): Promise<T> {
  const respuesta = await fetch(`${BASE}${ruta}`, { ...init, headers: cabeceras() });
  if (!respuesta.ok) {
    const detalle = await respuesta.text().catch(() => "");
    throw new Error(`API ${respuesta.status}: ${detalle.slice(0, 200)}`);
  }
  return (await respuesta.json()) as T;
}

export function buscar(solicitud: SolicitudBusqueda): Promise<RespuestaBusqueda> {
  return pedir<RespuestaBusqueda>("/buscar", {
    method: "POST",
    body: JSON.stringify(solicitud),
  });
}

export async function autocompletar(prefijo: string): Promise<string[]> {
  const datos = await pedir<{ sugerencias: string[] }>(
    `/autocompletar?q=${encodeURIComponent(prefijo)}`,
  );
  return datos.sugerencias;
}

export function estadisticas(): Promise<Estadisticas> {
  return pedir<Estadisticas>("/estadisticas");
}

export function resumen(): Promise<ResumenPanel> {
  return pedir<ResumenPanel>("/resumen");
}

export function obtenerTablero(): Promise<RespuestaTablero> {
  return pedir<RespuestaTablero>("/panel");
}

// ----- entidades (Fase 2) -----

export interface FiltrosEntidades {
  nombre?: string;
  curp?: string;
  cursor?: string;
  limite?: number;
}

export function entidades(f: FiltrosEntidades = {}): Promise<RespuestaEntidades> {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v) p.set(k, String(v));
  return pedir<RespuestaEntidades>(`/entidades?${p.toString()}`);
}

export function entidadDetalle(id: string): Promise<Entidad> {
  return pedir<Entidad>(`/entidades/${encodeURIComponent(id)}`);
}

export function entidadesStats(): Promise<EstadisticasEntidades> {
  return pedir<EstadisticasEntidades>("/entidades/estadisticas");
}

// Misma persona, distintas estructuras: proyección dinámica por receta.
export function entidadProyectar(
  id: string,
  receta: string,
): Promise<{ receta: string; salida: Record<string, any> }> {
  return pedir(`/entidades/${encodeURIComponent(id)}/proyectar?receta=${encodeURIComponent(receta)}`);
}

export function entidadActivo(id: string, activo: boolean): Promise<{ activo: boolean }> {
  return pedir(`/entidades/${encodeURIComponent(id)}/activo?activo=${activo}`, { method: "POST" });
}

// Exporta TODAS las personas a un solo archivo con la receta dada (p.ej. fz1_bundle
// produce el archivo Fz1 completo: _metadata + personas[] + _mapeo).
export function exportarEntidades(receta: string, limite = 10000): Promise<unknown> {
  return pedir(`/entidades/exportar?receta=${encodeURIComponent(receta)}&limite=${limite}`);
}

export interface ResumenBackfill {
  docs: number; con_persona: number; sin_persona: number; anclas: number;
  entidades_nuevas: number; entidades_fusionadas: number; errores: number;
  cursor: string | null;
}

// E4 (1er paso): lanza la resolución de los registros YA INDEXADOS (con CURP/RFC)
// en SEGUNDO PLANO (gobernada por K15: no satura ni tumba el panel). Reanudable.
export function backfillEntidades(maxDocs = 2000): Promise<{ lanzado: boolean; motivo: string }> {
  return pedir<{ lanzado: boolean; motivo: string }>(
    `/entidades/backfill?max_docs=${maxDocs}`, { method: "POST" },
  );
}

export interface EstadoBackfill {
  ejecutando: boolean;
  disponible: boolean;
  ultimo: (ResumenBackfill & { ts: string; ejecutando: boolean; error: string | null }) | null;
}

// Avance del backfill en curso (o el último resumen) — para mostrar progreso sin bloquear.
export function estadoBackfill(): Promise<EstadoBackfill> {
  return pedir<EstadoBackfill>("/entidades/backfill/estado");
}

export interface AtributoDeclarado { nombre: string; normalizador: string }

export interface NucleoEntidad {
  campos: { nombre: string; normalizador: string; ancla: boolean }[];
  derivados: string[];
}

// El esquema FIJO de la persona (de solo lectura): lo que SIEMPRE se captura.
export function nucleoEntidad(): Promise<NucleoEntidad> {
  return pedir<NucleoEntidad>("/entidades/config/nucleo");
}

// Atributos EXTRA que la entidad captura además del núcleo fijo (color_favorito…).
export function atributosDeclarados(): Promise<AtributoDeclarado[]> {
  return pedir<AtributoDeclarado[]>("/entidades/config/atributos");
}

export function guardarAtributos(atributos: AtributoDeclarado[]): Promise<AtributoDeclarado[]> {
  return pedir<AtributoDeclarado[]>("/entidades/config/atributos", {
    method: "PUT", body: JSON.stringify({ atributos }),
  });
}

export interface DestinoEntidades {
  habilitado: boolean; url: string; auth_token: string;
  lote: number; intervalo_seg: number;
}

// Destino al que Azazel manda las entidades resueltas (el backend central AEB).
export function destinoEntidades(): Promise<DestinoEntidades> {
  return pedir<DestinoEntidades>("/entidades/config/destino");
}

export function guardarDestino(d: DestinoEntidades): Promise<DestinoEntidades> {
  return pedir<DestinoEntidades>("/entidades/config/destino", {
    method: "PUT", body: JSON.stringify(d),
  });
}

export interface UltimoEnvio {
  ts: string; ok: boolean; detuvo_en: string | null;
  entidades: number; creadas: number; actualizadas: number; fallidas: number; errores: string[];
}
export interface EstadoEnvio {
  habilitado: boolean; url: string | null; cursor: string | null; pendientes: number;
  intervalo_seg: number; ultimo: UltimoEnvio | null; disponible?: boolean;
}
export interface ResumenEnvio {
  lotes: number; entidades: number; creadas: number; actualizadas: number;
  sin_cambio: number; fallidas: number; cursor: string | null;
  detuvo_en: string | null; errores: string[];
}

// Estado del envío de entidades al AEB (cuántas faltan) y disparo del worker.
export function estadoEnvio(): Promise<EstadoEnvio> {
  return pedir<EstadoEnvio>("/entidades/enviar/estado");
}

export function enviarEntidades(reiniciar = false): Promise<ResumenEnvio> {
  return pedir<ResumenEnvio>(`/entidades/enviar?reiniciar=${reiniciar}`, { method: "POST" });
}

// ----- recetas de proyección (editables) -----

export function recetas(clase?: string): Promise<Receta[]> {
  return pedir<Receta[]>(`/entidades/recetas${clase ? `?clase=${clase}` : ""}`);
}

export function guardarReceta(r: Partial<Receta>): Promise<Receta> {
  return pedir<Receta>(`/entidades/recetas/${encodeURIComponent(r.clave!)}`, {
    method: "PUT",
    body: JSON.stringify({
      clave: r.clave, nombre: r.nombre, descripcion: r.descripcion ?? "",
      definicion: r.definicion, version: r.version ?? "v1",
      tipo: r.tipo ?? "persona", clase: r.clase ?? "proyeccion",
    }),
  });
}

export function borrarReceta(clave: string): Promise<{ borrada: boolean }> {
  return pedir(`/entidades/recetas/${encodeURIComponent(clave)}`, { method: "DELETE" });
}

export type AmbitoCarpetas = "datos" | "destino";

export function carpetas(ruta?: string, ambito: AmbitoCarpetas = "datos"): Promise<RespuestaCarpetas> {
  const params = new URLSearchParams({ ambito });
  if (ruta) params.set("ruta", ruta);
  return pedir<RespuestaCarpetas>(`/sistema/carpetas?${params.toString()}`);
}

export function crearCarpeta(ruta: string, nombre: string): Promise<RespuestaCarpetas> {
  return pedir<RespuestaCarpetas>("/sistema/carpetas", {
    method: "POST",
    body: JSON.stringify({ ruta, nombre }),
  });
}

export function ejecutarPipeline(
  ruta: string,
  destino?: string | null,
  workers?: number | null,
): Promise<{ corrida_id: number }> {
  const cuerpo: Record<string, unknown> = { ruta };
  if (destino) cuerpo.destino = destino;
  if (workers) cuerpo.workers = workers;
  return pedir<{ corrida_id: number }>("/pipeline/ejecutar", {
    method: "POST",
    body: JSON.stringify(cuerpo),
  });
}

export function estadoPipeline(historial?: number): Promise<EstadoPipeline> {
  const sufijo = historial ? `?historial=${historial}` : "";
  return pedir<EstadoPipeline>(`/pipeline/estado${sufijo}`);
}

export function preservados(): Promise<RespuestaPreservados> {
  return pedir<RespuestaPreservados>("/pipeline/preservados");
}

// Raíz real del almacén por disco — para mostrar dónde quedó cada original
// aunque cada corrida haya elegido una carpeta destino distinta.
export function destinosPorDisco(): Promise<DestinosDisco> {
  return pedir<DestinosDisco>("/sistema/destinos-disco");
}

// ----- explorador de cola -----

export interface FiltrosCola {
  estado?: string;
  ruta_decision?: string;
  motivo?: string;
  error_motivo?: string;
  extension?: string;
  nombre?: string;
  disco_id?: string;
  puntaje_min?: number; // franja gris: [umbral_cold, umbral_hot)
  puntaje_max?: number;
  cursor?: string;
  limite?: number;
}

export function archivosCola(filtros: FiltrosCola): Promise<RespuestaColaArchivos> {
  const params = new URLSearchParams();
  for (const [clave, valor] of Object.entries(filtros)) {
    if (valor !== undefined && valor !== null && valor !== "") params.set(clave, String(valor));
  }
  return pedir<RespuestaColaArchivos>(`/cola/archivos?${params.toString()}`);
}

export function reprocesarErrores(motivoComo?: string): Promise<RespuestaReprocesar> {
  return pedir<RespuestaReprocesar>("/cola/reprocesar-errores", {
    method: "POST",
    body: JSON.stringify(motivoComo ? { motivo_como: motivoComo } : {}),
  });
}

export function rescoreFrio(): Promise<{ re_encolados: number }> {
  return pedir<{ re_encolados: number }>("/cola/rescore-frio", { method: "POST" });
}

// Re-explorar contenedores preservados sin abrir (RAR sin herramienta, etc.):
// los devuelve a PENDIENTE para re-precalificarlos con las herramientas ya
// instaladas. A diferencia de rescore-frío, estos viven en HOT.
export function reexplorarPreservados(): Promise<{ re_encolados: number }> {
  return pedir<{ re_encolados: number }>("/cola/reexplorar-preservados", { method: "POST" });
}

// ----- filtro editable -----

export function obtenerFiltro(): Promise<RespuestaFiltro> {
  return pedir<RespuestaFiltro>("/filtro");
}

export function guardarFiltro(cambios: SolicitudFiltro): Promise<RespuestaFiltro> {
  return pedir<RespuestaFiltro>("/filtro", { method: "PUT", body: JSON.stringify(cambios) });
}

export function restablecerFiltro(): Promise<RespuestaFiltro> {
  return pedir<RespuestaFiltro>("/filtro", { method: "DELETE" });
}

// ----- gobernador de recursos (K15) -----

export interface EstadoRecursos {
  modo: "adaptativo" | "fijo";
  politica: "conservador" | "balanceado" | "maximo";
  reserva_pct: number;
  nucleos_tope: number;
  workers_sugeridos: number;
  psutil: boolean;
  total_mb?: number;
  disponible_mb?: number;
  reserva_mb?: number;
  porcentaje_usado?: number;
  bajo_presion?: boolean;
}

export interface CambioRecursos {
  modo?: "adaptativo" | "fijo";
  politica?: "conservador" | "balanceado" | "maximo";
  mem_por_worker_mb?: number;
  workers_max?: number;
}

// Estado del gobernador: RAM libre, presión, política y workers sugeridos AHORA.
export function recursosEstado(): Promise<EstadoRecursos> {
  return pedir<EstadoRecursos>("/sistema/recursos");
}

// Cambia la política de recursos sin reiniciar (se aplica en vivo a los daemons).
export function guardarRecursos(cambios: CambioRecursos): Promise<EstadoRecursos> {
  return pedir<EstadoRecursos>("/sistema/recursos", {
    method: "PUT",
    body: JSON.stringify(cambios),
  });
}

export function urlContenido(archivoId: string): string {
  return `${BASE}/archivo/${encodeURIComponent(archivoId)}/contenido`;
}

// ---- Claves de acceso al buscador (con nombre) ----
export function listarClavesBusqueda(): Promise<ClaveBusqueda[]> {
  return pedir<ClaveBusqueda[]>("/seguridad/claves-busqueda");
}

export async function generarClaveBusqueda(nombre: string): Promise<RespuestaClaveGenerada> {
  const r = await pedir<RespuestaClaveGenerada>("/seguridad/claves-busqueda", {
    method: "POST",
    body: JSON.stringify({ nombre }),
  });
  // Guarda la clave localmente para que ESTE panel siga funcionando al activarse la auth.
  localStorage.setItem("norm_api_key", r.clave);
  return r;
}

export function revocarClaveBusqueda(nombre: string): Promise<{ revocada: boolean }> {
  return pedir<{ revocada: boolean }>(`/seguridad/claves-busqueda/${encodeURIComponent(nombre)}`, {
    method: "DELETE",
  });
}

export function formatearDuracion(segundos: number): string {
  if (segundos < 60) return `${segundos < 10 ? segundos.toFixed(2) : segundos.toFixed(1)}s`;
  const total = Math.round(segundos);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`;
}

export function formatearBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}
