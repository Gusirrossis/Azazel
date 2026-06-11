// Panel de control: visibilidad de la cola (Postgres), no solo de lo indexado.
// Dos donas — frío vs caliente y reparto por tipo — más el desglose del frío.

import { useCallback, useEffect, useState } from "react";
import { resumen as pedirResumen, formatearBytes } from "../api";
import type { GrupoResumen, ResumenPanel } from "../tipos";
import Dona, { type SegmentoDona } from "./Dona";

type Modo = "peso" | "cantidad";

// Paleta apagada, en clave con el tema (oro/bronce/plata/verde/teal/mauve…).
const COLORES_TIPO = [
  "#c9a45c", "#8aa17d", "#7d96a1", "#b5685f", "#a78bba", "#c2a06b",
  "#6f9a8d", "#9a8f6f", "#b08a5a", "#8e939b", "#7e9c6b", "#c08f6a",
];
const COLOR_HOT = "#c9a45c"; // oro: caliente, lo que permanece
const COLOR_COLD = "#6f8fa1"; // azul acero frío

function etiquetaTipo(mime: string): string {
  const corto: Record<string, string> = {
    "application/x-7z-compressed": "7z",
    "application/x-rar-compressed": "rar",
    "application/zip": "zip",
    "application/x-tar": "tar",
    "application/gzip": "gzip",
    "text/plain": "texto",
    "text/csv": "csv",
    "application/json": "json",
    "application/pdf": "pdf",
    "application/octet-stream": "binario",
    "application/x-ms-wim": "wim",
  };
  if (corto[mime]) return corto[mime];
  const sub = mime.includes("/") ? mime.split("/")[1] : mime;
  return sub.replace(/^x-/, "").replace(/-compressed$/, "");
}

const ETIQUETA_DECISION: Record<string, string> = {
  HOT: "Caliente (se conserva)",
  COLD: "Frío (se deja de lado)",
};

export default function Panel() {
  const [datos, setDatos] = useState<ResumenPanel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [abierto, setAbierto] = useState(true);
  const [modo, setModo] = useState<Modo>("peso");
  const [activoTipo, setActivoTipo] = useState<string | null>(null);
  const [activoDec, setActivoDec] = useState<string | null>(null);

  const cargar = useCallback(() => {
    pedirResumen()
      .then((r) => {
        setDatos(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    if (!abierto) return;
    cargar();
    const id = setInterval(cargar, 12000);
    return () => clearInterval(id);
  }, [abierto, cargar]);

  const valorDe = (g: GrupoResumen) => (modo === "peso" ? g.bytes : g.archivos);
  const fmt = (v: number) => (modo === "peso" ? formatearBytes(v) : v.toLocaleString());

  // ---- frío vs caliente (sobre lo YA decidido) ----
  const dec = datos?.por_decision ?? [];
  const hot = dec.find((d) => d.clave === "HOT") ?? { clave: "HOT", archivos: 0, bytes: 0 };
  const cold = dec.find((d) => d.clave === "COLD") ?? { clave: "COLD", archivos: 0, bytes: 0 };
  const sin = dec.find((d) => d.clave === "SIN_DECIDIR") ?? { clave: "SIN_DECIDIR", archivos: 0, bytes: 0 };
  const baseDecidido = valorDe(hot) + valorDe(cold);
  const pctFrio = baseDecidido > 0 ? (valorDe(cold) / baseDecidido) * 100 : 0;

  // Solo lo DECIDIDO (caliente vs frío); los pendientes irían como una porción
  // gigante que aplasta el resto, así que se muestran como contexto en la nota.
  const segDecision: SegmentoDona[] = [
    { clave: "HOT", valor: valorDe(hot), color: COLOR_HOT },
    { clave: "COLD", valor: valorDe(cold), color: COLOR_COLD },
  ];

  // ---- por tipo (top + "otros") ----
  const tipos = datos?.por_tipo ?? [];
  const TOP = 8;
  const ordenados = [...tipos].sort((a, b) => valorDe(b) - valorDe(a));
  const principales = ordenados.slice(0, TOP);
  const restoVal = ordenados.slice(TOP).reduce((s, g) => s + valorDe(g), 0);
  const segTipo: SegmentoDona[] = principales.map((g, i) => ({
    clave: g.clave,
    valor: valorDe(g),
    color: COLORES_TIPO[i % COLORES_TIPO.length],
  }));
  if (restoVal > 0) segTipo.push({ clave: "otros", valor: restoVal, color: "#5a5660" });

  return (
    <section className="panel">
      <div className="panel-barra">
        <button className="panel-toggle" onClick={() => setAbierto((v) => !v)} aria-expanded={abierto}>
          <span className="panel-chevron">{abierto ? "▾" : "▸"}</span> Panel de control
        </button>
        {datos && (
          <span className="panel-resumen-cifras">
            <b>{datos.total_archivos.toLocaleString()}</b> archivos ·{" "}
            <b>{formatearBytes(datos.bytes_totales)}</b> catalogados
          </span>
        )}
        {abierto && (
          <div className="panel-modo" role="group" aria-label="Medir por">
            <button className={modo === "peso" ? "activo" : ""} onClick={() => setModo("peso")}>
              Peso
            </button>
            <button className={modo === "cantidad" ? "activo" : ""} onClick={() => setModo("cantidad")}>
              Cantidad
            </button>
          </div>
        )}
      </div>

      {abierto && error && <div className="panel-error">No se pudo cargar el panel: {error}</div>}

      {abierto && datos && (
        <div className="panel-grid">
          {/* ---- Frío vs caliente ---- */}
          <article className="panel-tarjeta">
            <h3>Frío vs caliente</h3>
            <div className="panel-tarjeta-cuerpo">
              <Dona segmentos={segDecision} activo={activoDec} onActivar={setActivoDec}>
                <div className="dona-centro-frio">
                  <span className="dona-centro-num" style={{ color: COLOR_COLD }}>
                    {pctFrio.toFixed(1)}%
                  </span>
                  <span className="dona-centro-etq">en frío</span>
                </div>
              </Dona>
              <ul className="leyenda">
                {segDecision.map((s) => {
                  const g = s.clave === "HOT" ? hot : s.clave === "COLD" ? cold : sin;
                  return (
                    <li
                      key={s.clave}
                      className={activoDec && activoDec !== s.clave ? "atenuado" : ""}
                      onMouseEnter={() => setActivoDec(s.clave)}
                      onMouseLeave={() => setActivoDec(null)}
                    >
                      <span className="punto" style={{ background: s.color }} />
                      <span className="leyenda-etq">{ETIQUETA_DECISION[s.clave]}</span>
                      <span className="leyenda-val">
                        {g.archivos.toLocaleString()} · {formatearBytes(g.bytes)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
            <p className="panel-nota">
              De lo ya decidido, <b style={{ color: COLOR_COLD }}>{formatearBytes(cold.bytes)}</b> en{" "}
              <b>{cold.archivos.toLocaleString()}</b> archivos van a frío —{" "}
              <b>{pctFrio.toFixed(1)}%</b> del peso. (Quedan{" "}
              {sin.archivos.toLocaleString()} sin decidir.)
            </p>
          </article>

          {/* ---- Por tipo ---- */}
          <article className="panel-tarjeta">
            <h3>Reparto por tipo ({modo === "peso" ? "peso" : "cantidad"})</h3>
            <div className="panel-tarjeta-cuerpo">
              <Dona segmentos={segTipo} activo={activoTipo} onActivar={setActivoTipo}>
                <div className="dona-centro-frio">
                  <span className="dona-centro-num">{segTipo.length}</span>
                  <span className="dona-centro-etq">tipos</span>
                </div>
              </Dona>
              <ul className="leyenda">
                {segTipo.map((s) => (
                  <li
                    key={s.clave}
                    className={activoTipo && activoTipo !== s.clave ? "atenuado" : ""}
                    onMouseEnter={() => setActivoTipo(s.clave)}
                    onMouseLeave={() => setActivoTipo(null)}
                  >
                    <span className="punto" style={{ background: s.color }} />
                    <span className="leyenda-etq">{s.clave === "otros" ? "otros" : etiquetaTipo(s.clave)}</span>
                    <span className="leyenda-val">{fmt(s.valor)}</span>
                  </li>
                ))}
              </ul>
            </div>
            <p className="panel-nota">
              Solo cuenta lo ya tipificado; los {sin.archivos.toLocaleString()} archivos en cola aún
              no tienen tipo asignado.
            </p>
          </article>

          {/* ---- Por estado ---- */}
          <article className="panel-tarjeta panel-tarjeta-ancha">
            <h3>Por estado de la cola</h3>
            <div className="panel-estados">
              {[...datos.por_estado]
                .sort((a, b) => b.archivos - a.archivos)
                .map((g) => (
                  <div key={g.clave} className="chip-estado" title={formatearBytes(g.bytes)}>
                    <span className={`chip-punto estado-${g.clave}`} />
                    <span className="chip-clave">{g.clave}</span>
                    <span className="chip-num">{g.archivos.toLocaleString()}</span>
                    <span className="chip-bytes">{formatearBytes(g.bytes)}</span>
                  </div>
                ))}
            </div>
          </article>
        </div>
      )}
    </section>
  );
}
