// Cliente de la API (vía el proxy /api de Vite — sin CORS en dev).

import type {
  Estadisticas,
  EstadoPipeline,
  RespuestaBusqueda,
  RespuestaCarpetas,
  RespuestaPreservados,
  ResumenPanel,
  SolicitudBusqueda,
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

export function estadoPipeline(): Promise<EstadoPipeline> {
  return pedir<EstadoPipeline>("/pipeline/estado");
}

export function preservados(): Promise<RespuestaPreservados> {
  return pedir<RespuestaPreservados>("/pipeline/preservados");
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
