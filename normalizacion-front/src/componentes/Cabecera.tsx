import { useState } from "react";
import { formatearBytes } from "../api";
import type { Estadisticas } from "../tipos";

export default function Cabecera({ stats }: { stats: Estadisticas | null }) {
  const [llave, setLlave] = useState(localStorage.getItem("norm_api_key") ?? "");

  const guardarLlave = (valor: string) => {
    setLlave(valor);
    if (valor) localStorage.setItem("norm_api_key", valor);
    else localStorage.removeItem("norm_api_key");
  };

  return (
    <header className="cabecera">
      <div>
        <h1>Azazel</h1>
        <span className="lema">Registro de archivos — lo que se indexa, permanece</span>
      </div>
      <div className="cabecera-derecha">
        {stats && (
          <div
            className="stats"
            title="Lo BUSCABLE en el índice (OpenSearch). El tablero de Inicio muestra lo CATALOGADO (toda la cola en Postgres), que incluye frío, errores y pendientes — por eso las cifras difieren."
          >
            <span>
              <b>{stats.total_documentos.toLocaleString()}</b> buscables
            </span>
            <span>
              <b>{formatearBytes(stats.bytes_totales)}</b> en el índice
            </span>
          </div>
        )}
        <input
          className="campo-llave"
          type="password"
          placeholder="API key (opcional)"
          value={llave}
          onChange={(e) => guardarLlave(e.target.value)}
          title="Se manda como X-API-Key; vacío si la API no exige auth"
        />
      </div>
    </header>
  );
}
