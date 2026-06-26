import { useCallback, useEffect, useState } from "react";
import {
  recursosEstado,
  guardarRecursos,
  formatearBytes,
  type EstadoRecursos,
} from "../api";

// Etiquetas legibles de la política (lo que el usuario decide en una palabra).
const POLITICAS: { valor: EstadoRecursos["politica"]; texto: string; ayuda: string }[] = [
  { valor: "conservador", texto: "Conservador", ayuda: "Reserva ~40% de RAM al SO y otros programas. Más estable." },
  { valor: "balanceado", texto: "Balanceado", ayuda: "Reserva ~30%. Punto medio velocidad/estabilidad." },
  { valor: "maximo", texto: "Máximo", ayuda: "Reserva ~20%. Más rápido, arriesgado si hay otro sistema." },
];

// Visibilidad de "cuándo el sistema puede o no": muestra la RAM libre, si hay
// presión, y cuántos workers sugiere AHORA el gobernador adaptativo (K15). El
// operador elige la política sin tocar config ni reiniciar.
export default function Recursos() {
  const [estado, setEstado] = useState<EstadoRecursos | null>(null);
  const [guardando, setGuardando] = useState(false);

  const refrescar = useCallback(() => {
    recursosEstado().then(setEstado).catch(() => setEstado(null));
  }, []);

  useEffect(() => {
    refrescar();
    const t = window.setInterval(refrescar, 4000);
    return () => window.clearInterval(t);
  }, [refrescar]);

  const cambiarPolitica = (politica: EstadoRecursos["politica"]) => {
    setGuardando(true);
    guardarRecursos({ politica })
      .then(setEstado)
      .catch(() => refrescar())
      .finally(() => setGuardando(false));
  };

  const cambiarModo = (modo: EstadoRecursos["modo"]) => {
    setGuardando(true);
    guardarRecursos({ modo })
      .then(setEstado)
      .catch(() => refrescar())
      .finally(() => setGuardando(false));
  };

  if (!estado) return null;

  const libre = estado.disponible_mb ? formatearBytes(estado.disponible_mb * 1024 * 1024) : "—";
  const total = estado.total_mb ? formatearBytes(estado.total_mb * 1024 * 1024) : "—";

  return (
    <div className="recursos">
      <div className="recursos-estado">
        <span className={`chip ${estado.bajo_presion ? "alerta" : "ok"}`}>
          {estado.bajo_presion ? "⚠ memoria al límite" : "✓ memoria ok"}
        </span>
        {estado.psutil ? (
          <span className="recursos-metrica" title="RAM libre / total del sistema">
            RAM libre {libre} / {total}
            {typeof estado.porcentaje_usado === "number" && ` (${Math.round(estado.porcentaje_usado)}% en uso)`}
          </span>
        ) : (
          <span className="sin-sub">sin medición de memoria (psutil no disponible)</span>
        )}
        <span className="recursos-metrica" title="Workers que caben AHORA según la RAM libre">
          workers sugeridos: <strong>{estado.workers_sugeridos}</strong> / {estado.nucleos_tope} núcleos
        </span>
      </div>

      <div className="recursos-control">
        <label className="opcion-destino" title="Adaptativo: se ajusta a la RAM libre en tiempo real.">
          <input
            type="checkbox"
            checked={estado.modo === "adaptativo"}
            disabled={guardando}
            onChange={(e) => cambiarModo(e.target.checked ? "adaptativo" : "fijo")}
          />
          Adaptativo
        </label>
        <div className="recursos-politicas">
          {POLITICAS.map((p) => (
            <button
              key={p.valor}
              className={estado.politica === p.valor ? "primario" : "secundario"}
              disabled={guardando || estado.modo !== "adaptativo"}
              title={p.ayuda}
              onClick={() => cambiarPolitica(p.valor)}
            >
              {p.texto}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
