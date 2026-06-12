// Vista de UNA corrida (fases, progreso en vivo, veredicto) — compartida entre
// la barra de ingesta (inicio) y la pestaña Corridas (historial completo).

import { formatearDuracion } from "../../api";
import type { Corrida, FaseEjecutada } from "../../tipos";

export const NOMBRES_FASE: Record<string, string> = {
  catalogo: "Catálogo",
  precalificacion: "Doble filtro",
  worker: "Blobs + índice",
  mover_frio: "Mover a frío",
  verificacion: "Verificación",
  puerta: "Puerta",
};
const ORDEN_FASES = Object.keys(NOMBRES_FASE);

function resumenMetricas(m: Record<string, unknown>): string {
  // "errores" se renderiza aparte, resaltado — lo importante no se entierra
  const interesantes = [
    "archivos_vistos", "archivos_nuevos", "procesados", "procesos", "hot", "cold",
    "re_encolados", "blobs_nuevos", "deduplicados", "movidos", "verificados",
    "transitorios", "hechos", "pendientes",
  ];
  return interesantes
    .filter((k) => typeof m[k] === "number" && (m[k] as number) > 0)
    .map((k) => `${k.replace(/_/g, " ")}: ${m[k] as number}`)
    .join(" · ");
}

function FilaFase({ f }: { f: FaseEjecutada }) {
  const errores = typeof f.metricas.errores === "number" ? (f.metricas.errores as number) : 0;
  return (
    <div className="fase-fila hecha">
      <span className="fase-check">{errores > 0 ? "⚠" : "✓"}</span>
      <span className="fase-nombre">{NOMBRES_FASE[f.fase] ?? f.fase}</span>
      <span className="fase-dur">
        {formatearDuracion(f.duracion_s)}
        {f.archivos_por_segundo ? ` · ${f.archivos_por_segundo} archivos/s` : ""}
      </span>
      <span className="fase-metricas">{resumenMetricas(f.metricas)}</span>
      {errores > 0 && (
        <span className="chip bajo" title="ver el porqué de cada uno en la pestaña Errores">
          {errores} a dead-letter
        </span>
      )}
    </div>
  );
}

function duracionCorrida(corrida: Corrida): string | null {
  const inicio = new Date(corrida.iniciada_en).getTime();
  const fin = corrida.terminada_en ? new Date(corrida.terminada_en).getTime() : Date.now();
  if (Number.isNaN(inicio) || Number.isNaN(fin)) return null;
  return formatearDuracion((fin - inicio) / 1000);
}

export default function VistaCorrida({
  corrida,
  progreso,
}: {
  corrida: Corrida;
  progreso?: Record<string, number> | null;
}) {
  const hechas = new Set(corrida.fases.map((f) => f.fase));
  const total = progreso ? Object.values(progreso).reduce((a, b) => a + b, 0) : 0;
  const duracion = duracionCorrida(corrida);
  return (
    <div className="corrida">
      <div className="corrida-titulo">
        <b>{corrida.ruta}</b>
        {duracion && (
          <span className="corrida-tiempo">
            {corrida.estado === "EN_CURSO" ? `lleva ${duracion}` : `duró ${duracion}`}
          </span>
        )}
        {corrida.estado === "EN_CURSO" && <span className="chip medio">en curso</span>}
        {corrida.estado === "COMPLETADA" && <span className="chip ok">completada</span>}
        {corrida.estado === "FALLIDA" && <span className="chip bajo">fallida</span>}
        {corrida.seguro_para_desechar === true && (
          <span className="chip ok">✓ seguro para desechar</span>
        )}
        {corrida.seguro_para_desechar === false && (
          <span className="chip medio">aún no seguro</span>
        )}
      </div>
      {corrida.destino && (
        <div className="corrida-destino" title={corrida.destino}>
          destino: <code>{corrida.destino}</code>
        </div>
      )}
      {corrida.fases.map((f) => (
        <FilaFase key={f.fase} f={f} />
      ))}
      {corrida.estado === "EN_CURSO" &&
        (() => {
          // El doble filtro y los blobs+índice corren EN PARALELO: cuando la fase
          // actual es "worker", la precalificación sigue trabajando a la vez (y los
          // resultados ya son buscables). La UI lo refleja en vez de aparentar espera.
          const enParalelo = corrida.fase_actual === "worker";
          const activas = new Set(
            enParalelo ? ["precalificacion", "worker"] : [corrida.fase_actual ?? ""],
          );
          return (
            <>
              {ORDEN_FASES.filter((f) => !hechas.has(f)).map((f) => {
                const activa = activas.has(f);
                const paralela = enParalelo && (f === "precalificacion" || f === "worker");
                return (
                  <div key={f} className={activa ? "fase-fila activa" : "fase-fila"}>
                    <span className="fase-check">{activa ? "⟳" : "·"}</span>
                    <span className="fase-nombre">{NOMBRES_FASE[f]}</span>
                    {activa && <span className="fase-dur">trabajando…</span>}
                    {paralela && <span className="fase-paralelo">∥ paralelo</span>}
                  </div>
                );
              })}
              {enParalelo && (
                <div className="nota-paralelo">
                  filtro y blobs+índice avanzan a la vez — los resultados ya son buscables
                  mientras corre
                </div>
              )}
            </>
          );
        })()}
      {corrida.estado === "EN_CURSO" && progreso && total > 0 && (
        <div className="progreso-vivo">
          {Object.entries(progreso)
            .sort(([, a], [, b]) => b - a)
            .map(([estado, n]) => (
              <span key={estado}>
                {estado}: <b>{n.toLocaleString()}</b>
              </span>
            ))}
          <span className="progreso-total">de {total.toLocaleString()} en cola</span>
        </div>
      )}
      {corrida.error && <div className="corrida-error">{corrida.error}</div>}
    </div>
  );
}
