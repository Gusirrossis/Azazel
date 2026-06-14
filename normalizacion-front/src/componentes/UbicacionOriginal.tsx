// Muestra DÓNDE quedó guardada la copia original del archivo (el blob). El jefe
// quería poder ver, p. ej., dónde está un txt ya indexado — aquí está la ruta.
// La raíz se resuelve por el DISCO del archivo (la carpeta destino de su corrida),
// así refleja la carpeta real del sistema aunque cada corrida use su propio destino.

import { rutaOriginal } from "../almacen";
import type { DestinosDisco } from "../tipos";

export default function UbicacionOriginal({
  hash,
  rutaDecision,
  discoId,
  destinos,
}: {
  hash: string | null;
  rutaDecision: string | null;
  discoId: string;
  destinos: DestinosDisco | null;
}) {
  const raices = destinos ? (destinos.por_disco[discoId] ?? destinos.global) : null;
  const loc = rutaOriginal(hash, rutaDecision, raices);
  if (!loc) return null;
  const esMinio = loc.raiz.startsWith("minio://");
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
        Copia content-addressed por hash (ab/cd/…).{" "}
        {esMinio
          ? "Está en el almacén de objetos MinIO (no una carpeta del sistema)."
          : "Es la carpeta real del sistema donde quedó el archivo."}
      </p>
    </>
  );
}
