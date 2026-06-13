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

export function urlContenido(archivoId: string): string {
  return `${BASE}/archivo/${encodeURIComponent(archivoId)}/contenido`;
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
