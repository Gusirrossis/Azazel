import type { RespuestaBusqueda } from "../tipos";
import type { Filtros } from "./Buscador";

interface Props {
  facetas: RespuestaBusqueda["facetas"];
  filtros: Filtros;
  onFiltrar: (campo: "tipo_real" | "extension" | "disco_id", valor: string) => void;
}

const TITULOS: Record<string, { etiqueta: string; campo: "tipo_real" | "extension" | "disco_id" }> =
  {
    por_tipo: { etiqueta: "Tipo real", campo: "tipo_real" },
    por_extension: { etiqueta: "Extensión", campo: "extension" },
    por_disco: { etiqueta: "Disco", campo: "disco_id" },
  };

export default function Facetas({ facetas, filtros, onFiltrar }: Props) {
  if (!facetas) return <aside className="facetas" />;
  return (
    <aside className="facetas">
      {Object.entries(facetas).map(([nombre, conteos]) => {
        const info = TITULOS[nombre];
        if (!info || Object.keys(conteos).length === 0) return null;
        return (
          <section key={nombre}>
            <h3>{info.etiqueta}</h3>
            <ul>
              {Object.entries(conteos)
                .sort(([, a], [, b]) => b - a)
                .map(([valor, cuenta]) => (
                  <li key={valor}>
                    <button
                      className={filtros[info.campo] === valor ? "faceta activa" : "faceta"}
                      onClick={() => onFiltrar(info.campo, valor)}
                      title={valor}
                    >
                      <span className="faceta-valor">{valor.split("/").pop()}</span>
                      <span className="faceta-cuenta">{cuenta}</span>
                    </button>
                  </li>
                ))}
            </ul>
          </section>
        );
      })}
    </aside>
  );
}
