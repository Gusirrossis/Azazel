// Histograma de puntajes contra los umbrales del filtro — el gráfico de
// CALIBRACIÓN. Problema que resuelve: en datos reales la cubeta HOT puede tener
// 19k y la franja gris 20 — en escala lineal la gris (lo que se quiere ver)
// desaparece. Por eso:
//   · altura en escala LOGARÍTMICA → las cubetas chicas siguen siendo visibles
//   · bandas de color de fondo por zona (frío / gris / HOT) → lectura inmediata
//   · líneas de umbral con etiqueta del valor exacto

import type { BucketPuntaje } from "../../tipos";

const CUBETAS = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90];
const alturaLog = (n: number) => (n <= 0 ? 0 : Math.log10(n + 1));

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
  const maxLog = Math.max(...buckets.map((b) => alturaLog(b.archivos)), 1);
  const zonaReal = (desde: number): "frio" | "gris" | "hot" => {
    const centro = desde + 5;
    if (centro < umbralCold) return "frio";
    if (centro >= umbralHot) return "hot";
    return "gris";
  };

  return (
    <div className="histograma">
      <div className="histo-area">
        {/* bandas de fondo por zona: posición = % del eje 0-100 */}
        <span className="histo-zona zona-frio" style={{ left: 0, width: `${umbralCold}%` }} />
        <span
          className="histo-zona zona-gris"
          style={{ left: `${umbralCold}%`, width: `${umbralHot - umbralCold}%` }}
        />
        <span className="histo-zona zona-hot" style={{ left: `${umbralHot}%`, width: `${100 - umbralHot}%` }} />

        <div className="histo-cols">
          {CUBETAS.map((desde) => {
            const n = porCubeta.get(desde) ?? 0;
            const alto = (alturaLog(n) / maxLog) * 100;
            return (
              <div
                key={desde}
                className="histo-col"
                title={`puntaje ${desde}–${desde + 9}: ${n.toLocaleString()} archivos`}
              >
                {n > 0 && <span className="histo-n">{n.toLocaleString()}</span>}
                <div
                  className={`histo-barra zona-${zonaReal(desde)}`}
                  style={{ height: `${Math.max(alto, n > 0 ? 4 : 0)}%` }}
                />
              </div>
            );
          })}
        </div>

        <span className="histo-umbral" style={{ left: `${umbralCold}%` }}>
          <i>{umbralCold}</i>
        </span>
        <span className="histo-umbral" style={{ left: `${umbralHot}%` }}>
          <i>{umbralHot}</i>
        </span>
      </div>
      <div className="histo-eje">
        <span className="histo-eje-frio">◀ frío</span>
        <span className="histo-eje-gris">franja gris (a calibrar)</span>
        <span className="histo-eje-hot">HOT ▶</span>
      </div>
      <p className="histo-escala">altura en escala logarítmica · cada barra = una cubeta de 10 puntos</p>
    </div>
  );
}
