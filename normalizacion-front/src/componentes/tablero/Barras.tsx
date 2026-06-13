// Barras horizontales del tablero: composición por causa/estado con etiqueta
// humana, conteo y peso. El ancho es relativo al mayor del grupo.

import { formatearBytes } from "../../api";
import type { GrupoResumen } from "../../tipos";

export interface FilaBarra extends GrupoResumen {
  etiqueta: string; // nombre humano (la clave cruda va en el tooltip)
  color: string;
  tooltip?: string;
}

export default function Barras({
  filas,
  vacio = "nada que mostrar",
}: {
  filas: FilaBarra[];
  vacio?: string;
}) {
  if (filas.length === 0) return <div className="sin-sub">{vacio}</div>;
  const max = Math.max(...filas.map((f) => f.archivos), 1);
  return (
    <div className="barras">
      {filas.map((f) => (
        <div key={f.clave} className="barra-fila" title={f.tooltip ?? f.clave}>
          <span className="barra-etiqueta">{f.etiqueta}</span>
          <span className="barra-pista">
            <span
              className="barra-relleno"
              style={{ width: `${(f.archivos / max) * 100}%`, background: f.color }}
            />
          </span>
          <span className="barra-valor">
            <b>{f.archivos.toLocaleString()}</b>
            <i>{formatearBytes(f.bytes)}</i>
          </span>
        </div>
      ))}
    </div>
  );
}
