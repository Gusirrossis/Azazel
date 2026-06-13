// Muestra DÓNDE quedó guardada la copia original del archivo (el blob). El jefe
// quería poder ver, p. ej., dónde está un txt ya indexado — aquí está la ruta.

import { rutaOriginal } from "../almacen";

export default function UbicacionOriginal({
  hash,
  rutaDecision,
  destinos,
}: {
  hash: string | null;
  rutaDecision: string | null;
  destinos: Record<string, string> | null;
}) {
  const loc = rutaOriginal(hash, rutaDecision, destinos);
  if (!loc) return null;
  return (
    <>
      <h3>Ubicación del original</h3>
      <div className="ubicacion">
        <span className={`chip ${loc.almacen === "hot" ? "ok" : "medio"}`}>
          {loc.almacen === "hot" ? "almacén permanente" : "frío reversible"}
        </span>
        <code className="ubicacion-ruta" title={loc.ruta}>
          {loc.ruta}
        </code>
        <button
          className="enlace"
          onClick={() => navigator.clipboard?.writeText(loc.ruta)}
          title="copiar la ruta completa"
        >
          copiar
        </button>
      </div>
      <p className="panel-nota">
        Copia content-addressed por hash (ab/cd/…). La raíz es el destino vigente del
        almacén; si una corrida usó otra carpeta de destino, varía.
      </p>
    </>
  );
}
