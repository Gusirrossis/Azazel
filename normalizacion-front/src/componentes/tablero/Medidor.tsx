// Medidor de proporción: barra apilada horizontal gruesa. A diferencia de una
// dona, comunica bien la DOMINANCIA EXTREMA (una categoría al 99.9% se ve como
// una barra casi llena con una astilla del resto — y el número lo confirma).
// Cada segmento garantiza un mínimo visible para no desaparecer.

import { formatearBytes } from "../../api";

export interface SegmentoMedidor {
  clave: string;
  etiqueta: string;
  valor: number; // se mide en bytes (peso)
  archivos: number;
  color: string;
}

export default function Medidor({
  segmentos,
  destacado,
}: {
  segmentos: SegmentoMedidor[];
  // { clave, prefijo, sufijo }: el % de ese segmento se muestra grande arriba
  destacado: { clave: string; prefijo: string; sufijo: string };
}) {
  const total = segmentos.reduce((s, x) => s + x.valor, 0) || 1;
  const seg = segmentos.find((s) => s.clave === destacado.clave);
  const pct = seg ? (seg.valor / total) * 100 : 0;

  return (
    <div className="medidor">
      <p className="medidor-titular">
        {destacado.prefijo}{" "}
        <b className="medidor-pct" style={{ color: seg?.color }}>
          {pct < 0.1 && pct > 0 ? "<0.1" : pct.toFixed(1)}%
        </b>{" "}
        {destacado.sufijo}
      </p>
      <div className="medidor-barra" role="img" aria-label={`${destacado.prefijo} ${pct.toFixed(1)}%`}>
        {segmentos.map((s) => {
          const ancho = (s.valor / total) * 100;
          return (
            <span
              key={s.clave}
              className="medidor-seg"
              style={{
                width: `${ancho}%`,
                minWidth: s.valor > 0 ? 4 : 0,
                background: s.color,
              }}
              title={`${s.etiqueta}: ${ancho.toFixed(2)}%`}
            />
          );
        })}
      </div>
      <div className="medidor-leyenda">
        {segmentos.map((s) => (
          <span key={s.clave} className="medidor-item">
            <span className="punto" style={{ background: s.color }} />
            <span className="medidor-item-etq">{s.etiqueta}</span>
            <span className="medidor-item-val">
              {s.archivos.toLocaleString()} · {formatearBytes(s.valor)}
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}
