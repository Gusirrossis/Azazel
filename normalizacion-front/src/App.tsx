import { useEffect, useState } from "react";
import { estadisticas, topologia } from "./api";
import type { Topologia } from "./api";
import type { Estadisticas } from "./tipos";
import { ProveedorSesion, alcanza, useSesion } from "./contexto/Sesion";
import Cabecera from "./componentes/Cabecera";
import Login from "./componentes/Login";
import CambioContrasena from "./componentes/CambioContrasena";
import Ingesta from "./componentes/Ingesta";
import Tablero from "./componentes/tablero/Tablero";
import Busqueda from "./componentes/Busqueda";
import Corridas from "./componentes/Corridas";
import ExploradorCola from "./componentes/ExploradorCola";
import Filtro from "./componentes/Filtro";
import Entidades from "./componentes/Entidades";
import ClavesBusqueda from "./componentes/ClavesBusqueda";
import Usuarios from "./componentes/Usuarios";
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

// Rol mínimo por pestaña. Es una cortesía visual, NO la autorización: quien la
// impone es el backend en cada endpoint. Esconder aquí lo que allí se rechazaría
// evita ofrecer botones que solo pueden terminar en un 403.
const ROL_POR_PESTANA: Partial<Record<Pestana, "lector" | "operador" | "admin">> = {
  filtro: "operador",
  acceso: "admin",
};

function Panel() {
  const { identidad } = useSesion();
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
    if (necesita && topo && !topo.capacidades[necesita]) return false;
    const rolMinimo = ROL_POR_PESTANA[p.clave];
    if (rolMinimo && !alcanza(identidad?.rol, rolMinimo)) return false;
    return true;
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
      {/* Acceso reúne las dos formas de entrar: personas (usuarios) y máquinas
          (claves con nombre). Verlas juntas evita la confusión de creer que una
          clave sirve para entrar al panel — ya no. */}
      {pestana === "acceso" && (
        <>
          <Usuarios />
          <ClavesBusqueda />
        </>
      )}
    </div>
  );
}

/** Decide entre login, cambio obligatorio de contraseña y panel. */
function Puerta() {
  const { identidad, cargando } = useSesion();

  // Sin este estado intermedio el login parpadea en cada recarga, antes de que
  // vuelva la comprobación de la cookie.
  if (cargando) return <div className="sesion-cargando">Comprobando sesión…</div>;
  if (!identidad) return <Login />;
  if (identidad.debe_cambiar) return <CambioContrasena />;
  return <Panel />;
}

export default function App() {
  return (
    <ProveedorSesion>
      <Puerta />
    </ProveedorSesion>
  );
}
