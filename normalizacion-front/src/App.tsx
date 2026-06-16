import { useEffect, useState } from "react";
import { estadisticas } from "./api";
import type { Estadisticas } from "./tipos";
import Cabecera from "./componentes/Cabecera";
import Ingesta from "./componentes/Ingesta";
import Tablero from "./componentes/tablero/Tablero";
import Busqueda from "./componentes/Busqueda";
import Corridas from "./componentes/Corridas";
import ExploradorCola from "./componentes/ExploradorCola";
import Filtro from "./componentes/Filtro";
import Entidades from "./componentes/Entidades";

type Pestana = "inicio" | "busqueda" | "entidades" | "corridas" | "archivos" | "errores" | "filtro";

const PESTANAS: { clave: Pestana; etiqueta: string }[] = [
  { clave: "inicio", etiqueta: "Inicio" },
  { clave: "busqueda", etiqueta: "Búsqueda" },
  { clave: "entidades", etiqueta: "Entidades" },
  { clave: "corridas", etiqueta: "Corridas" },
  { clave: "archivos", etiqueta: "Archivos" },
  { clave: "errores", etiqueta: "Errores" },
  { clave: "filtro", etiqueta: "Filtro" },
];

export default function App() {
  const [pestana, setPestana] = useState<Pestana>("inicio");
  const [stats, setStats] = useState<Estadisticas | null>(null);

  useEffect(() => {
    estadisticas().then(setStats).catch(() => setStats(null));
  }, []);

  return (
    <div className="aplicacion">
      <Cabecera stats={stats} />
      <nav className="pestanas" aria-label="Secciones">
        {PESTANAS.map((p) => (
          <button
            key={p.clave}
            className={pestana === p.clave ? "pestana activa" : "pestana"}
            onClick={() => setPestana(p.clave)}
          >
            {p.etiqueta}
          </button>
        ))}
      </nav>

      {/* Inicio: el tablero de control + la barra de ingesta (lanzar/ver corrida) */}
      {pestana === "inicio" && (
        <>
          <Ingesta onIrACorridas={() => setPestana("corridas")} />
          <Tablero onIrA={(destino) => setPestana(destino)} />
        </>
      )}

      {/* Búsqueda: montada siempre (oculta) para conservar resultados al cambiar de pestaña */}
      <div style={{ display: pestana === "busqueda" ? undefined : "none" }}>
        <Busqueda />
      </div>

      {pestana === "entidades" && <Entidades />}
      {pestana === "corridas" && <Corridas />}
      {pestana === "archivos" && <ExploradorCola modo="todos" />}
      {pestana === "errores" && <ExploradorCola modo="errores" />}
      {pestana === "filtro" && <Filtro />}
    </div>
  );
}
