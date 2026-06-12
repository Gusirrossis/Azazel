// Pestaña Corridas: la corrida en curso + el historial COMPLETO + destinos +
// preservados. (El inicio solo muestra la corrida en curso — el detalle vive aquí.)

import { useCallback, useEffect, useState } from "react";
import { estadoPipeline } from "../api";
import type { EstadoPipeline } from "../tipos";
import VistaCorrida from "./corridas/VistaCorrida";
import Preservados from "./corridas/Preservados";

export default function Corridas() {
  const [estado, setEstado] = useState<EstadoPipeline | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refrescar = useCallback(() => {
    estadoPipeline(50)
      .then((e) => {
        setEstado(e);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    refrescar();
    const intervalo = window.setInterval(refrescar, 5000);
    return () => window.clearInterval(intervalo);
  }, [refrescar]);

  return (
    <section className="corridas-pagina">
      {error && <div className="banner-error">{error}</div>}

      {estado?.en_curso && (
        <>
          <h4>Corrida en curso</h4>
          <VistaCorrida corrida={estado.en_curso} progreso={estado.progreso} />
        </>
      )}

      {estado && (
        <div className="destinos">
          <h4>Destinos (configurables con variables NORM_* en .env)</h4>
          {Object.entries(estado.destinos).map(([k, v]) => (
            <div key={k} className="destino">
              <span>{k.replace(/_/g, " ")}</span>
              <code>{v}</code>
            </div>
          ))}
        </div>
      )}

      <Preservados />

      <h4>Historial de corridas</h4>
      {estado && estado.historial.length === 0 && (
        <div className="sin-sub">aún no hay corridas — lanza una desde Inicio</div>
      )}
      {estado?.historial.map((c) => (
        <VistaCorrida key={c.id} corrida={c} />
      ))}
    </section>
  );
}
