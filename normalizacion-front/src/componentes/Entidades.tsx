// Pestaña Entidades (Fase 2): personas canónicas resueltas a partir del lago.
// Cada fila es UNA persona deduplicada (misma CURP de varias fuentes = una sola);
// el detalle muestra la ficha Fz1 con los campos derivados de la CURP y las
// procedencias (de qué fuentes salió cada dato).

import { useCallback, useEffect, useState } from "react";
import { entidades as pedirEntidades, entidadesStats } from "../api";
import type { Entidad, EstadisticasEntidades } from "../tipos";

const ANCLA_ETQ: Record<string, string> = {
  curp: "CURP", rfc: "RFC", email: "correo", telefono: "teléfono",
};

function DetalleEntidad({ e, onCerrar }: { e: Entidad; onCerrar: () => void }) {
  const c = e.campos ?? {};
  const nom = c.nombre ?? {};
  const dir = c.direccion ?? {};
  const norm = c.normalizados ?? {};
  const fila = (etq: string, val: unknown) =>
    val ? (
      <div className="detalle-fila">
        <span className="detalle-etiqueta">{etq}</span>
        <span className="detalle-valor">{String(val)}</span>
      </div>
    ) : null;

  return (
    <aside className="detalle">
      <div className="detalle-encabezado">
        <h2 title={c.nombre_completo}>{c.nombre_completo || "(sin nombre)"}</h2>
        <button className="cerrar" onClick={onCerrar} aria-label="Cerrar">×</button>
      </div>

      <div className="veredicto tono-ok">
        <div className="veredicto-chips">
          <span className="chip-veredicto tono-ok">
            ancla {ANCLA_ETQ[e.ancla_tipo] ?? e.ancla_tipo}
          </span>
          <span className="chip-veredicto">{e.procedencias.length || 0} fuente(s)</span>
        </div>
        <p className="veredicto-frase">
          Persona canónica resuelta por <b className="titulo-ok">{ANCLA_ETQ[e.ancla_tipo]}</b>.
          Misma {ANCLA_ETQ[e.ancla_tipo]} de varias fuentes = una sola entidad (sin duplicar).
        </p>
      </div>

      <h3>Identidad</h3>
      {fila("Nombre", nom.nombre1 ? `${nom.nombre1} ${nom.nombre2 ?? ""} ${nom.apellido1 ?? ""} ${nom.apellido2 ?? ""}`.replace(/\s+/g, " ").trim() : null)}
      {fila("Alias", c.alias)}
      {fila("CURP", c.curp)}
      {fila("RFC", c.rfc)}
      {fila("Sexo", c.sexo)}
      {fila("Edad", c.edad)}

      <h3>Derivado de la CURP</h3>
      {fila("Fecha de nacimiento", norm.normalized_dob)}
      {fila("Sexo (normalizado)", norm.normalized_sex)}
      {fila("Estado de nacimiento", norm.normalized_estado)}
      {!norm.normalized_dob && <p className="panel-nota">sin CURP válida: no hay derivación determinista</p>}

      {(dir.calle || dir.municipio || dir.estado) && (
        <>
          <h3>Domicilio</h3>
          {fila("Calle", [dir.calle, dir.numero_exterior, dir.numero_interior].filter(Boolean).join(" "))}
          {fila("Colonia", dir.colonia)}
          {fila("Municipio", dir.municipio)}
          {fila("CP", dir.codigo_postal)}
          {fila("Estado", dir.estado)}
        </>
      )}

      <h3>Contacto</h3>
      {fila("Email", c.email)}
      {fila("Teléfono", c.telefono)}
      {!c.email && !c.telefono && <p className="panel-nota">sin contacto</p>}

      <h3>Procedencia y versión</h3>
      {fila("Receta", e.version_receta)}
      {fila("Resolución", e.version_resolucion)}
      {fila("entidad_id", `${e.entidad_id.slice(0, 16)}…`)}
      {e.procedencias.length > 0 && (
        <pre className="texto-extraido">{JSON.stringify(e.procedencias, null, 2)}</pre>
      )}
    </aside>
  );
}

export default function Entidades() {
  const [stats, setStats] = useState<EstadisticasEntidades | null>(null);
  const [lista, setLista] = useState<Entidad[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [nombre, setNombre] = useState("");
  const [curp, setCurp] = useState("");
  const [sel, setSel] = useState<Entidad | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  const cargar = useCallback(
    (pagina: string | null = null) => {
      setCargando(true);
      pedirEntidades({ nombre: nombre.trim() || undefined, curp: curp.trim() || undefined, cursor: pagina ?? undefined, limite: 50 })
        .then((r) => {
          setLista((prev) => (pagina ? [...prev, ...r.entidades] : r.entidades));
          setTotal(r.total);
          setCursor(r.cursor);
          setError(null);
        })
        .catch((e) => setError(String(e)))
        .finally(() => setCargando(false));
    },
    [nombre, curp],
  );

  useEffect(() => {
    entidadesStats().then(setStats).catch(() => setStats(null));
  }, [lista]);

  useEffect(() => {
    const t = window.setTimeout(() => cargar(), 250);
    return () => window.clearTimeout(t);
  }, [cargar]);

  return (
    <section className="explorador">
      <div className="kpis kpis-secundarios" style={{ padding: "16px 18px 0" }}>
        <div className="kpi tono-ok">
          <span className="kpi-valor kpi-ok">{(stats?.total ?? 0).toLocaleString()}</span>
          <span className="kpi-etiqueta">personas canónicas</span>
        </div>
        <div className="kpi tono-ok">
          <span className="kpi-valor kpi-ok">{(stats?.con_curp ?? 0).toLocaleString()}</span>
          <span className="kpi-etiqueta">con CURP válida</span>
        </div>
        <div className="kpi">
          <span className="kpi-valor kpi-gris">
            {Object.entries(stats?.por_ancla ?? {}).map(([k, v]) => `${ANCLA_ETQ[k] ?? k}:${v}`).join(" · ") || "—"}
          </span>
          <span className="kpi-etiqueta">por ancla de resolución</span>
        </div>
      </div>

      <div className="explorador-filtros">
        <div className="explorador-campos">
          <input value={nombre} placeholder="nombre contiene…" onChange={(e) => setNombre(e.target.value)} />
          <input value={curp} placeholder="CURP exacta…" className="campo-corto" onChange={(e) => setCurp(e.target.value)} />
        </div>
      </div>

      {error && <div className="banner-error">{error}</div>}
      <div className="resultados-resumen">
        <b>{total.toLocaleString()}</b> personas resueltas — cada una deduplicada por su ancla fuerte
      </div>

      <table>
        <thead>
          <tr>
            <th>Nombre</th><th>CURP</th><th>Sexo</th><th>Edad</th><th>Estado</th><th>Ancla</th><th>Fuentes</th>
          </tr>
        </thead>
        <tbody>
          {lista.map((e) => {
            const c = e.campos ?? {};
            return (
              <tr key={e.entidad_id} onClick={() => setSel(e)}>
                <td className="celda-nombre">{c.nombre_completo || "—"}</td>
                <td className="celda-tipo">{c.curp || "—"}</td>
                <td>{c.sexo || "—"}</td>
                <td>{c.edad || "—"}</td>
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

      {cursor && (
        <button className="cargar-mas" disabled={cargando} onClick={() => cargar(cursor)}>
          {cargando ? "cargando…" : `cargar más (${lista.length} de ${total.toLocaleString()})`}
        </button>
      )}

      {sel && <DetalleEntidad e={sel} onCerrar={() => setSel(null)} />}
    </section>
  );
}
