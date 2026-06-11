// Dona (donut) en SVG puro — sin dependencias. Usa el truco de la circunferencia
// ≈ 100 (r = 15.915) para que `stroke-dasharray` sea directamente el porcentaje.

import type { ReactNode } from "react";

export interface SegmentoDona {
  clave: string;
  valor: number;
  color: string;
}

export default function Dona({
  segmentos,
  tamano = 188,
  grosor = 5.5,
  activo,
  onActivar,
  children,
}: {
  segmentos: SegmentoDona[];
  tamano?: number;
  grosor?: number;
  activo?: string | null;
  onActivar?: (clave: string | null) => void;
  children?: ReactNode;
}) {
  const total = segmentos.reduce((s, x) => s + x.valor, 0);
  const r = 15.915;
  let acumulado = 0;

  return (
    <div className="dona-wrap" style={{ width: tamano, height: tamano }}>
      <svg viewBox="0 0 42 42" width={tamano} height={tamano} role="img">
        <circle className="dona-pista" cx="21" cy="21" r={r} fill="none" strokeWidth={grosor} />
        <g transform="rotate(-90 21 21)">
          {total > 0 &&
            segmentos.map((seg) => {
              const pct = (seg.valor / total) * 100;
              if (pct <= 0) return null;
              const atenuado = activo != null && activo !== seg.clave;
              const circ = (
                <circle
                  key={seg.clave}
                  cx="21"
                  cy="21"
                  r={r}
                  fill="none"
                  stroke={seg.color}
                  strokeWidth={activo === seg.clave ? grosor + 1.2 : grosor}
                  strokeDasharray={`${pct} ${100 - pct}`}
                  strokeDashoffset={-acumulado}
                  opacity={atenuado ? 0.32 : 1}
                  style={{ transition: "opacity .15s, stroke-width .15s", cursor: onActivar ? "pointer" : "default" }}
                  onMouseEnter={() => onActivar?.(seg.clave)}
                  onMouseLeave={() => onActivar?.(null)}
                >
                  <title>{`${seg.clave}: ${pct.toFixed(1)}%`}</title>
                </circle>
              );
              acumulado += pct;
              return circ;
            })}
        </g>
      </svg>
      {children != null && <div className="dona-centro">{children}</div>}
    </div>
  );
}
