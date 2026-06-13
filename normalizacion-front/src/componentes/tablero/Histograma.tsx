// Histograma de puntajes contra los umbrales del filtro — el gráfico de
// CALIBRACIÓN: si la masa cae en la franja gris, el filtro está adivinando;
// si se acumula pegada a un umbral, el umbral está mal puesto.

import type { BucketPuntaje } from "../../tipos";

const CUBETAS = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90];

export default function Histograma({
  buckets,
  umbralCold,
  umbralHot,
}: {
  buckets: BucketPuntaje[];
  umbralCold: number;
  umbralHot: number;
}) {
  const porCubeta = new Map(buckets.map((b) => [b.desde, b.archivos]));
  const max = Math.max(...buckets.map((b) => b.archivos), 1);
  const zona = (desde: number) =>
    desde + 10 <= umbralCold ? "frio" : desde >= umbralHot ? "hot" : "gris";

  return (
    <div className="histograma">
      <div className="histo-area">
        {CUBETAS.map((desde) => {
          const n = porCubeta.get(desde) ?? 0;
          return (
            <div key={desde} className="histo-col" title={`${desde}–${desde + 9}: ${n.toLocaleString()} archivos`}>
              {n > 0 && <span className="histo-n">{n.toLocaleString()}</span>}
              <div
                className={`histo-barra zona-${zona(desde)}`}
                style={{ height: `${Math.max((n / max) * 100, n > 0 ? 3 : 0)}%` }}
              />
            </div>
          );
        })}
        <span className="histo-umbral" style={{ left: `${umbralCold}%` }} title={`umbral frío: ${umbralCold}`} />
        <span className="histo-umbral" style={{ left: `${umbralHot}%` }} title={`umbral HOT: ${umbralHot}`} />
      </div>
      <div className="histo-eje">
        <span>0</span>
        <span className="histo-eje-frio">frío &lt; {umbralCold}</span>
        <span className="histo-eje-gris">franja gris</span>
        <span className="histo-eje-hot">HOT ≥ {umbralHot}</span>
        <span>100</span>
      </div>
    </div>
  );
}
