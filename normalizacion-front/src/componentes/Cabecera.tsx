import { formatearBytes } from "../api";
import { useSesion } from "../contexto/Sesion";
import type { Estadisticas } from "../tipos";

/**
 * Cabecera del panel: identidad de Azazel, cifras del índice y quién ha entrado.
 *
 * Aquí vivía un campo `<input type="password">` donde se pegaba a mano la API key,
 * que se guardaba en `localStorage`. Lo ha sustituido la sesión: la credencial va
 * en una cookie `httpOnly` y esta cabecera solo muestra quién eres y te deja salir.
 */
export default function Cabecera({ stats }: { stats: Estadisticas | null }) {
  const { identidad, salir } = useSesion();

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
        {identidad && (
          <div className="sesion-barra">
            <span className="sesion-usuario">{identidad.nombre || identidad.usuario}</span>
            <span className="sesion-rol" title="Lo que este rol te permite hacer">
              {identidad.rol}
            </span>
            <button className="sesion-salir" onClick={salir}>
              Salir
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
