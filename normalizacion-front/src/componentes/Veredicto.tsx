// El VEREDICTO de un archivo en una frase: qué decidió el sistema, por qué, y
// qué hacer al respecto. Más el puntaje contra los umbrales como medidor visual.
// Compartido por el Detalle del índice y el explorador de la cola.

import { describirError, describirMotivo, type InfoMotivo } from "../motivos";

const ETIQUETA_ESTADO: Record<string, string> = {
  PENDIENTE: "en cola, sin evaluar",
  PRECALIFICADO: "evaluado, esperando worker",
  EN_PROCESO: "procesándose ahora",
  INDEXADO: "indexado, falta verificar",
  VERIFICADO: "verificado",
  HECHO: "completado y verificado",
  COLD: "en frío reversible",
  ERROR: "en dead-letter",
};

const TIER_EXPLICA: Record<string, string> = {
  T0: "Decidido en T0 por sus metadatos — ni siquiera hubo que abrirlo.",
  T1: "Decidido en T1 leyendo solo la firma del archivo (primeros KB).",
  T2: "Decidido en T2 analizando la estructura del head (entropía, formato).",
};

export function GaugePuntaje({
  puntaje,
  umbralCold = 35,
  umbralHot = 65,
}: {
  puntaje: number;
  umbralCold?: number;
  umbralHot?: number;
}) {
  return (
    <div className="gauge-puntaje">
      <div className="gauge-pista">
        <div className="gauge-zona zona-frio" style={{ width: `${umbralCold}%` }} />
        <div
          className="gauge-zona zona-gris"
          style={{ width: `${umbralHot - umbralCold}%` }}
        />
        <div className="gauge-zona zona-hot" style={{ width: `${100 - umbralHot}%` }} />
        <span className="gauge-aguja" style={{ left: `${Math.min(puntaje, 100)}%` }}>
          <b>{puntaje}</b>
        </span>
      </div>
      <div className="gauge-leyenda">
        <span>frío &lt; {umbralCold}</span>
        <span>franja gris</span>
        <span>HOT ≥ {umbralHot}</span>
      </div>
    </div>
  );
}

export default function Veredicto({
  estado,
  rutaDecision,
  puntaje,
  motivo,
  errorMotivo,
  intentos,
  tier,
  umbralCold,
  umbralHot,
}: {
  estado?: string | null;
  rutaDecision: string | null;
  puntaje: number | null;
  motivo: string | null;
  errorMotivo?: string | null;
  intentos?: number;
  tier?: string | null;
  umbralCold?: number;
  umbralHot?: number;
}) {
  const error = describirError(errorMotivo);
  const info: InfoMotivo = error ?? describirMotivo(motivo);
  const esError = error !== null;

  return (
    <div className={`veredicto tono-${info.tono}`}>
      <div className="veredicto-chips">
        {estado && (
          <span className="chip-veredicto chip-estado-grande">
            <span className={`chip-punto estado-${estado}`} />
            {estado}
            <i>· {ETIQUETA_ESTADO[estado] ?? ""}</i>
          </span>
        )}
        {!esError && rutaDecision && (
          <span className={`chip-veredicto ${rutaDecision === "HOT" ? "tono-ok" : "tono-frio"}`}>
            {rutaDecision === "HOT" ? "→ embudo HOT" : "→ frío reversible"}
          </span>
        )}
        {esError && intentos !== undefined && intentos > 1 && (
          <span className="chip-veredicto tono-alerta">{intentos} intentos</span>
        )}
      </div>

      <p className="veredicto-frase">
        <b className={`titulo-${info.tono}`}>{info.titulo}.</b> {info.explicacion}
        {!esError && tier && TIER_EXPLICA[tier] ? ` ${TIER_EXPLICA[tier]}` : ""}
      </p>
      {esError && errorMotivo && (
        <p className="veredicto-crudo" title={errorMotivo}>
          <code>{errorMotivo}</code>
        </p>
      )}
      {info.accion && (
        <p className="veredicto-accion">
          <span className="accion-marca">⮕ qué hacer:</span> {info.accion}
        </p>
      )}

      {puntaje !== null && puntaje !== undefined && (
        <GaugePuntaje puntaje={puntaje} umbralCold={umbralCold} umbralHot={umbralHot} />
      )}
    </div>
  );
}
