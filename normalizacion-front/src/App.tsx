import { useCallback, useEffect, useState } from "react";
import { buscar, estadisticas } from "./api";
import type { DocumentoArchivo, Estadisticas, RespuestaBusqueda, SolicitudBusqueda } from "./tipos";
import Buscador, { type Filtros } from "./componentes/Buscador";
import Facetas from "./componentes/Facetas";
import Resultados from "./componentes/Resultados";
import Detalle from "./componentes/Detalle";
import Cabecera from "./componentes/Cabecera";
import Ingesta from "./componentes/Ingesta";
import Panel from "./componentes/Panel";
import Corridas from "./componentes/Corridas";
import ExploradorCola from "./componentes/ExploradorCola";
import Filtro from "./componentes/Filtro";

type Pestana = "inicio" | "corridas" | "archivos" | "errores" | "filtro";

const PESTANAS: { clave: Pestana; etiqueta: string }[] = [
  { clave: "inicio", etiqueta: "Inicio" },
  { clave: "corridas", etiqueta: "Corridas" },
  { clave: "archivos", etiqueta: "Archivos" },
  { clave: "errores", etiqueta: "Errores" },
  { clave: "filtro", etiqueta: "Filtro" },
];

export default function App() {
  const [pestana, setPestana] = useState<Pestana>("inicio");
  const [filtros, setFiltros] = useState<Filtros>({});
  const [docs, setDocs] = useState<DocumentoArchivo[]>([]);
  const [total, setTotal] = useState(0);
  const [facetas, setFacetas] = useState<RespuestaBusqueda["facetas"]>(null);
  const [cursor, setCursor] = useState<unknown[] | null>(null);
  const [seleccionado, setSeleccionado] = useState<DocumentoArchivo | null>(null);
  const [stats, setStats] = useState<Estadisticas | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  const ejecutarBusqueda = useCallback(
    async (f: Filtros, cursorPagina: unknown[] | null = null) => {
      setCargando(true);
      setError(null);
      try {
        const solicitud: SolicitudBusqueda = {
          ...f,
          facetas: true,
          tamano_pagina: 20,
          cursor: cursorPagina,
        };
        const r = await buscar(solicitud);
        setDocs((previos) => (cursorPagina ? [...previos, ...r.documentos] : r.documentos));
        setTotal(r.total);
        setFacetas(r.facetas);
        setCursor(r.documentos.length > 0 ? r.cursor : null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setCargando(false);
      }
    },
    [],
  );

  useEffect(() => {
    estadisticas().then(setStats).catch(() => setStats(null));
    void ejecutarBusqueda({});
  }, [ejecutarBusqueda]);

  const aplicarFiltros = (f: Filtros) => {
    setFiltros(f);
    void ejecutarBusqueda(f);
  };

  const filtrarPorFaceta = (campo: "tipo_real" | "extension" | "disco_id", valor: string) => {
    const f = { ...filtros, [campo]: filtros[campo] === valor ? undefined : valor };
    aplicarFiltros(f);
  };

  const refrescarTodo = useCallback(() => {
    estadisticas().then(setStats).catch(() => null);
    void ejecutarBusqueda(filtros);
  }, [ejecutarBusqueda, filtros]);

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
      {error && (
        <div className="banner-error">
          {error} — ¿está arriba la API? (<code>norm api</code>)
        </div>
      )}
      {pestana === "inicio" && (
        <>
          <Ingesta
            onCompletada={refrescarTodo}
            onProgreso={refrescarTodo}
            onIrACorridas={() => setPestana("corridas")}
          />
          <Panel />
          <Buscador filtros={filtros} onBuscar={aplicarFiltros} cargando={cargando} />
          <main className="cuerpo">
            <Facetas facetas={facetas} filtros={filtros} onFiltrar={filtrarPorFaceta} />
            <Resultados
              documentos={docs}
              total={total}
              hayMas={cursor !== null && docs.length < total}
              cargando={cargando}
              onSeleccionar={setSeleccionado}
              onCargarMas={() => void ejecutarBusqueda(filtros, cursor)}
            />
            {seleccionado && (
              <Detalle doc={seleccionado} onCerrar={() => setSeleccionado(null)} />
            )}
          </main>
        </>
      )}
      {pestana === "corridas" && <Corridas />}
      {pestana === "archivos" && <ExploradorCola modo="todos" />}
      {pestana === "errores" && <ExploradorCola modo="errores" />}
      {pestana === "filtro" && <Filtro />}
    </div>
  );
}
