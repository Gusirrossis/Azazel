import { useEffect, useState } from "react";
import { estadisticas, topologia } from "./api";
import type { Topologia } from "./api";
import type { Estadisticas } from "./tipos";
import Cabecera from "./componentes/Cabecera";
import Ingesta from "./componentes/Ingesta";
import Tablero from "./componentes/tablero/Tablero";
import Busqueda from "./componentes/Busqueda";
import Corridas from "./componentes/Corridas";
import ExploradorCola from "./componentes/ExploradorCola";
import Filtro from "./componentes/Filtro";
import Entidades from "./componentes/Entidades";
import ClavesBusqueda from "./componentes/ClavesBusqueda";
import ResumenNodo from "./componentes/ResumenNodo";

type Pestana =
  | "inicio" | "busqueda" | "entidades" | "corridas" | "archivos" | "errores" | "filtro" | "acceso";

const PESTANAS: { clave: Pestana; etiqueta: string }[] = [
  { clave: "inicio", etiqueta: "Inicio" },
  { clave: "busqueda", etiqueta: "Búsqueda" },
  { clave: "entidades", etiqueta: "Entidades" },
  { clave: "corridas", etiqueta: "Corridas" },
  { clave: "archivos", etiqueta: "Archivos" },
  { clave: "errores", etiqueta: "Errores" },
  { clave: "filtro", etiqueta: "Filtro" },
  { clave: "acceso", etiqueta: "Acceso" },
];

// ⚙K16 — qué pestaña necesita qué capacidad. Las que no aparecen aquí las tiene
// cualquier nodo. Añadir una topología nueva no toca esta tabla: cambia lo que el
// servidor responde en /sistema/topologia.
const CAPACIDAD_POR_PESTANA: Partial<Record<Pestana, keyof Topologia["capacidades"]>> = {
  entidades: "entidades",
};

export default function App() {
  const [pestana, setPestana] = useState<Pestana>("inicio");
  const [stats, setStats] = useState<Estadisticas | null>(null);
  const [topo, setTopo] = useState<Topologia | null>(null);

  useEffect(() => {
    estadisticas().then(setStats).catch(() => setStats(null));
    // Si falla (API vieja, sin llave), `topo` queda en null y NO se oculta nada:
    // degradar mostrando de más es preferible a esconder algo que sí existe.
    topologia().then(setTopo).catch(() => setTopo(null));
  }, []);

  const visibles = PESTANAS.filter((p) => {
    const necesita = CAPACIDAD_POR_PESTANA[p.clave];
    return !necesita || !topo || topo.capacidades[necesita];
  });

  // Si la pestaña activa deja de estar disponible (llegó la topología después del
  // primer render), no dejar al usuario en una sección muerta.
  useEffect(() => {
    if (topo && !visibles.some((p) => p.clave === pestana)) setPestana("inicio");
  }, [topo, pestana, visibles]);

  return (
    <div className="aplicacion">
      <Cabecera stats={stats} />
      {topo && topo.perfil !== "local" && (
        <p className="insignia-nodo" title="Se fija al arrancar (NORM_DESPLIEGUE__PERFIL); no es editable desde aquí">
          Nodo <strong>{topo.nodo_id}</strong> · perfil {topo.perfil}
          {!topo.capacidades.archivo_maestro && " · no es el archivo maestro"}
        </p>
      )}
      <nav className="pestanas" aria-label="Secciones">
        {visibles.map((p) => (
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
          <ResumenNodo />
          <Ingesta onIrACorridas={() => setPestana("corridas")} />
          <Tablero onIrA={(destino) => setPestana(destino)} />
        </>
      )}

      {/* Búsqueda: montada siempre (oculta) para conservar resultados al cambiar de pestaña */}
      <div style={{ display: pestana === "busqueda" ? undefined : "none" }}>
        <Busqueda />
      </div>

      {pestana === "entidades" && (!topo || topo.capacidades.entidades) && <Entidades />}
      {pestana === "corridas" && <Corridas />}
      {pestana === "archivos" && <ExploradorCola modo="todos" />}
      {pestana === "errores" && <ExploradorCola modo="errores" />}
      {pestana === "filtro" && <Filtro />}
      {pestana === "acceso" && <ClavesBusqueda />}
    </div>
  );
}
