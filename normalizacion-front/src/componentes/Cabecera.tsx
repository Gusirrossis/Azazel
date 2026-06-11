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
          <div className="stats">
            <span>
              <b>{stats.total_documentos.toLocaleString()}</b> documentos
            </span>
            <span>
              <b>{formatearBytes(stats.bytes_totales)}</b> indexados
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
