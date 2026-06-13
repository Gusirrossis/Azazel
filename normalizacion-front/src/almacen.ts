// Ubicación física de la copia original (blob content-addressed).
//
// El almacén guarda cada original como {raíz}/ab/cd/abcd… donde ab/cd son los
// primeros 4 caracteres del sha256 — el mismo layout que usa el backend
// (modelo.py: clave_almacen). La raíz depende de la decisión del archivo:
//   HOT  → almacén permanente (destinos.originales_hot)
//   COLD → frío reversible    (destinos.frio_reversible)
//
// La raíz sale de los `destinos` que ya expone GET /pipeline/estado; refleja el
// destino VIGENTE — si distintas corridas usaron carpetas distintas, es la del
// destino configurado ahora (en el piloto, con un destino único, es exacta).

export function claveAlmacen(hash: string): string {
  return `${hash.slice(0, 2)}/${hash.slice(2, 4)}/${hash}`;
}

export function rutaOriginal(
  hash: string | null,
  rutaDecision: string | null,
  destinos: Record<string, string> | null,
): { raiz: string; ruta: string; almacen: "hot" | "frio" } | null {
  if (!hash || !destinos) return null;
  const esFrio = rutaDecision === "COLD";
  const raiz = esFrio ? destinos.frio_reversible : destinos.originales_hot;
  if (!raiz) return null;
  const sep = raiz.includes("\\") ? "\\" : "/";
  return {
    raiz,
    ruta: `${raiz}${sep}${claveAlmacen(hash)}`,
    almacen: esFrio ? "frio" : "hot",
  };
}
