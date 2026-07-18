import { useEffect, useState } from "react";
import {
  listarClavesBusqueda,
  generarClaveBusqueda,
  revocarClaveBusqueda,
} from "../api";
import type { ClaveBusqueda } from "../tipos";

/**
 * Gestión de claves de acceso al endpoint de búsqueda (POST /buscar) y descarga.
 * Solo se guarda el hash en el servidor: la clave en claro se muestra UNA vez al generar.
 * Sin ninguna clave, el endpoint está ABIERTO; al crear la primera queda cerrado.
 */
export default function ClavesBusqueda() {
  const [claves, setClaves] = useState<ClaveBusqueda[]>([]);
  const [nombre, setNombre] = useState("");
  const [generada, setGenerada] = useState<{ nombre: string; clave: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copiada, setCopiada] = useState(false);

  const cargar = () =>
    listarClavesBusqueda()
      .then(setClaves)
      .catch((e) => setError(String(e.message ?? e)));

  useEffect(() => {
    cargar();
  }, []);

  async function generar() {
    const n = nombre.trim();
    if (!n) {
      setError("Ponle un nombre (el consumidor dueño de la clave).");
      return;
    }
    setError(null);
    try {
      const r = await generarClaveBusqueda(n);
      setGenerada(r);
      setNombre("");
      setCopiada(false);
      cargar();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }

  async function revocar(n: string) {
    if (!confirm(`¿Revocar la clave de "${n}"? Dejará de poder consultar.`)) return;
    try {
      await revocarClaveBusqueda(n);
      cargar();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }

  return (
    <div className="config-atributos">
      <h3>Claves de acceso al buscador</h3>
      <p className="panel-nota">
        Cada consumidor externo necesita su <b>clave</b> para llamar al buscador
        (<code>POST /buscar</code>) y descargar archivos. El servidor guarda solo el <b>hash</b>:
        la clave se muestra <b>una sola vez</b> al generarla — cópiala en el momento.
        Sin ninguna clave, el endpoint está <b>abierto</b>; al crear la primera queda <b>cerrado</b>.
      </p>

      {error && <div className="banner-error">{error}</div>}

      {generada && (
        <div className="banner-aviso" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <span>
            🔑 Clave de <b>{generada.nombre}</b> — cópiala ahora, no se vuelve a mostrar:
          </span>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <code style={{ flex: 1, wordBreak: "break-all", userSelect: "all" }}>
              {generada.clave}
            </code>
            <button
              className="boton"
              onClick={() => {
                navigator.clipboard.writeText(generada.clave);
                setCopiada(true);
              }}
            >
              {copiada ? "copiada ✓" : "copiar"}
            </button>
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "center", margin: "12px 0", maxWidth: 640 }}>
        <input
          value={nombre}
          placeholder="nombre del consumidor (p. ej. reddoor)"
          onChange={(e) => setNombre(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && generar()}
          style={{ flex: 1 }}
        />
        <button className="boton-primario" onClick={generar}>
          Generar clave
        </button>
      </div>

      <table className="tabla-claves" style={{ width: "100%", maxWidth: 640 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Consumidor</th>
            <th style={{ textAlign: "left" }}>Creada</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {claves.length === 0 && (
            <tr>
              <td colSpan={3}>
                <span className="panel-nota">Sin claves — el endpoint está abierto.</span>
              </td>
            </tr>
          )}
          {claves.map((c) => (
            <tr key={c.nombre}>
              <td>{c.nombre}</td>
              <td className="mono">{c.creada_en ? c.creada_en.replace("T", " ").slice(0, 16) : "—"}</td>
              <td style={{ textAlign: "right" }}>
                <button className="boton" onClick={() => revocar(c.nombre)}>
                  revocar
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
