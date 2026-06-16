// Pestaña Entidades (Fase 2): personas canónicas resueltas + RECETAS dinámicas.
//
// Dos modos:
//  · Personas — lista deduplicada; el detalle permite VER LA MISMA PERSONA bajo
//    cualquier receta (distintas estructuras/valores por sistema consumidor) y
//    ejecutar contingencias (desactivar/reactivar — soft-delete LFPDPPP).
//  · Recetas — gestión: listar, ver, editar y crear recetas de proyección (la
//    estructura de salida es DATO editable, no código).

import { useCallback, useEffect, useState } from "react";
import {
  borrarReceta, entidadActivo, entidadProyectar, entidades as pedirEntidades,
  entidadesStats, exportarEntidades, guardarReceta, recetas as pedirRecetas,
} from "../api";
import type { Entidad, EstadisticasEntidades, Receta } from "../tipos";

const ANCLA_ETQ: Record<string, string> = {
  curp: "CURP", rfc: "RFC", email: "correo", telefono: "teléfono",
};

// ---------------------------------------------------------------- detalle de persona

function DetalleEntidad({
  e, recetasDisp, onCerrar, onCambio,
}: {
  e: Entidad;
  recetasDisp: Receta[];
  onCerrar: () => void;
  onCambio: () => void;
}) {
  const c = e.campos ?? {};
  const nom = c.nombre ?? {};
  const norm = c.normalizados ?? {};
  const [recetaSel, setRecetaSel] = useState(recetasDisp[0]?.clave ?? "");
  const [proyectado, setProyectado] = useState<Record<string, unknown> | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);

  // Mantén una receta válida seleccionada aunque la lista llegue tarde o cambie.
  useEffect(() => {
    if (recetasDisp.length && !recetasDisp.some((r) => r.clave === recetaSel)) {
      setRecetaSel(recetasDisp[0].clave);
    }
  }, [recetasDisp, recetaSel]);

  useEffect(() => {
    if (!recetaSel) { setProyectado(null); return; }
    entidadProyectar(e.entidad_id, recetaSel)
      .then((r) => setProyectado(r.salida))
      .catch(() => setProyectado(null));
  }, [e.entidad_id, recetaSel]);

  const fila = (etq: string, val: unknown) =>
    val ? (
      <div className="detalle-fila">
        <span className="detalle-etiqueta">{etq}</span>
        <span className="detalle-valor">{String(val)}</span>
      </div>
    ) : null;

  const desactivar = () => {
    if (!window.confirm("¿Desactivar esta persona? (soft-delete: se oculta, no se borra)")) return;
    entidadActivo(e.entidad_id, false).then(() => { onCambio(); onCerrar(); }).catch((x) => setAviso(String(x)));
  };

  return (
    <aside className="detalle">
      <div className="detalle-encabezado">
        <h2 title={c.nombre_completo}>{c.nombre_completo || "(sin nombre)"}</h2>
        <button className="cerrar" onClick={onCerrar} aria-label="Cerrar">×</button>
      </div>

      <div className="veredicto tono-ok">
        <div className="veredicto-chips">
          <span className="chip-veredicto tono-ok">ancla {ANCLA_ETQ[e.ancla_tipo] ?? e.ancla_tipo}</span>
          <span className="chip-veredicto">{e.procedencias.length || 0} fuente(s)</span>
        </div>
        <p className="veredicto-frase">
          Persona canónica resuelta por <b className="titulo-ok">{ANCLA_ETQ[e.ancla_tipo]}</b>.
          La misma de varias fuentes = una sola (sin duplicar).
        </p>
      </div>

      {/* DINAMISMO: la misma persona bajo cualquier receta de salida */}
      <h3>Ver como receta (salida por sistema)</h3>
      <div className="explorador-campos" style={{ marginBottom: 8 }}>
        <select value={recetaSel} onChange={(ev) => setRecetaSel(ev.target.value)} className="select-receta">
          {recetasDisp.map((r) => (
            <option key={r.clave} value={r.clave}>{r.nombre} ({r.clave})</option>
          ))}
        </select>
      </div>
      <pre className="texto-extraido">{proyectado ? JSON.stringify(proyectado, null, 2) : "…"}</pre>

      <h3>Identidad (canónica)</h3>
      {fila("Nombre", nom.nombre1 ? `${nom.nombre1} ${nom.nombre2 ?? ""} ${nom.apellido1 ?? ""} ${nom.apellido2 ?? ""}`.replace(/\s+/g, " ").trim() : null)}
      {fila("Alias", c.alias)}
      {fila("CURP", c.curp)}
      {fila("RFC", c.rfc)}
      {fila("Sexo", c.sexo)}
      {fila("Edad", c.edad)}
      {fila("Nacimiento (de CURP)", norm.normalized_dob)}
      {fila("Estado de nacimiento", norm.normalized_estado)}
      {fila("Email", c.email)}
      {fila("Teléfono", c.telefono)}

      <h3>Contingencia</h3>
      {aviso && <div className="banner-error">{aviso}</div>}
      <button className="secundario" onClick={desactivar}>🚫 Desactivar (soft-delete)</button>
      <p className="panel-nota">No borra: la deja oculta y auditable (LFPDPPP). Reactivable desde el listado de inactivas.</p>
    </aside>
  );
}

// ---------------------------------------------------------------- gestión de recetas

const PLANTILLA_NUEVA = JSON.stringify(
  { salida: [
      { path: "full_name", de: "nombre_completo" },
      { path: "id", de: "curp" },
      { path: "gender", de: "sexo", mapa: { H: "male", M: "female" } },
    ] },
  null, 2,
);

function GestionRecetas() {
  const [lista, setLista] = useState<Receta[]>([]);
  const [sel, setSel] = useState<Receta | null>(null);
  const [editJson, setEditJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const [creando, setCreando] = useState(false);
  const [nuevaClave, setNuevaClave] = useState("");
  const [nuevoNombre, setNuevoNombre] = useState("");

  const cargar = useCallback(() => {
    pedirRecetas().then(setLista).catch((e) => setError(String(e)));
  }, []);
  useEffect(() => cargar(), [cargar]);

  const abrir = (r: Receta) => {
    setSel(r); setCreando(false); setAviso(null); setError(null);
    setEditJson(JSON.stringify(r.definicion, null, 2));
  };

  const guardar = (clave: string, nombre: string) => {
    let def: Record<string, unknown>;
    try { def = JSON.parse(editJson); }
    catch { setError("La definición no es JSON válido"); return; }
    guardarReceta({ clave, nombre, definicion: def })
      .then((r) => { setAviso(`guardada: ${r.clave}`); setError(null); cargar(); abrir(r); setCreando(false); })
      .catch((e) => setError(String(e)));
  };

  const borrar = (r: Receta) => {
    if (!window.confirm(`¿Borrar la receta "${r.clave}"?`)) return;
    borrarReceta(r.clave).then(() => { setSel(null); cargar(); }).catch((e) => setError(String(e)));
  };

  return (
    <div className="recetas-cuerpo">
      <div className="recetas-lista">
        <div className="explorador-filtros">
          <button className="primario" onClick={() => {
            setCreando(true); setSel(null); setError(null); setAviso(null);
            setNuevaClave(""); setNuevoNombre(""); setEditJson(PLANTILLA_NUEVA);
          }}>＋ Nueva receta</button>
        </div>
        {lista.map((r) => (
          <div key={r.clave} className={`receta-item${sel?.clave === r.clave ? " activa" : ""}`} onClick={() => abrir(r)}>
            <span className="receta-nombre">{r.nombre}</span>
            <span className="receta-clave">{r.clave}{r.editable ? "" : " · base"}</span>
            <span className="receta-desc">{r.descripcion}</span>
          </div>
        ))}
      </div>

      <div className="receta-editor">
        {error && <div className="banner-error">{error}</div>}
        {aviso && <div className="banner-aviso">{aviso}</div>}
        {creando && (
          <>
            <h3>Nueva receta de proyección</h3>
            <div className="explorador-campos">
              <input value={nuevaClave} placeholder="clave (a-z0-9_-)" onChange={(e) => setNuevaClave(e.target.value)} />
              <input value={nuevoNombre} placeholder="nombre" onChange={(e) => setNuevoNombre(e.target.value)} />
            </div>
            <p className="panel-nota">definición: {"{ passthrough: true }"} = canónica; {"{ salida: [{ path, de | constante, mapa? }] }"} = transformar 1 persona; {"{ sobre, coleccion, item }"} = archivo completo (exportable).</p>
            <textarea className="json-receta" value={editJson} onChange={(e) => setEditJson(e.target.value)} spellCheck={false} />
            <div className="filtro-acciones">
              <button className="primario" disabled={!nuevaClave.trim() || !nuevoNombre.trim()} onClick={() => guardar(nuevaClave.trim(), nuevoNombre.trim())}>Crear</button>
            </div>
          </>
        )}
        {sel && !creando && (
          <>
            <h3>{sel.nombre} <span className="receta-clave">({sel.clave})</span></h3>
            <p className="panel-nota">{sel.descripcion}{sel.editable ? "" : " — receta BASE: clónala para variar (no editable)."}</p>
            <textarea className="json-receta" value={editJson} onChange={(e) => setEditJson(e.target.value)} spellCheck={false} disabled={!sel.editable} />
            <div className="filtro-acciones">
              <button className="primario" disabled={!sel.editable} onClick={() => guardar(sel.clave, sel.nombre)}>Guardar</button>
              {sel.editable && <button className="secundario" onClick={() => borrar(sel)}>Borrar</button>}
            </div>
          </>
        )}
        {!sel && !creando && <p className="panel-nota">Elige una receta para ver/editar su definición, o crea una nueva.</p>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- pestaña

export default function Entidades() {
  const [modo, setModo] = useState<"personas" | "recetas">("personas");
  const [stats, setStats] = useState<EstadisticasEntidades | null>(null);
  const [lista, setLista] = useState<Entidad[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [nombre, setNombre] = useState("");
  const [curp, setCurp] = useState("");
  const [sel, setSel] = useState<Entidad | null>(null);
  const [recetasDisp, setRecetasDisp] = useState<Receta[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);
  const [recetaExp, setRecetaExp] = useState("fz1_bundle");
  const [exportando, setExportando] = useState(false);

  // Recetas de COLECCIÓN (arman el archivo completo) vs POR-PERSONA (1 ficha).
  const colecciones = recetasDisp.filter((r) => r.definicion && "coleccion" in r.definicion);
  const recetasPersona = recetasDisp.filter((r) => !(r.definicion && "coleccion" in r.definicion));

  const descargar = async () => {
    setExportando(true);
    try {
      const archivo = await exportarEntidades(recetaExp);
      const blob = new Blob([JSON.stringify(archivo, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${recetaExp}.json`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(String(e)); }
    finally { setExportando(false); }
  };

  const cargar = useCallback((pagina: string | null = null) => {
    setCargando(true);
    pedirEntidades({ nombre: nombre.trim() || undefined, curp: curp.trim() || undefined, cursor: pagina ?? undefined, limite: 50 })
      .then((r) => {
        setLista((prev) => (pagina ? [...prev, ...r.entidades] : r.entidades));
        setTotal(r.total); setCursor(r.cursor); setError(null);
      })
      .catch((e) => setError(String(e))).finally(() => setCargando(false));
  }, [nombre, curp]);

  useEffect(() => { entidadesStats().then(setStats).catch(() => setStats(null)); }, [lista]);
  useEffect(() => { pedirRecetas("proyeccion").then(setRecetasDisp).catch(() => setRecetasDisp([])); }, []);
  useEffect(() => { if (modo !== "personas") return; const t = window.setTimeout(() => cargar(), 250); return () => window.clearTimeout(t); }, [cargar, modo]);

  return (
    <section className="explorador">
      <div className="pestanas" style={{ margin: "0 0 6px", padding: "10px 18px 0" }}>
        <button className={modo === "personas" ? "pestana activa" : "pestana"} onClick={() => setModo("personas")}>Personas</button>
        <button className={modo === "recetas" ? "pestana activa" : "pestana"} onClick={() => setModo("recetas")}>Recetas</button>
      </div>

      {modo === "recetas" ? <GestionRecetas /> : (
        <>
          <div className="kpis kpis-secundarios" style={{ padding: "10px 18px 0" }}>
            <div className="kpi tono-ok"><span className="kpi-valor kpi-ok">{(stats?.total ?? 0).toLocaleString()}</span><span className="kpi-etiqueta">personas canónicas</span></div>
            <div className="kpi tono-ok"><span className="kpi-valor kpi-ok">{(stats?.con_curp ?? 0).toLocaleString()}</span><span className="kpi-etiqueta">con CURP válida</span></div>
            <div className="kpi"><span className="kpi-valor kpi-gris">{Object.entries(stats?.por_ancla ?? {}).map(([k, v]) => `${ANCLA_ETQ[k] ?? k}:${v}`).join(" · ") || "—"}</span><span className="kpi-etiqueta">por ancla</span></div>
          </div>
          <div className="explorador-filtros">
            <div className="explorador-campos">
              <input value={nombre} placeholder="nombre contiene…" onChange={(e) => setNombre(e.target.value)} />
              <input value={curp} placeholder="CURP exacta…" className="campo-corto" onChange={(e) => setCurp(e.target.value)} />
            </div>
          </div>
          {error && <div className="banner-error">{error}</div>}
          {colecciones.length > 0 && (
            <div className="explorador-filtros" style={{ paddingTop: 0 }}>
              <div className="explorador-campos">
                <span className="panel-nota" style={{ margin: 0, alignSelf: "center" }}>Exportar archivo completo:</span>
                <select className="select-receta" value={recetaExp} onChange={(e) => setRecetaExp(e.target.value)}>
                  {colecciones.map((r) => <option key={r.clave} value={r.clave}>{r.nombre} ({r.clave})</option>)}
                </select>
                <button className="secundario" disabled={exportando} onClick={descargar}>{exportando ? "exportando…" : "⬇ Descargar JSON"}</button>
              </div>
            </div>
          )}
          <div className="resultados-resumen"><b>{total.toLocaleString()}</b> personas resueltas — cada una deduplicada por su ancla fuerte</div>
          <table>
            <thead><tr><th>Nombre</th><th>CURP</th><th>Sexo</th><th>Edad</th><th>Estado</th><th>Ancla</th><th>Fuentes</th></tr></thead>
            <tbody>
              {lista.map((e) => {
                const c = e.campos ?? {};
                return (
                  <tr key={e.entidad_id} onClick={() => setSel(e)}>
                    <td className="celda-nombre">{c.nombre_completo || "—"}</td>
                    <td className="celda-tipo">{c.curp || "—"}</td>
                    <td>{c.sexo || "—"}</td><td>{c.edad || "—"}</td>
                    <td className="celda-tipo">{c.normalizados?.normalized_estado || "—"}</td>
                    <td><span className="chip ok">{ANCLA_ETQ[e.ancla_tipo] ?? e.ancla_tipo}</span></td>
                    <td>{e.procedencias.length || 0}</td>
                  </tr>
                );
              })}
              {lista.length === 0 && !cargando && (
                <tr><td colSpan={7} className="vacio">aún no hay entidades — proyecta un dataset (CLI <code>norm proyectar</code> o POST /entidades/proyectar)</td></tr>
              )}
            </tbody>
          </table>
          {cursor && <button className="cargar-mas" disabled={cargando} onClick={() => cargar(cursor)}>{cargando ? "cargando…" : `cargar más (${lista.length} de ${total.toLocaleString()})`}</button>}
          {sel && <DetalleEntidad e={sel} recetasDisp={recetasPersona} onCerrar={() => setSel(null)} onCambio={() => cargar()} />}
        </>
      )}
    </section>
  );
}
