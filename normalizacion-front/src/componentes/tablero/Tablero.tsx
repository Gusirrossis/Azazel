// El tablero de Inicio: entrar y darse cuenta de TODO en una pantalla.
// Organizado en bandas semánticas, cada una respondiendo una pregunta:
//   KPIs        → ¿cuánto hay, cuánto terminó, qué está mal? (dos niveles)
//   Flujo       → ¿dónde está parado el trabajo? + ¿qué se conserva vs frío?
//   Calibración → ¿el filtro decide o adivina? + ¿por qué a frío? + errores
//   Composición → ¿de qué está hecho? + ¿cómo va cada disco? + corridas

import { useCallback, useEffect, useState } from "react";
import { formatearBytes, formatearDuracion, obtenerTablero } from "../../api";
import { describirCausa, etiquetaTipo, type Tono } from "../../motivos";
import type { RespuestaTablero } from "../../tipos";
import Dona, { type SegmentoDona } from "../Dona";
import Barras, { type FilaBarra } from "./Barras";
import Histograma from "./Histograma";
import Kpi from "./Kpi";
import Medidor from "./Medidor";

const COLOR_TONO: Record<Tono, string> = {
  ok: "#c9a45c",
  frio: "#6f8fa1",
  gris: "#8e939b",
  alerta: "#c98f3f",
  critico: "#cf6a5c",
};
const COLOR_ESTADO: Record<string, string> = {
  PENDIENTE: "#6b6770",
  PRECALIFICADO: "#8a7344",
  EN_PROCESO: "#c9a45c",
  INDEXADO: "#9aa67d",
  VERIFICADO: "#7da18d",
  HECHO: "#8aa17d",
  COLD: "#6f8fa1",
  ERROR: "#cf6a5c",
};
const ORDEN_ESTADOS = [
  "PENDIENTE", "PRECALIFICADO", "EN_PROCESO", "INDEXADO", "VERIFICADO", "HECHO", "COLD", "ERROR",
];
const COLORES_TIPO = [
  "#c9a45c", "#8aa17d", "#7d96a1", "#b5685f", "#a78bba", "#c2a06b",
  "#6f9a8d", "#9a8f6f", "#b08a5a", "#8e939b",
];

export type DestinoNavegacion = "archivos" | "errores" | "corridas";

// Título de tarjeta con la explicación movida a un tooltip (ⓘ) — los datos
// respiran, el porqué sigue a un hover de distancia.
function Titulo({ children, ayuda }: { children: string; ayuda: string }) {
  return (
    <h3 className="tarjeta-titulo">
      {children}
      <span className="ayuda" title={ayuda} role="img" aria-label="ayuda">
        ⓘ
      </span>
    </h3>
  );
}

export default function Tablero({ onIrA }: { onIrA: (destino: DestinoNavegacion) => void }) {
  const [datos, setDatos] = useState<RespuestaTablero | null>(null);
  const [error, setError] = useState<string | null>(null);
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

  // ---- frío vs caliente (medidor, peso) ----
  const dec = new Map(datos.por_decision.map((g) => [g.clave, g]));
  const hot = dec.get("HOT") ?? { clave: "HOT", archivos: 0, bytes: 0 };
  const cold = dec.get("COLD") ?? { clave: "COLD", archivos: 0, bytes: 0 };

  // ---- embudo por estado (orden del pipeline, no por tamaño) ----
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
      {/* ============ BANDA 1 · estado del sistema ============ */}
      <div className="kpis kpis-primarios">
        <Kpi
          valor={t.archivos.toLocaleString()}
          etiqueta="catalogados"
          sub={`${formatearBytes(t.bytes)} · toda la cola`}
          tono="ok"
          title="Total catalogado en la cola (Postgres): incluye frío, errores y pendientes. El header muestra solo lo buscable en el índice."
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
          tono={t.pendientes + t.en_proceso > 0 ? "alerta" : "gris"}
        />
        <Kpi
          valor={t.errores.toLocaleString()}
          etiqueta={t.errores > 0 ? "⚠ errores" : "errores"}
          sub={t.errores > 0 ? undefined : "dead-letter limpio"}
          tono={t.errores > 0 ? "critico" : "ok"}
          destino={t.errores > 0 ? "ver el porqué" : undefined}
          onClick={t.errores > 0 ? () => onIrA("errores") : undefined}
        />
      </div>
      <div className="kpis kpis-secundarios">
        <Kpi
          tamano="chico"
          valor={t.cold.toLocaleString()}
          etiqueta="en frío (reversible)"
          tono="frio"
          destino="explorar"
          onClick={() => onIrA("archivos")}
        />
        <Kpi
          tamano="chico"
          valor={t.franja_gris.toLocaleString()}
          etiqueta="franja gris"
          sub="el filtro duda"
          tono={t.franja_gris > 0 ? "alerta" : "ok"}
          destino="calibrar"
          onClick={() => onIrA("archivos")}
        />
        <Kpi
          tamano="chico"
          valor={duplicadosEvitados.toLocaleString()}
          etiqueta="duplicados evitados"
          sub={`${t.hash_unicos.toLocaleString()} blobs únicos`}
          tono="ok"
        />
      </div>

      {/* ============ BANDA 2 · flujo y decisión ============ */}
      <div className="tablero-fila fila-2">
        <article className="panel-tarjeta">
          <Titulo ayuda="El camino sano es PENDIENTE → … → HECHO. Lo que se estanca en un estado intermedio es trabajo detenido. Barras en escala raíz para que lo chico no desaparezca.">
            Embudo del pipeline
          </Titulo>
          <Barras filas={filasEstado} vacio="aún no hay nada catalogado" />
        </article>

        <article className="panel-tarjeta">
          <Titulo ayuda="Proporción del PESO ya decidido que se conserva (HOT) vs lo que va a frío reversible (COLD). Los pendientes aún no cuentan aquí.">
            Frío vs caliente
          </Titulo>
          <Medidor
            destacado={{ clave: "COLD", prefijo: "", sufijo: "del peso decidido va a frío reversible" }}
            segmentos={[
              { clave: "HOT", etiqueta: "Caliente (se conserva)", valor: hot.bytes, archivos: hot.archivos, color: COLOR_TONO.ok },
              { clave: "COLD", etiqueta: "Frío (se deja de lado)", valor: cold.bytes, archivos: cold.archivos, color: COLOR_TONO.frio },
            ]}
          />
        </article>
      </div>

      {/* ============ BANDA 3 · calibración (el corazón del tablero) ============ */}
      <article className="panel-tarjeta">
        <Titulo ayuda="Distribución de puntajes en cubetas de 10, contra los umbrales del filtro. Altura logarítmica para que la franja gris (lo que se quiere calibrar) no la aplaste el pico de HOT.">
          ¿El filtro decide o adivina?
        </Titulo>
        <Histograma
          buckets={datos.histograma_puntaje}
          umbralCold={datos.umbral_cold}
          umbralHot={datos.umbral_hot}
        />
        <p className="panel-nota">
          <b>{t.franja_gris.toLocaleString()}</b> archivos caen en la franja gris
          ({datos.umbral_cold}–{datos.umbral_hot - 1}): ahí el filtro no está seguro
          (hoy van a HOT por recall). Es la zona que el T4 va a resolver — si crece, conviene calibrar.
        </p>
      </article>

      <div className="tablero-fila fila-2">
        <article className="panel-tarjeta">
          <Titulo ayuda="Composición del frío por causa, en lenguaje humano. Si aquí aparece algo valioso, la lista blanca necesita ese tipo: pestaña Filtro + «Re-puntuar frío» lo rescata.">
            Por qué se va a frío
          </Titulo>
          <Barras filas={filasCausa(datos.causas_cold, false)} vacio="nada en frío todavía" />
        </article>

        <article className="panel-tarjeta">
          <Titulo ayuda="Errores agrupados por familia. Cada una tiene una acción distinta (pasa el cursor por la barra). El detalle por archivo vive en la pestaña Errores.">
            Errores por familia
          </Titulo>
          <Barras
            filas={filasCausa(datos.causas_error, true)}
            vacio="sin errores 🎉 — el dead-letter está limpio"
          />
        </article>
      </div>

      {/* ============ BANDA 4 · composición y operación ============ */}
      <div className="tablero-fila fila-3">
        <article className="panel-tarjeta">
          <Titulo ayuda="Reparto del PESO por tipo real (top 10). Solo cuenta lo ya tipificado.">
            Reparto por tipo
          </Titulo>
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

        <article className="panel-tarjeta">
          <Titulo ayuda="Cada disco de origen con su avance: archivos catalogados, peso, completados (✓) y errores.">
            Por disco de origen
          </Titulo>
          {datos.discos.length === 0 && <div className="sin-sub">sin discos catalogados</div>}
          {datos.discos.map((d) => (
            <div key={d.disco_id} className="disco-fila" title={d.disco_id}>
              <span className="disco-nombre">{d.disco_id}</span>
              <span className="disco-datos">
                {d.archivos.toLocaleString()} · {formatearBytes(d.bytes)}
                {" · "}
                <b className="kpi-ok">{d.hechos.toLocaleString()} ✓</b>
                {d.errores > 0 && <b className="kpi-critico"> · {d.errores} ⚠</b>}
              </span>
            </div>
          ))}
        </article>

        <article className="panel-tarjeta">
          <Titulo ayuda="Las últimas corridas con su estado y duración. El historial completo está en la pestaña Corridas.">
            Corridas recientes
          </Titulo>
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
