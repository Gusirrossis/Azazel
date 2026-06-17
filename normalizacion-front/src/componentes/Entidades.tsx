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
  atributosDeclarados as pedirAtributos, backfillEntidades, borrarReceta, entidadActivo,
  entidadProyectar, entidades as pedirEntidades, entidadesStats, exportarEntidades,
  guardarAtributos, guardarReceta, nucleoEntidad, recetas as pedirRecetas,
} from "../api";
import type { AtributoDeclarado, NucleoEntidad } from "../api";
import type { Entidad, EstadisticasEntidades, Receta } from "../tipos";

const NORMALIZADORES = ["texto", "curp", "rfc", "email", "telefono", "nombre"];

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

      {c.atributos && Object.keys(c.atributos).length > 0 && (
        <>
          <h3>Atributos extra (declarados)</h3>
          {Object.entries(c.atributos as Record<string, unknown>).map(([k, v]) => fila(k, v))}
        </>
      )}

      <h3>Ficha completa (canónica)</h3>
      <details>
        <summary className="panel-nota" style={{ cursor: "pointer" }}>ver TODO el dato canónico de esta persona (JSON)</summary>
        <pre className="texto-extraido">{JSON.stringify(c, null, 2)}</pre>
      </details>

      <h3>Contingencia</h3>
      {aviso && <div className="banner-error">{aviso}</div>}
      <button className="secundario" onClick={desactivar}>🚫 Desactivar (soft-delete)</button>
      <p className="panel-nota">No borra: la deja oculta y auditable (LFPDPPP). Reactivable desde el listado de inactivas.</p>
    </aside>
  );
}

// ---------------------------------------------------------------- gestión de recetas

type FilaSalida = { path: string; modo: "de" | "constante"; valor: string; mapa: string };

// Campos canónicos sugeridos para el origen "de" (autocompletado).
const CAMPOS_CANONICOS = [
  "curp", "rfc", "nombre_completo", "nombre.nombre1", "nombre.nombre2",
  "nombre.apellido1", "nombre.apellido2", "alias", "sexo", "edad", "email", "telefono",
  "relacion", "direccion.calle", "direccion.colonia", "direccion.municipio",
  "direccion.estado", "direccion.codigo_postal", "normalizados.normalized_dob",
  "normalizados.normalized_estado", "atributos",
];

function defAsalida(def: Record<string, any>): { salida: any[]; tipo: "item" | "coleccion" | "passthrough" } {
  if (def?.passthrough) return { salida: [], tipo: "passthrough" };
  if (def && "coleccion" in def) return { salida: def.item?.salida ?? [], tipo: "coleccion" };
  return { salida: def?.salida ?? [], tipo: "item" };
}

function aFilas(salida: any[]): FilaSalida[] {
  return (salida || []).map((s) => ({
    path: s.path ?? "",
    modo: "constante" in s ? "constante" : "de",
    valor: "constante" in s ? String(s.constante) : (s.de ?? ""),
    mapa: s.mapa ? Object.entries(s.mapa).map(([k, v]) => `${k}:${v}`).join(", ") : "",
  }));
}

function aSalida(filas: FilaSalida[]): any[] {
  return filas.filter((f) => f.path.trim()).map((f) => {
    const spec: Record<string, any> = { path: f.path.trim() };
    if (f.modo === "constante") { spec.constante = f.valor; return spec; }
    spec.de = f.valor.trim();
    const mapa: Record<string, string> = {};
    for (const par of f.mapa.split(",")) {
      const i = par.indexOf(":");
      if (i > 0) mapa[par.slice(0, i).trim()] = par.slice(i + 1).trim();
    }
    if (Object.keys(mapa).length) spec.mapa = mapa;
    return spec;
  });
}

// Editor VISUAL de los campos de salida (una fila por campo, sin tocar JSON).
function EditorSalida({ filas, setFilas, editable }: {
  filas: FilaSalida[]; setFilas: (f: FilaSalida[]) => void; editable: boolean;
}) {
  const set = (i: number, patch: Partial<FilaSalida>) =>
    setFilas(filas.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  return (
    <div className="editor-salida">
      <table className="tabla-salida">
        <thead><tr><th>Campo de salida</th><th>Origen</th><th>Valor</th><th>Mapa</th><th /></tr></thead>
        <tbody>
          {filas.map((f, i) => (
            <tr key={i}>
              <td><input value={f.path} disabled={!editable} placeholder="contact.email"
                         onChange={(e) => set(i, { path: e.target.value })} /></td>
              <td>
                <select value={f.modo} disabled={!editable}
                        onChange={(e) => set(i, { modo: e.target.value as "de" | "constante" })}>
                  <option value="de">de campo</option>
                  <option value="constante">constante</option>
                </select>
              </td>
              <td><input list={f.modo === "de" ? "campos-canonicos" : undefined} value={f.valor}
                         disabled={!editable} placeholder={f.modo === "de" ? "email" : "valor fijo"}
                         onChange={(e) => set(i, { valor: e.target.value })} /></td>
              <td>{f.modo === "de"
                ? <input value={f.mapa} disabled={!editable} placeholder="ej: H:male, M:female"
                         onChange={(e) => set(i, { mapa: e.target.value })} />
                : <span className="panel-nota">—</span>}</td>
              <td>{editable && <button className="icono-quitar"
                    onClick={() => setFilas(filas.filter((_, j) => j !== i))}>×</button>}</td>
            </tr>
          ))}
          {filas.length === 0 && <tr><td colSpan={5} className="vacio">sin campos — agrega el primero</td></tr>}
        </tbody>
      </table>
      <datalist id="campos-canonicos">{CAMPOS_CANONICOS.map((c) => <option key={c} value={c} />)}</datalist>
      {editable && (
        <button className="secundario" onClick={() => setFilas([...filas, { path: "", modo: "de", valor: "", mapa: "" }])}>
          ＋ Agregar campo
        </button>
      )}
    </div>
  );
}

function GestionRecetas() {
  const [lista, setLista] = useState<Receta[]>([]);
  const [sel, setSel] = useState<Receta | null>(null);
  const [creando, setCreando] = useState(false);
  const [nuevaClave, setNuevaClave] = useState("");
  const [nuevoNombre, setNuevoNombre] = useState("");
  const [filas, setFilas] = useState<FilaSalida[]>([]);
  const [tipoDef, setTipoDef] = useState<"item" | "coleccion" | "passthrough">("item");
  const [baseDef, setBaseDef] = useState<Record<string, any>>({});
  const [modoJson, setModoJson] = useState(false);
  const [editJson, setEditJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);

  const cargar = useCallback(() => {
    pedirRecetas().then(setLista).catch((e) => setError(String(e)));
  }, []);
  useEffect(() => cargar(), [cargar]);

  const cargarDef = (def: Record<string, any>) => {
    const { salida, tipo } = defAsalida(def);
    setBaseDef(def); setTipoDef(tipo); setFilas(aFilas(salida));
    setEditJson(JSON.stringify(def, null, 2));
    setModoJson(tipo === "passthrough");
  };

  const abrir = (r: Receta) => {
    setSel(r); setCreando(false); setAviso(null); setError(null);
    cargarDef(r.definicion);
  };

  const definicionActual = (): Record<string, any> | null => {
    if (modoJson) {
      try { return JSON.parse(editJson); } catch { setError("El JSON no es válido"); return null; }
    }
    if (tipoDef === "passthrough") return { passthrough: true };
    if (tipoDef === "coleccion") return { ...baseDef, item: { salida: aSalida(filas) } };
    return { salida: aSalida(filas) };
  };

  const guardar = (clave: string, nombre: string) => {
    const def = definicionActual();
    if (!def) return;
    guardarReceta({ clave, nombre, definicion: def })
      .then((r) => { setAviso(`guardada: ${r.clave}`); setError(null); cargar(); abrir(r); setCreando(false); })
      .catch((e) => setError(String(e)));
  };

  const borrar = (r: Receta) => {
    if (!window.confirm(`¿Borrar la receta "${r.clave}"?`)) return;
    borrarReceta(r.clave).then(() => { setSel(null); cargar(); }).catch((e) => setError(String(e)));
  };

  const editable = creando || (sel?.editable ?? false);
  const verVisual = () => { const d = definicionActual(); if (d) { cargarDef(d); setModoJson(false); } };
  const verJson = () => { setEditJson(JSON.stringify(definicionActual() ?? baseDef, null, 2)); setModoJson(true); };

  const editor = (
    <>
      {tipoDef === "coleccion" && (
        <p className="panel-nota">Receta de COLECCIÓN (archivo completo): editas la receta por-persona; el sobre/_metadata se conserva (tócalo en JSON avanzado).</p>
      )}
      <div className="receta-modo">
        <button className={!modoJson ? "pestana activa" : "pestana"} disabled={tipoDef === "passthrough"} onClick={verVisual}>Editor visual</button>
        <button className={modoJson ? "pestana activa" : "pestana"} onClick={verJson}>JSON avanzado</button>
      </div>
      {modoJson ? (
        <textarea className="json-receta" value={editJson} disabled={!editable} spellCheck={false} onChange={(e) => setEditJson(e.target.value)} />
      ) : tipoDef === "passthrough" ? (
        <p className="panel-nota">Devuelve la persona canónica tal cual (passthrough): no hay campos que editar.</p>
      ) : (
        <EditorSalida filas={filas} setFilas={setFilas} editable={editable} />
      )}
    </>
  );

  return (
    <div className="recetas-cuerpo">
      <div className="recetas-lista">
        <div className="explorador-filtros">
          <button className="primario" onClick={() => {
            setCreando(true); setSel(null); setError(null); setAviso(null);
            setNuevaClave(""); setNuevoNombre(""); cargarDef({ salida: [{ path: "id", de: "curp" }] });
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
            <h3>Nueva receta de salida</h3>
            <div className="explorador-campos">
              <input value={nuevaClave} placeholder="clave (a-z0-9_-)" onChange={(e) => setNuevaClave(e.target.value)} />
              <input value={nuevoNombre} placeholder="nombre visible" onChange={(e) => setNuevoNombre(e.target.value)} />
            </div>
            <p className="panel-nota">Cada renglón = un campo de salida: a dónde va (<b>path</b>) y de dónde sale (un campo canónico) o un valor fijo. Traduce valores con <code>H:male, M:female</code>.</p>
            {editor}
            <div className="filtro-acciones">
              <button className="primario" disabled={!nuevaClave.trim() || !nuevoNombre.trim()} onClick={() => guardar(nuevaClave.trim(), nuevoNombre.trim())}>Crear</button>
            </div>
          </>
        )}
        {sel && !creando && (
          <>
            <h3>{sel.nombre} <span className="receta-clave">({sel.clave})</span></h3>
            <p className="panel-nota">{sel.descripcion}{sel.editable ? "" : " — receta BASE: clónala para variar (no editable)."}</p>
            {editor}
            <div className="filtro-acciones">
              <button className="primario" disabled={!sel.editable} onClick={() => guardar(sel.clave, sel.nombre)}>Guardar</button>
              {sel.editable && <button className="secundario" onClick={() => borrar(sel)}>Borrar</button>}
            </div>
          </>
        )}
        {!sel && !creando && <p className="panel-nota">Elige una receta para ver/editar, o crea una nueva.</p>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- atributos extra

function GestionAtributos() {
  const [lista, setLista] = useState<AtributoDeclarado[]>([]);
  const [nucleo, setNucleo] = useState<NucleoEntidad | null>(null);
  const [nuevoNombre, setNuevoNombre] = useState("");
  const [nuevoNorm, setNuevoNorm] = useState("texto");
  const [guardando, setGuardando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);

  const cargar = useCallback(() => {
    pedirAtributos().then(setLista).catch((e) => setError(String(e)));
  }, []);
  useEffect(() => cargar(), [cargar]);
  useEffect(() => { nucleoEntidad().then(setNucleo).catch(() => setNucleo(null)); }, []);

  const persistir = (next: AtributoDeclarado[]) => {
    setGuardando(true);
    guardarAtributos(next)
      .then((r) => { setLista(r); setAviso("guardado"); setError(null); })
      .catch((e) => { setError(String(e)); setAviso(null); })
      .finally(() => setGuardando(false));
  };
  const agregar = () => {
    const nombre = nuevoNombre.trim().toLowerCase();
    if (!nombre) return;
    persistir([...lista, { nombre, normalizador: nuevoNorm }]);
    setNuevoNombre(""); setNuevoNorm("texto");
  };
  const quitar = (nombre: string) => persistir(lista.filter((a) => a.nombre !== nombre));

  return (
    <div className="recetas-cuerpo">
      <div className="receta-editor" style={{ width: "100%" }}>
        <h3>Núcleo fijo (siempre se captura)</h3>
        <p className="panel-nota">
          Estos campos vienen por defecto y NO se editan. Las marcadas como <b>ancla</b>
          (CURP/RFC/email/teléfono) identifican a la persona y deduplican.
        </p>
        {nucleo && (
          <table>
            <thead><tr><th>Campo</th><th>Normalizador</th><th>Ancla</th></tr></thead>
            <tbody>
              {nucleo.campos.map((c) => (
                <tr key={c.nombre}>
                  <td className="celda-nombre">{c.nombre}</td>
                  <td className="celda-tipo">{c.normalizador}</td>
                  <td>{c.ancla ? <span className="chip ok">ancla</span> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {nucleo && (
          <p className="panel-nota">Derivados de la CURP (automáticos): {nucleo.derivados.join(" · ")}</p>
        )}

        <h3 style={{ marginTop: 20 }}>Atributos extra (declarados por ti)</h3>
        <p className="panel-nota">
          Además del núcleo, declara qué datos EXTRA capturar (p. ej. <code>color_favorito</code>,
          <code>placa</code>). Lo declarado se guarda en <code>atributos</code>; lo no declarado se
          descarta (el archivo origen queda en el lago, reproyectable). Aplica a la próxima proyección/backfill.
        </p>
        {error && <div className="banner-error">{error}</div>}
        {aviso && <div className="banner-aviso">{aviso}</div>}
        <div className="explorador-campos">
          <input value={nuevoNombre} placeholder="nombre (a-z, dígitos, _)"
                 onChange={(e) => setNuevoNombre(e.target.value)} />
          <select className="select-receta" value={nuevoNorm} onChange={(e) => setNuevoNorm(e.target.value)}>
            {NORMALIZADORES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <button className="primario" disabled={!nuevoNombre.trim() || guardando} onClick={agregar}>＋ Declarar</button>
        </div>
        {lista.length === 0 ? (
          <p className="panel-nota">Aún no hay atributos extra: solo se captura el núcleo.</p>
        ) : (
          <table>
            <thead><tr><th>Atributo</th><th>Normalizador</th><th></th></tr></thead>
            <tbody>
              {lista.map((a) => (
                <tr key={a.nombre}>
                  <td className="celda-nombre">{a.nombre}</td>
                  <td className="celda-tipo">{a.normalizador}</td>
                  <td><button className="secundario" disabled={guardando} onClick={() => quitar(a.nombre)}>quitar</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- pestaña

export default function Entidades() {
  const [modo, setModo] = useState<"personas" | "recetas" | "atributos">("personas");
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
  const [backfilling, setBackfilling] = useState(false);
  const [backfillMsg, setBackfillMsg] = useState<string | null>(null);

  // Recetas de COLECCIÓN (arman el archivo completo) vs POR-PERSONA (1 ficha).
  const colecciones = recetasDisp.filter((r) => r.definicion && "coleccion" in r.definicion);
  const recetasPersona = recetasDisp.filter((r) => !(r.definicion && "coleccion" in r.definicion));

  const procesarIndexados = async () => {
    setBackfilling(true); setBackfillMsg(null);
    try {
      const r = await backfillEntidades(2000);
      setBackfillMsg(
        `Lote: ${r.docs} docs revisados · ${r.con_persona} con persona · ` +
        `${r.entidades_nuevas} nuevas, ${r.entidades_fusionadas} fusionadas. ` +
        (r.docs >= 2000 ? "Hay más: vuelve a pulsar para seguir." : "Índice al día."),
      );
      cargar(); entidadesStats().then(setStats).catch(() => {});
    } catch (e) { setBackfillMsg(`error: ${e}`); }
    finally { setBackfilling(false); }
  };

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
        <button className={modo === "atributos" ? "pestana activa" : "pestana"} onClick={() => setModo("atributos")}>Atributos</button>
      </div>

      {modo === "recetas" ? <GestionRecetas /> : modo === "atributos" ? <GestionAtributos /> : (
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
          <div className="entidades-barra">
            <div className="acc-grupo">
              <span className="acc-etq">Ya indexados</span>
              <button className="secundario" disabled={backfilling} onClick={procesarIndexados}
                      title="Resuelve personas de los archivos ya indexados que traen CURP/RFC">
                {backfilling ? "procesando…" : "⟳ Procesar (CURP/RFC)"}
              </button>
            </div>
            {colecciones.length > 0 && (
              <div className="acc-grupo">
                <span className="acc-etq">Exportar archivo</span>
                <select className="select-receta" value={recetaExp} onChange={(e) => setRecetaExp(e.target.value)}>
                  {colecciones.map((r) => <option key={r.clave} value={r.clave}>{r.nombre}</option>)}
                </select>
                <button className="secundario" disabled={exportando} onClick={descargar}>
                  {exportando ? "exportando…" : "⬇ Descargar JSON"}
                </button>
              </div>
            )}
          </div>
          {backfillMsg && <div className="banner-aviso">{backfillMsg}</div>}
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
