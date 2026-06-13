// El tablero de Inicio: entrar y darse cuenta de TODO en una pantalla.
// Cada bloque responde una pregunta concreta:
//   KPIs        → ¿cuánto hay, cuánto terminó, qué está mal?
//   Embudo      → ¿dónde está parado el trabajo en el pipeline?
//   Frío/HOT    → ¿qué proporción del peso se conserva vs se deja de lado?
//   Causas frío → ¿POR QUÉ se está yendo a frío? (calibración de lista blanca)
//   Errores     → ¿qué familias de fallo hay y de qué tamaño?
//   Histograma  → ¿el filtro decide con certeza o adivina? (franja gris)
//   Tipos       → ¿de qué está hecho el corpus?
//   Discos      → ¿cómo va cada origen?
//   Corridas    → ¿qué se ha ejecutado últimamente?

import { useCallback, useEffect, useState } from "react";
import { formatearBytes, formatearDuracion, obtenerTablero } from "../../api";
import { describirCausa, etiquetaTipo, type Tono } from "../../motivos";
import type { RespuestaTablero } from "../../tipos";
import Dona, { type SegmentoDona } from "../Dona";
import Barras, { type FilaBarra } from "./Barras";
import Histograma from "./Histograma";
import Kpi from "./Kpi";

const COLOR_TONO: Record<Tono, string> = {
  ok: "#c9a45c",
  frio: "#6f8fa1",
  gris: "#8e939b",
  alerta: "#c98f3f",
  critico: "#b5685f",
};
const COLOR_ESTADO: Record<string, string> = {
  PENDIENTE: "#6b6770",
  PRECALIFICADO: "#8a7344",
  EN_PROCESO: "#c9a45c",
  INDEXADO: "#9aa67d",
  VERIFICADO: "#7da18d",
  HECHO: "#8aa17d",
  COLD: "#6f8fa1",
  ERROR: "#b5685f",
};
const ORDEN_ESTADOS = [
  "PENDIENTE", "PRECALIFICADO", "EN_PROCESO", "INDEXADO", "VERIFICADO", "HECHO", "COLD", "ERROR",
];
const COLORES_TIPO = [
  "#c9a45c", "#8aa17d", "#7d96a1", "#b5685f", "#a78bba", "#c2a06b",
  "#6f9a8d", "#9a8f6f", "#b08a5a", "#8e939b",
];

export type DestinoNavegacion = "archivos" | "errores" | "corridas";

export default function Tablero({ onIrA }: { onIrA: (destino: DestinoNavegacion) => void }) {
  const [datos, setDatos] = useState<RespuestaTablero | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activoDec, setActivoDec] = useState<string | null>(null);
  const [activoTipo, setActivoTipo] = useState<string | null>(null);

  const cargar = useCallback(() => {
    obtenerTablero()
      .then((r) => {
        setDatos(r);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    cargar();
    const id = window.setInterval(cargar, 12000);
    return () => window.clearInterval(id);
  }, [cargar]);

  if (error) return <div className="banner-error">No se pudo cargar el tablero: {error}</div>;
  if (!datos) return <div className="sin-sub">cargando tablero…</div>;

  const t = datos.totales;
  const pctHechos = t.archivos > 0 ? (t.hechos / t.archivos) * 100 : 0;
  const duplicadosEvitados = t.con_hash - t.hash_unicos;

  // ---- frío vs caliente (peso, sobre lo decidido) ----
  const dec = new Map(datos.por_decision.map((g) => [g.clave, g]));
  const hot = dec.get("HOT") ?? { clave: "HOT", archivos: 0, bytes: 0 };
  const cold = dec.get("COLD") ?? { clave: "COLD", archivos: 0, bytes: 0 };
  const pesoDecidido = hot.bytes + cold.bytes;
  const pctFrio = pesoDecidido > 0 ? (cold.bytes / pesoDecidido) * 100 : 0;
  const segDecision: SegmentoDona[] = [
    { clave: "HOT", valor: hot.bytes, color: COLOR_TONO.ok },
    { clave: "COLD", valor: cold.bytes, color: COLOR_TONO.frio },
  ];

  // ---- embudo por estado ----
  const porEstado = new Map(datos.por_estado.map((g) => [g.clave, g]));
  const filasEstado: FilaBarra[] = ORDEN_ESTADOS.filter((e) => porEstado.has(e)).map((e) => ({
    ...porEstado.get(e)!,
    etiqueta: e,
    color: COLOR_ESTADO[e] ?? "#8e939b",
  }));

  // ---- causas (frío y errores) con diccionario humano ----
  const filasCausa = (grupos: typeof datos.causas_cold, esError: boolean): FilaBarra[] =>
    grupos.map((g) => {
      const info = describirCausa(g.clave, esError);
      return {
        ...g,
        etiqueta: info.titulo,
        color: COLOR_TONO[info.tono],
        tooltip: `${info.explicacion}${info.accion ? `\n⮕ ${info.accion}` : ""}\n[${g.clave}]`,
      };
    });

  // ---- tipos (top por peso) ----
  const segTipo: SegmentoDona[] = datos.por_tipo.map((g, i) => ({
    clave: g.clave,
    valor: g.bytes,
    color: COLORES_TIPO[i % COLORES_TIPO.length],
  }));

  return (
    <section className="tablero">
      {/* ---------- KPIs: el estado del sistema en una línea ---------- */}
      <div className="kpis">
        <Kpi
          valor={t.archivos.toLocaleString()}
          etiqueta="catalogados"
          sub={formatearBytes(t.bytes)}
          tono="ok"
        />
        <Kpi
          valor={t.hechos.toLocaleString()}
          etiqueta="completados ✓"
          sub={`${pctHechos.toFixed(1)}% del total`}
          tono="ok"
        />
        <Kpi
          valor={(t.pendientes + t.en_proceso).toLocaleString()}
          etiqueta="en la cola"
          sub={`${t.pendientes.toLocaleString()} por evaluar · ${t.en_proceso.toLocaleString()} en proceso`}
          tono="gris"
        />
        <Kpi
          valor={t.cold.toLocaleString()}
          etiqueta="en frío (reversible)"
          sub={`${pctFrio.toFixed(1)}% del peso decidido`}
          tono="frio"
          onClick={() => onIrA("archivos")}
        />
        <Kpi
          valor={t.errores.toLocaleString()}
          etiqueta="errores"
          sub={t.errores > 0 ? "click para ver el porqué de cada uno" : "dead-letter limpio"}
          tono={t.errores > 0 ? "critico" : "ok"}
          onClick={() => onIrA("errores")}
        />
        <Kpi
          valor={t.franja_gris.toLocaleString()}
          etiqueta="franja gris"
          sub="donde el filtro duda — a calibrar"
          tono={t.franja_gris > 0 ? "alerta" : "ok"}
          onClick={() => onIrA("archivos")}
        />
        <Kpi
          valor={duplicadosEvitados.toLocaleString()}
          etiqueta="duplicados evitados"
          sub={`${t.hash_unicos.toLocaleString()} blobs únicos en el almacén`}
          tono="ok"
        />
      </div>

      <div className="tablero-grid">
        {/* ---------- el pipeline: dónde está parado el trabajo ---------- */}
        <article className="panel-tarjeta">
          <h3>Embudo del pipeline</h3>
          <Barras filas={filasEstado} vacio="aún no hay nada catalogado" />
          <p className="panel-nota">
            El camino sano es PENDIENTE → … → HECHO. Lo que se estanca en un estado
            intermedio es trabajo detenido; ERROR y COLD tienen su tarjeta propia.
          </p>
        </article>

        {/* ---------- frío vs caliente ---------- */}
        <article className="panel-tarjeta">
          <h3>Frío vs caliente (peso)</h3>
          <div className="panel-tarjeta-cuerpo">
            <Dona segmentos={segDecision} activo={activoDec} onActivar={setActivoDec}>
              <div className="dona-centro-frio">
                <span className="dona-centro-num" style={{ color: COLOR_TONO.frio }}>
                  {pctFrio.toFixed(1)}%
                </span>
                <span className="dona-centro-etq">en frío</span>
              </div>
            </Dona>
            <ul className="leyenda">
              <li onMouseEnter={() => setActivoDec("HOT")} onMouseLeave={() => setActivoDec(null)}>
                <span className="punto" style={{ background: COLOR_TONO.ok }} />
                <span className="leyenda-etq">Caliente (se conserva)</span>
                <span className="leyenda-val">
                  {hot.archivos.toLocaleString()} · {formatearBytes(hot.bytes)}
                </span>
              </li>
              <li onMouseEnter={() => setActivoDec("COLD")} onMouseLeave={() => setActivoDec(null)}>
                <span className="punto" style={{ background: COLOR_TONO.frio }} />
                <span className="leyenda-etq">Frío (se deja de lado)</span>
                <span className="leyenda-val">
                  {cold.archivos.toLocaleString()} · {formatearBytes(cold.bytes)}
                </span>
              </li>
            </ul>
          </div>
        </article>

        {/* ---------- por qué se va a frío: calibración de la lista ---------- */}
        <article className="panel-tarjeta">
          <h3>Por qué se va a frío</h3>
          <Barras
            filas={filasCausa(datos.causas_cold, false)}
            vacio="nada en frío todavía"
          />
          <p className="panel-nota">
            Si aquí aparece algo valioso, la lista blanca necesita ese tipo —
            pestaña Filtro + «Re-puntuar frío» lo rescata.
          </p>
        </article>

        {/* ---------- errores por familia ---------- */}
        <article className="panel-tarjeta">
          <h3>Errores por familia</h3>
          <Barras
            filas={filasCausa(datos.causas_error, true)}
            vacio="sin errores 🎉 — el dead-letter está limpio"
          />
          <p className="panel-nota">
            Cada familia tiene acción distinta (el tooltip la dice); el detalle por
            archivo vive en la pestaña Errores.
          </p>
        </article>

        {/* ---------- histograma: certeza del filtro ---------- */}
        <article className="panel-tarjeta panel-tarjeta-ancha">
          <h3>
            Distribución de puntajes — ¿el filtro decide o adivina?
          </h3>
          <Histograma
            buckets={datos.histograma_puntaje}
            umbralCold={datos.umbral_cold}
            umbralHot={datos.umbral_hot}
          />
          <p className="panel-nota">
            <b>{t.franja_gris.toLocaleString()}</b> archivos caen en la franja gris
            ({datos.umbral_cold}–{datos.umbral_hot - 1}): ahí el filtro no está seguro
            (hoy van a HOT por recall). Esa zona es la que el etiquetado del T4 va a
            resolver — si crece, conviene calibrar.
          </p>
        </article>

        {/* ---------- de qué está hecho el corpus ---------- */}
        <article className="panel-tarjeta">
          <h3>Reparto por tipo (peso)</h3>
          <div className="panel-tarjeta-cuerpo">
            <Dona segmentos={segTipo} activo={activoTipo} onActivar={setActivoTipo}>
              <div className="dona-centro-frio">
                <span className="dona-centro-num">{segTipo.length}</span>
                <span className="dona-centro-etq">tipos</span>
              </div>
            </Dona>
            <ul className="leyenda">
              {datos.por_tipo.map((g, i) => (
                <li
                  key={g.clave}
                  className={activoTipo && activoTipo !== g.clave ? "atenuado" : ""}
                  onMouseEnter={() => setActivoTipo(g.clave)}
                  onMouseLeave={() => setActivoTipo(null)}
                  title={g.clave}
                >
                  <span className="punto" style={{ background: COLORES_TIPO[i % COLORES_TIPO.length] }} />
                  <span className="leyenda-etq">{etiquetaTipo(g.clave)}</span>
                  <span className="leyenda-val">
                    {g.archivos.toLocaleString()} · {formatearBytes(g.bytes)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </article>

        {/* ---------- discos ---------- */}
        <article className="panel-tarjeta">
          <h3>Por disco de origen</h3>
          {datos.discos.length === 0 && <div className="sin-sub">sin discos catalogados</div>}
          {datos.discos.map((d) => (
            <div key={d.disco_id} className="disco-fila" title={d.disco_id}>
              <span className="disco-nombre">{d.disco_id}</span>
              <span className="disco-datos">
                {d.archivos.toLocaleString()} archivos · {formatearBytes(d.bytes)}
                {" · "}
                <b className="kpi-ok">{d.hechos.toLocaleString()} ✓</b>
                {d.errores > 0 && <b className="kpi-critico"> · {d.errores} en error</b>}
              </span>
            </div>
          ))}
        </article>

        {/* ---------- corridas recientes ---------- */}
        <article className="panel-tarjeta">
          <h3>Corridas recientes</h3>
          {datos.corridas.length === 0 && (
            <div className="sin-sub">aún no hay corridas — lanza una con «Indexar carpeta…»</div>
          )}
          {datos.corridas.map((c) => (
            <div key={c.id} className="disco-fila" title={c.ruta}>
              <span className="disco-nombre">{c.ruta.split(/[\\/]/).pop() ?? c.ruta}</span>
              <span className="disco-datos">
                {c.estado === "COMPLETADA" && <span className="chip ok">completada</span>}
                {c.estado === "EN_CURSO" && <span className="chip medio">en curso</span>}
                {c.estado === "FALLIDA" && <span className="chip bajo">fallida</span>}
                {c.duracion_s !== null && <i> {formatearDuracion(c.duracion_s)}</i>}
              </span>
            </div>
          ))}
          <button className="enlace" onClick={() => onIrA("corridas")}>
            historial completo →
          </button>
        </article>
      </div>
    </section>
  );
}
