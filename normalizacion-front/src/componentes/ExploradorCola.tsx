// Explorador del plano de control (Postgres): TODO lo catalogado — COLD, ERROR,
// pendientes, indexados — con puntaje, motivo y señales (entropía). Es la vista
// para auditar si el filtro está decidiendo bien.
//
// modo="todos"  → pestaña Archivos (filtro por estado libre)
// modo="errores" → pestaña Errores (estado=ERROR fijo + botón reprocesar)

import { useCallback, useEffect, useState } from "react";
import {
  archivosCola,
  formatearBytes,
  obtenerFiltro,
  reprocesarErrores,
  type FiltrosCola,
} from "../api";
import type { ArchivoCola, FiltroVisible } from "../tipos";
import Senales from "./Senales";

const ESTADOS = [
  "PENDIENTE",
  "PRECALIFICADO",
  "EN_PROCESO",
  "INDEXADO",
  "VERIFICADO",
  "HECHO",
  "COLD",
  "ERROR",
];

function entropiaDe(a: ArchivoCola): number | null {
  const e = a.senales?.entropia;
  return typeof e === "number" ? e : null;
}

function claseEntropia(e: number, filtro: FiltroVisible | null): string {
  const textoMax = filtro?.entropia_texto_max ?? 3.5;
  const comprimidoMin = filtro?.entropia_comprimido_min ?? 7.5;
  if (e < textoMax) return "entropia-texto";
  if (e > comprimidoMin) return "entropia-alta";
  return "entropia-media";
}

function DetalleFila({
  archivo,
  filtro,
  onCerrar,
}: {
  archivo: ArchivoCola;
  filtro: FiltroVisible | null;
  onCerrar: () => void;
}) {
  return (
    <aside className="detalle">
      <div className="detalle-encabezado">
        <h2 title={archivo.nombre}>{archivo.nombre}</h2>
        <button className="cerrar" onClick={onCerrar} aria-label="Cerrar">
          ×
        </button>
      </div>
      {[
        ["Estado", archivo.estado],
        ["Decisión", archivo.ruta_decision],
        ["Puntaje del filtro", archivo.puntaje],
        ["Motivo", archivo.motivo],
        ["Error", archivo.error_motivo],
        ["Intentos", archivo.intentos > 0 ? archivo.intentos : null],
        ["Prioridad en cola", archivo.prioridad],
        ["Tipo real", archivo.tipo_real],
        ["Extensión", archivo.extension],
        ["Ruta", archivo.ruta],
        ["Disco", archivo.disco_id],
        ["Tamaño", formatearBytes(archivo.tamano)],
        ["Modificado", new Date(archivo.mtime).toLocaleString()],
        ["Versión del filtro", archivo.version_filtro],
        ["Hash (sha256)", archivo.hash_contenido ? `${archivo.hash_contenido.slice(0, 16)}…` : null],
        ["Actualizado", new Date(archivo.actualizado_en).toLocaleString()],
      ]
        .filter(([, v]) => v !== null && v !== undefined && v !== "")
        .map(([etiqueta, valor]) => (
          <div key={String(etiqueta)} className="detalle-fila">
            <span className="detalle-etiqueta">{etiqueta}</span>
            <span className="detalle-valor">{String(valor)}</span>
          </div>
        ))}
      {archivo.senales && Object.keys(archivo.senales).length > 0 && (
        <>
          <h3>Señales del filtro</h3>
          <Senales
            senales={archivo.senales}
            entropiaTextoMax={filtro?.entropia_texto_max}
            entropiaComprimidoMin={filtro?.entropia_comprimido_min}
          />
        </>
      )}
    </aside>
  );
}

export default function ExploradorCola({ modo }: { modo: "todos" | "errores" }) {
  const esErrores = modo === "errores";
  const [estadoSel, setEstadoSel] = useState<string | null>(esErrores ? "ERROR" : null);
  const [nombre, setNombre] = useState("");
  const [extension, setExtension] = useState("");
  const [motivo, setMotivo] = useState("");
  const [filas, setFilas] = useState<ArchivoCola[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const [seleccionado, setSeleccionado] = useState<ArchivoCola | null>(null);
  const [filtro, setFiltro] = useState<FiltroVisible | null>(null);

  // Los umbrales de entropía (para colorear) salen del filtro vigente
  useEffect(() => {
    obtenerFiltro()
      .then((r) => setFiltro(r.efectivo))
      .catch(() => setFiltro(null));
  }, []);

  const construirFiltros = useCallback((): FiltrosCola => {
    const f: FiltrosCola = { limite: 50 };
    if (esErrores) {
      f.estado = "ERROR";
      if (motivo.trim()) f.error_motivo = motivo.trim();
    } else {
      if (estadoSel) f.estado = estadoSel;
      if (motivo.trim()) f.motivo = motivo.trim();
    }
    if (nombre.trim()) f.nombre = nombre.trim();
    if (extension.trim()) f.extension = extension.trim().startsWith(".") ? extension.trim() : `.${extension.trim()}`;
    return f;
  }, [esErrores, estadoSel, motivo, nombre, extension]);

  const cargar = useCallback(
    (pagina: string | null = null) => {
      setCargando(true);
      setError(null);
      archivosCola({ ...construirFiltros(), cursor: pagina ?? undefined })
        .then((r) => {
          setFilas((previas) => (pagina ? [...previas, ...r.archivos] : r.archivos));
          setTotal(r.total);
          setCursor(r.cursor);
        })
        .catch((e) => setError(String(e)))
        .finally(() => setCargando(false));
    },
    [construirFiltros],
  );

  useEffect(() => {
    cargar();
  }, [cargar]);

  const reprocesar = () => {
    const patron = motivo.trim() ? `${motivo.trim()}%` : undefined;
    const detalle = patron ? `con motivo "${patron}"` : "TODOS";
    if (!window.confirm(`¿Devolver ${detalle} los errores a su etapa de origen?`)) return;
    reprocesarErrores(patron)
      .then((r) => {
        const destinos = Object.entries(r.destinos)
          .map(([estado, n]) => `${n} → ${estado}`)
          .join(", ");
        setAviso(
          r.total === 0
            ? "no había errores que reprocesar"
            : `${r.total} archivos re-encolados (${destinos}). Se procesarán en la SIGUIENTE corrida — re-indexa la carpeta para drenarlos.`,
        );
        cargar();
      })
      .catch((e) => setError(String(e)));
  };

  return (
    <section className="explorador">
      <div className="explorador-filtros">
        {!esErrores && (
          <div className="explorador-estados">
            <button
              className={estadoSel === null ? "chip-estado activo" : "chip-estado"}
              onClick={() => setEstadoSel(null)}
            >
              <span className="chip-clave">Todos</span>
            </button>
            {ESTADOS.map((e) => (
              <button
                key={e}
                className={estadoSel === e ? "chip-estado activo" : "chip-estado"}
                onClick={() => setEstadoSel(estadoSel === e ? null : e)}
              >
                <span className={`chip-punto estado-${e}`} />
                <span className="chip-clave">{e}</span>
              </button>
            ))}
          </div>
        )}
        <div className="explorador-campos">
          <input
            value={nombre}
            placeholder="nombre contiene…"
            onChange={(e) => setNombre(e.target.value)}
          />
          <input
            value={extension}
            placeholder="extensión (.txt)"
            className="campo-corto"
            onChange={(e) => setExtension(e.target.value)}
          />
          <input
            value={motivo}
            placeholder={esErrores ? "error contiene… (agotado, corrupto…)" : "motivo empieza por…"}
            onChange={(e) => setMotivo(e.target.value)}
          />
          {esErrores && (
            <button className="secundario" disabled={total === 0} onClick={reprocesar}>
              ↻ Reprocesar ({total.toLocaleString()})
            </button>
          )}
        </div>
      </div>

      {error && <div className="banner-error">{error}</div>}
      {aviso && (
        <div className="banner-aviso">
          {aviso} <button className="enlace" onClick={() => setAviso(null)}>×</button>
        </div>
      )}

      <div className="resultados-resumen">
        {total.toLocaleString()} archivos
        {esErrores
          ? " en ERROR (dead-letter) — cada uno con su porqué"
          : " en la cola (incluye lo que el índice no ve: frío, errores, pendientes)"}
      </div>

      <table>
        <thead>
          <tr>
            <th>Nombre</th>
            {!esErrores && <th>Estado</th>}
            {!esErrores && <th>Decisión</th>}
            <th>Puntaje</th>
            <th>Entropía</th>
            <th>Tipo real</th>
            {esErrores ? <th>Error (porqué)</th> : <th>Motivo</th>}
            {esErrores && <th>Intentos</th>}
            <th>Tamaño</th>
          </tr>
        </thead>
        <tbody>
          {filas.map((a) => {
            const e = entropiaDe(a);
            return (
              <tr key={a.archivo_id} onClick={() => setSeleccionado(a)}>
                <td className="celda-nombre" title={a.ruta}>
                  {a.nombre}
                </td>
                {!esErrores && <td>{a.estado}</td>}
                {!esErrores && <td>{a.ruta_decision ?? "—"}</td>}
                <td>{a.puntaje ?? "—"}</td>
                <td>
                  {e !== null ? (
                    <span className={claseEntropia(e, filtro)}>{e.toFixed(2)}</span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="celda-tipo">{a.tipo_real ?? "—"}</td>
                <td className="celda-tipo" title={(esErrores ? a.error_motivo : a.motivo) ?? ""}>
                  {(esErrores ? a.error_motivo : a.motivo) ?? "—"}
                </td>
                {esErrores && <td>{a.intentos}</td>}
                <td>{formatearBytes(a.tamano)}</td>
              </tr>
            );
          })}
          {filas.length === 0 && !cargando && (
            <tr>
              <td colSpan={9} className="sin-sub">
                {esErrores ? "sin errores 🎉" : "sin archivos con esos filtros"}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {cursor && (
        <button className="cargar-mas" disabled={cargando} onClick={() => cargar(cursor)}>
          {cargando ? "cargando…" : `cargar más (${filas.length} de ${total.toLocaleString()})`}
        </button>
      )}

      {seleccionado && (
        <DetalleFila
          archivo={seleccionado}
          filtro={filtro}
          onCerrar={() => setSeleccionado(null)}
        />
      )}
    </section>
  );
}
