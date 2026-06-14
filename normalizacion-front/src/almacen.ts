// Ubicación física de la copia original (blob content-addressed).
//
// El almacén guarda cada original como {raíz}/ab/cd/abcd… donde ab/cd son los
// primeros 4 caracteres del sha256 — el mismo layout que usa el backend
// (modelo.py: clave_almacen). La raíz depende de la decisión del archivo:
//   HOT  → almacén permanente
//   COLD → frío reversible
//
// Las raíces vienen RESUELTAS POR DISCO desde GET /sistema/destinos-disco: el
// backend ya tradujo el destino de la corrida de cada disco a la carpeta REAL
// del sistema ({destino}/almacen), así que si una corrida eligió su propia
// carpeta destino, aquí se ve esa carpeta — no la del .env global.

import type { RaicesAlmacen } from "./tipos";

export function claveAlmacen(hash: string): string {
  return `${hash.slice(0, 2)}/${hash.slice(2, 4)}/${hash}`;
}

export function rutaOriginal(
  hash: string | null,
  rutaDecision: string | null,
  raices: RaicesAlmacen | null,
): { raiz: string; ruta: string; almacen: "hot" | "frio" } | null {
  if (!hash || !raices) return null;
  const esFrio = rutaDecision === "COLD";
  const raiz = esFrio ? raices.frio : raices.hot;
  if (!raiz) return null;
  const sep = raiz.includes("\\") ? "\\" : "/";
  return {
    raiz,
    ruta: `${raiz}${sep}${claveAlmacen(hash)}`,
    almacen: esFrio ? "frio" : "hot",
  };
}
