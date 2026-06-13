// Señales del filtro (T0-T3) de forma legible: la entropía como barra con los
// umbrales marcados, booleanos como chips, el resto como filas etiqueta/valor.
// Compartido por el Detalle (índice) y el explorador de la cola.
//
// La entropía se MUESTRA en 0–1 (normalizada) aunque el backend la maneje en
// bits 0–8 — la conversión es solo de presentación (ver entropia.ts).

import { formatearEntropia, normalizarEntropia } from "../entropia";

const ETIQUETA: Record<string, string> = {
  tier: "Tier que decidió",
  detector: "Detector",
  encoding: "Encoding",
  formato: "Formato contenedor",
  profundidad: "Profundidad de anidación",
  entradas_re_encoladas: "Entradas re-encoladas",
  columnas: "Columnas (CSV)",
  guard_violado: "Guard violado",
};

const CHIP_BOOL: Record<string, string> = {
  texto_legible: "texto legible",
  es_csv: "CSV",
  es_json: "JSON",
  es_ndjson: "NDJSON",
  es_xml: "XML",
  es_sql: "SQL",
  es_correo: "correo",
  es_html: "HTML",
  es_contenedor: "contenedor",
};

function zonaEntropia(e: number, textoMax: number, comprimidoMin: number): string {
  if (e < textoMax) return "texto plano";
  if (e > comprimidoMin) return "comprimido / cifrado";
  return "intermedio";
}

export default function Senales({
  senales,
  entropiaTextoMax = 3.5,
  entropiaComprimidoMin = 7.5,
}: {
  senales: Record<string, unknown>;
  entropiaTextoMax?: number;
  entropiaComprimidoMin?: number;
}) {
  const entropia = typeof senales.entropia === "number" ? senales.entropia : null;
  const ratio =
    typeof senales.ratio_imprimibles === "number" ? senales.ratio_imprimibles : null;
  const chips = Object.keys(CHIP_BOOL).filter((k) => senales[k] === true);
  const conocidas = new Set([
    "entropia",
    "ratio_imprimibles",
    "extension_miente",
    ...Object.keys(CHIP_BOOL),
    ...Object.keys(ETIQUETA),
  ]);
  const restantes = Object.entries(senales).filter(
    ([k, v]) => !conocidas.has(k) && v !== null && v !== false,
  );

  return (
    <div className="senales">
      {entropia !== null && (
        <div className="senal-entropia">
          <div className="detalle-fila">
            <span className="detalle-etiqueta">Entropía (Shannon)</span>
            <span>
              <b>{formatearEntropia(entropia)}</b> / 1 —{" "}
              {zonaEntropia(entropia, entropiaTextoMax, entropiaComprimidoMin)}
            </span>
          </div>
          <div
            className="barra-entropia"
            title={`umbrales del filtro: < ${formatearEntropia(entropiaTextoMax)} texto · > ${formatearEntropia(entropiaComprimidoMin)} comprimido/cifrado`}
          >
            <div
              className="barra-entropia-relleno"
              style={{ width: `${normalizarEntropia(entropia) * 100}%` }}
            />
            <span
              className="barra-marca"
              style={{ left: `${normalizarEntropia(entropiaTextoMax) * 100}%` }}
            />
            <span
              className="barra-marca"
              style={{ left: `${normalizarEntropia(entropiaComprimidoMin) * 100}%` }}
            />
          </div>
          <div className="barra-leyenda">
            <span>0 · texto</span>
            <span>{formatearEntropia(entropiaTextoMax)}</span>
            <span>{formatearEntropia(entropiaComprimidoMin)}</span>
            <span>1 · cifrado</span>
          </div>
        </div>
      )}
      {ratio !== null && (
        <div className="detalle-fila">
          <span className="detalle-etiqueta">Caracteres imprimibles</span>
          <span>{(ratio * 100).toFixed(1)}%</span>
        </div>
      )}
      {(chips.length > 0 || senales.extension_miente === true) && (
        <div className="senales-chips">
          {chips.map((k) => (
            <span key={k} className="chip ok">
              {CHIP_BOOL[k]}
            </span>
          ))}
          {senales.extension_miente === true && (
            <span className="chip alerta" title="La extensión no corresponde al contenido">
              ext. miente
            </span>
          )}
        </div>
      )}
      {Object.entries(ETIQUETA)
        .filter(([k]) => senales[k] !== undefined && senales[k] !== null)
        .map(([k, etiqueta]) => (
          <div key={k} className="detalle-fila">
            <span className="detalle-etiqueta">{etiqueta}</span>
            <span>{String(senales[k])}</span>
          </div>
        ))}
      {restantes.map(([k, v]) => (
        <div key={k} className="detalle-fila">
          <span className="detalle-etiqueta">{k.replace(/_/g, " ")}</span>
          <span>{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
        </div>
      ))}
    </div>
  );
}
