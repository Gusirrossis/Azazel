// Barras horizontales del tablero: composición por causa/estado con etiqueta
// humana, conteo y peso. El ancho usa escala RAÍZ CUADRADA para que un valor
// dominante (HECHO=19k) no deje en una rayita invisible a los chicos (ERROR=4);
// la barra es contexto visual, el número es el dato exacto.

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
  const maxRaiz = Math.max(...filas.map((f) => Math.sqrt(f.archivos)), 1);
  return (
    <div className="barras">
      {filas.map((f) => (
        <div key={f.clave} className="barra-fila" title={f.tooltip ?? f.clave}>
          <span className="barra-etiqueta">{f.etiqueta}</span>
          <span className="barra-pista">
            <span
              className="barra-relleno"
              style={{
                width: `${Math.max((Math.sqrt(f.archivos) / maxRaiz) * 100, f.archivos > 0 ? 4 : 0)}%`,
                background: f.color,
              }}
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
