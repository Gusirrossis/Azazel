// Explorador del plano de control (Postgres): TODO lo catalogado — COLD, ERROR,
// pendientes, indexados — con su PORQUÉ en lenguaje humano y agregados por causa.
// Diseñado para calibrar: de un vistazo se ve qué se va a dónde y por qué.
//
// modo="todos"  → pestaña Archivos (filtro por estado libre + franja gris)
// modo="errores" → pestaña Errores (estado=ERROR fijo + reprocesar con guía)

import { useCallback, useEffect, useState } from "react";
import {
  archivosCola,
  estadoPipeline,
  formatearBytes,
  obtenerFiltro,
  reprocesarErrores,
  resumen,
  type FiltrosCola,
} from "../api";
import type { ArchivoCola, FiltroVisible, ResumenCola } from "../tipos";
import { describirCausa, describirError, describirMotivo, esCausaDeError, etiquetaTipo } from "../motivos";
import { formatearEntropia } from "../entropia";
import Senales from "./Senales";
import Veredicto from "./Veredicto";
import UbicacionOriginal from "./UbicacionOriginal";

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

function DetalleFila({
  archivo,
  filtro,
  destinos,
  onCerrar,
}: {
  archivo: ArchivoCola;
  filtro: FiltroVisible | null;
  destinos: Record<string, string> | null;
  onCerrar: () => void;
}) {
  const tier = typeof archivo.senales?.tier === "string" ? archivo.senales.tier : null;
  return (
    <aside className="detalle">
      <div className="detalle-encabezado">
        <h2 title={archivo.nombre}>{archivo.nombre}</h2>
        <button className="cerrar" onClick={onCerrar} aria-label="Cerrar">
          ×
        </button>
      </div>

      <Veredicto
        estado={archivo.estado}
        rutaDecision={archivo.ruta_decision}
        puntaje={archivo.puntaje}
        motivo={archivo.motivo}
        errorMotivo={archivo.error_motivo}
        intentos={archivo.intentos}
        tier={tier}
        umbralCold={filtro?.umbral_cold}
        umbralHot={filtro?.umbral_hot}
      />

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

      <h3>Ficha técnica</h3>
      {[
        ["Tipo real", archivo.tipo_real ? `${etiquetaTipo(archivo.tipo_real)} (${archivo.tipo_real})` : null],
        ["Extensión", archivo.extension],
        ["Ruta", archivo.ruta],
        ["Disco", archivo.disco_id],
        ["Tamaño", formatearBytes(archivo.tamano)],
        ["Modificado", new Date(archivo.mtime).toLocaleString()],
        ["Prioridad en cola", archivo.prioridad],
        ["Versión del filtro", archivo.version_filtro],
        ["Motivo (código)", archivo.motivo],
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

      <UbicacionOriginal
        hash={archivo.hash_contenido}
        rutaDecision={archivo.ruta_decision}
        destinos={destinos}
      />
    </aside>
  );
}

export default function ExploradorCola({ modo }: { modo: "todos" | "errores" }) {
  const esErrores = modo === "errores";
  const [estadoSel, setEstadoSel] = useState<string | null>(esErrores ? "ERROR" : null);
  const [nombre, setNombre] = useState("");
  const [extension, setExtension] = useState("");
  const [causaSel, setCausaSel] = useState<string | null>(null);
  const [franjaGris, setFranjaGris] = useState(false);
  const [filas, setFilas] = useState<ArchivoCola[]>([]);
  const [total, setTotal] = useState(0);
  const [resumenCola, setResumenCola] = useState<ResumenCola | null>(null);
  const [conteosEstado, setConteosEstado] = useState<Record<string, number>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const [seleccionado, setSeleccionado] = useState<ArchivoCola | null>(null);
  const [filtro, setFiltro] = useState<FiltroVisible | null>(null);
  const [destinos, setDestinos] = useState<Record<string, string> | null>(null);

  // Umbrales del filtro vigente (colorear entropía, zonas del gauge, franja gris)
  // y destinos del almacén (ubicación del original en el detalle).
  useEffect(() => {
    obtenerFiltro()
      .then((r) => setFiltro(r.efectivo))
      .catch(() => setFiltro(null));
    resumen()
      .then((r) => {
        const conteos: Record<string, number> = {};
        for (const g of r.por_estado) conteos[g.clave] = g.archivos;
        setConteosEstado(conteos);
      })
      .catch(() => setConteosEstado({}));
    estadoPipeline()
      .then((e) => setDestinos(e.destinos))
      .catch(() => setDestinos(null));
  }, []);

  const construirFiltros = useCallback((): FiltrosCola => {
    const f: FiltrosCola = { limite: 50 };
    if (esErrores) {
      f.estado = "ERROR";
      if (causaSel) f.error_motivo = causaSel;
    } else {
      if (estadoSel) f.estado = estadoSel;
      if (causaSel) {
        if (esCausaDeError(causaSel)) f.error_motivo = causaSel;
        else f.motivo = causaSel;
      }
      if (franjaGris) {
        f.puntaje_min = filtro?.umbral_cold ?? 35;
        f.puntaje_max = (filtro?.umbral_hot ?? 65) - 1;
      }
    }
    if (nombre.trim()) f.nombre = nombre.trim();
    if (extension.trim())
      f.extension = extension.trim().startsWith(".") ? extension.trim() : `.${extension.trim()}`;
    return f;
  }, [esErrores, estadoSel, causaSel, franjaGris, filtro, nombre, extension]);

  const cargar = useCallback(
    (pagina: string | null = null) => {
      setCargando(true);
      setError(null);
      archivosCola({ ...construirFiltros(), cursor: pagina ?? undefined })
        .then((r) => {
          setFilas((previas) => (pagina ? [...previas, ...r.archivos] : r.archivos));
          setTotal(r.total);
          setCursor(r.cursor);
          if (!pagina) setResumenCola(r.resumen);
        })
        .catch((e) => setError(String(e)))
        .finally(() => setCargando(false));
    },
    [construirFiltros],
  );

  // Debounce: los campos de texto no disparan una consulta por tecla
  useEffect(() => {
    const t = window.setTimeout(() => cargar(), 300);
    return () => window.clearTimeout(t);
  }, [cargar]);

  const reprocesar = (clave?: string) => {
    const patron = clave ? `${clave}%` : causaSel ? `${causaSel}%` : undefined;
    const detalle = patron ? `los de la familia «${patron.slice(0, -1)}»` : "TODOS los errores";
    if (!window.confirm(`¿Devolver ${detalle} a su etapa de origen?`)) return;
    reprocesarErrores(patron)
      .then((r) => {
        const destinos = Object.entries(r.destinos)
          .map(([estado, n]) => `${n} → ${estado}`)
          .join(", ");
        setAviso(
          r.total === 0
            ? "no había errores que reprocesar con ese filtro"
            : `${r.total} archivos re-encolados (${destinos}). Se procesarán en la SIGUIENTE corrida — re-indexa la carpeta para drenarlos.`,
        );
        setCausaSel(null);
        cargar();
      })
      .catch((e) => setError(String(e)));
  };

  const causas = resumenCola?.por_causa ?? [];
  const tipos = (resumenCola?.por_tipo ?? []).slice(0, 6);

  return (
    <section className="explorador">
      {/* ---- composición: POR QUÉ está aquí lo filtrado (el dato de calibración) ---- */}
      {causas.length > 0 && (
        <div className="causas">
          {causas.map((c) => {
            const info = describirCausa(c.clave, esErrores);
            const activa = causaSel === c.clave;
            return (
              <button
                key={c.clave}
                className={`causa tono-${info.tono}${activa ? " activa" : ""}`}
                title={`${info.explicacion}${info.accion ? `\n⮕ ${info.accion}` : ""}`}
                onClick={() => setCausaSel(activa ? null : c.clave)}
              >
                <span className="causa-numero">{c.archivos.toLocaleString()}</span>
                <span className="causa-titulo">{info.titulo}</span>
                <span className="causa-bytes">{formatearBytes(c.bytes)}</span>
              </button>
            );
          })}
        </div>
      )}

      <div className="explorador-filtros">
        {!esErrores && (
          <div className="explorador-estados">
            <button
              className={estadoSel === null && !franjaGris ? "chip-estado activo" : "chip-estado"}
              onClick={() => {
                setEstadoSel(null);
                setFranjaGris(false);
              }}
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
                {conteosEstado[e] !== undefined && (
                  <span className="chip-num">{conteosEstado[e].toLocaleString()}</span>
                )}
              </button>
            ))}
            <button
              className={franjaGris ? "chip-estado activo franja" : "chip-estado franja"}
              title="Puntaje entre los umbrales frío/HOT: donde el filtro decide con menos certeza — la zona a calibrar (y la que resolverá el T4)"
              onClick={() => setFranjaGris(!franjaGris)}
            >
              <span className="chip-clave">
                ◐ franja gris {filtro ? `${filtro.umbral_cold}–${filtro.umbral_hot - 1}` : ""}
              </span>
            </button>
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
          {esErrores && (
            <button className="secundario" disabled={total === 0} onClick={() => reprocesar()}>
              ↻ Reprocesar {causaSel ? `«${causaSel}» ` : ""}({total.toLocaleString()})
            </button>
          )}
        </div>
      </div>

      {error && <div className="banner-error">{error}</div>}
      {aviso && (
        <div className="banner-aviso">
          {aviso}{" "}
          <button className="enlace" onClick={() => setAviso(null)}>
            ×
          </button>
        </div>
      )}

      <div className="resultados-resumen explorador-resumen">
        <span>
          <b>{total.toLocaleString()}</b>
          {esErrores
            ? " archivos en dead-letter — cada tarjeta de arriba es una familia con su acción"
            : " archivos en la cola (incluye lo que el índice no ve: frío, errores, pendientes)"}
        </span>
        {tipos.length > 0 && (
          <span className="tipos-resumen">
            {tipos.map((t) => (
              <span key={t.clave} className="tipo-mini" title={t.clave}>
                {etiquetaTipo(t.clave)} <b>{t.archivos.toLocaleString()}</b>
              </span>
            ))}
          </span>
        )}
      </div>

      <table>
        <thead>
          <tr>
            <th>Nombre</th>
            {!esErrores && <th>Estado</th>}
            <th>{esErrores ? "Qué pasó" : "Por qué está ahí"}</th>
            <th>Puntaje</th>
            <th>Entropía (0-1)</th>
            <th>Tipo real</th>
            {esErrores && <th>Intentos</th>}
            <th>Tamaño</th>
          </tr>
        </thead>
        <tbody>
          {filas.map((a) => {
            const e = entropiaDe(a);
            const info = esErrores || a.estado === "ERROR"
              ? (describirError(a.error_motivo) ?? describirMotivo(a.motivo))
              : describirMotivo(a.motivo);
            const crudo = (esErrores ? a.error_motivo : a.motivo) ?? "";
            return (
              <tr key={a.archivo_id} onClick={() => setSeleccionado(a)}>
                <td className="celda-nombre" title={a.ruta}>
                  {a.nombre}
                  <div className="celda-ruta">{a.ruta}</div>
                </td>
                {!esErrores && (
                  <td>
                    <span className="celda-estado">
                      <span className={`chip-punto estado-${a.estado}`} />
                      {a.estado}
                    </span>
                  </td>
                )}
                <td>
                  <span className={`chip-motivo tono-${info.tono}`} title={`${info.explicacion}${crudo ? `\n[${crudo}]` : ""}`}>
                    {info.titulo}
                  </span>
                </td>
                <td>
                  {a.puntaje !== null ? (
                    <span
                      className={
                        filtro && a.puntaje >= filtro.umbral_hot
                          ? "puntaje-hot"
                          : filtro && a.puntaje < filtro.umbral_cold
                            ? "puntaje-frio"
                            : "puntaje-gris"
                      }
                      title={
                        filtro
                          ? `umbrales: <${filtro.umbral_cold} frío · ≥${filtro.umbral_hot} HOT`
                          : undefined
                      }
                    >
                      {a.puntaje}
                    </span>
                  ) : (
                    <span className="celda-na">—</span>
                  )}
                </td>
                <td>
                  {e !== null ? (
                    <span
                      className={
                        filtro && e < filtro.entropia_texto_max
                          ? "entropia-texto"
                          : filtro && e > filtro.entropia_comprimido_min
                            ? "entropia-alta"
                            : "entropia-media"
                      }
                    >
                      {formatearEntropia(e)}
                    </span>
                  ) : (
                    <span className="celda-na" title="decidido en T0/T1: no hizo falta medir entropía">
                      —
                    </span>
                  )}
                </td>
                <td className="celda-tipo" title={a.tipo_real ?? ""}>
                  {a.tipo_real ? etiquetaTipo(a.tipo_real) : "—"}
                </td>
                {esErrores && <td>{a.intentos}</td>}
                <td>{formatearBytes(a.tamano)}</td>
              </tr>
            );
          })}
          {filas.length === 0 && !cargando && (
            <tr>
              <td colSpan={8} className="vacio">
                {esErrores ? "sin errores 🎉 — el dead-letter está limpio" : "sin archivos con esos filtros"}
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
          destinos={destinos}
          onCerrar={() => setSeleccionado(null)}
        />
      )}
    </section>
  );
}
