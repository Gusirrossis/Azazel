import { useEffect, useState } from "react";
import { estadisticas, entidadesStats, obtenerTablero, topologia } from "../api";
import type { Topologia } from "../api";
import type { Estadisticas, EstadisticasEntidades, RespuestaTablero } from "../tipos";

// Resumen "qué hace ESTE nodo" para el Inicio. Se adapta por capacidad:
//  - todo nodo: documentos en el índice (crece al indexar).
//  - con `entidades` (vps): entidades resueltas + CURP/RFC.
//  - con ingesta y cola con pendientes (mac): procesados vs en cola.
// Se auto-actualiza para que el trabajo se VEA moverse, no un tablero estático.

const REFRESCO_MS = 8000;

function rolDe(t: Topologia): string {
  const c = t.capacidades;
  if (c.entidades && c.publico && !c.archivo_maestro) return "Búsqueda pública y resolución de entidades";
  if (c.ingesta && c.archivo_maestro && !c.entidades) return "Ingesta de discos y archivo maestro";
  if (c.ingesta && c.entidades && c.archivo_maestro) return "Nodo local: todo en una máquina";
  const partes = [];
  if (c.ingesta) partes.push("ingesta");
  if (c.entidades) partes.push("entidades");
  if (c.publico) partes.push("búsqueda pública");
  return partes.join(" · ") || "nodo";
}

function fmt(n: number | undefined): string {
  return (n ?? 0).toLocaleString("es-MX");
}

export default function ResumenNodo() {
  const [topo, setTopo] = useState<Topologia | null>(null);
  const [stats, setStats] = useState<Estadisticas | null>(null);
  const [ents, setEnts] = useState<EstadisticasEntidades | null>(null);
  const [tablero, setTablero] = useState<RespuestaTablero | null>(null);
  const [hace, setHace] = useState(0);

  useEffect(() => {
    topologia().then(setTopo).catch(() => setTopo(null));
  }, []);

  useEffect(() => {
    let vivo = true;
    const tira = () => {
      estadisticas().then((d) => vivo && setStats(d)).catch(() => {});
      obtenerTablero().then((d) => vivo && setTablero(d)).catch(() => {});
      if (!topo || topo.capacidades.entidades) {
        entidadesStats().then((d) => vivo && setEnts(d)).catch(() => {});
      }
      if (vivo) setHace(0);
    };
    tira();
    const id = setInterval(tira, REFRESCO_MS);
    const tic = setInterval(() => vivo && setHace((h) => h + 1), 1000);
    return () => { vivo = false; clearInterval(id); clearInterval(tic); };
  }, [topo]);

  if (!topo) return null;
  const c = topo.capacidades;
  const tot = tablero?.totales;
  const pendientes = tot ? tot.pendientes + tot.en_proceso : 0;
  const trabajando = c.ingesta && pendientes > 0;

  return (
    <section className="resumen-nodo" aria-label="Resumen del nodo">
      <div className="rn-cabecera">
        <div>
          <span className="rn-nodo">{topo.nodo_id}</span>
          <span className="rn-rol">{rolDe(topo)}</span>
        </div>
        <span className="rn-live" title="Se actualiza solo cada 8 s">
          <span className="rn-punto" /> en vivo · hace {hace}s
        </span>
      </div>

      <div className="rn-kpis">
        <div className="rn-kpi">
          <div className="rn-v">{fmt(stats?.total_documentos)}</div>
          <div className="rn-k">Documentos en el índice</div>
        </div>

        {c.entidades && (
          <>
            <div className="rn-kpi rn-acento">
              <div className="rn-v">{fmt(ents?.total)}</div>
              <div className="rn-k">Entidades resueltas</div>
            </div>
            <div className="rn-kpi">
              <div className="rn-v">{fmt(ents?.con_curp)}</div>
              <div className="rn-k">con CURP</div>
            </div>
            <div className="rn-kpi">
              <div className="rn-v">{fmt(ents?.por_ancla?.rfc)}</div>
              <div className="rn-k">con RFC</div>
            </div>
          </>
        )}

        {trabajando && (
          <div className="rn-kpi">
            <div className="rn-v">{fmt(pendientes)}</div>
            <div className="rn-k">En cola por procesar</div>
          </div>
        )}
      </div>

      {trabajando && (
        <p className="rn-nota">
          Indexando en vivo — los procesados suben conforme el nodo trabaja
          {!c.entidades && " (la resolución de entidades corre en el nodo de servicio)"}.
        </p>
      )}
      {c.entidades && !trabajando && (
        <p className="rn-nota">
          Este nodo sirve búsquedas y resuelve entidades sobre el corpus replicado — no ingesta discos.
        </p>
      )}
    </section>
  );
}
