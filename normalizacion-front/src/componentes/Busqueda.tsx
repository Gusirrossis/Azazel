// Pestaña Búsqueda: el buscador completo (texto + facetas + resultados +
// detalle) como página autónoma. Su estado vive aquí — App lo mantiene montado
// (oculto) al cambiar de pestaña para no perder la búsqueda en curso.

import { useCallback, useEffect, useState } from "react";
import { buscar, estadoPipeline } from "../api";
import type { DocumentoArchivo, RespuestaBusqueda, SolicitudBusqueda } from "../tipos";
import Buscador, { type Filtros } from "./Buscador";
import Facetas from "./Facetas";
import Resultados from "./Resultados";
import Detalle from "./Detalle";

export default function Busqueda() {
  const [filtros, setFiltros] = useState<Filtros>({});
  const [docs, setDocs] = useState<DocumentoArchivo[]>([]);
  const [total, setTotal] = useState(0);
  const [facetas, setFacetas] = useState<RespuestaBusqueda["facetas"]>(null);
  const [cursor, setCursor] = useState<unknown[] | null>(null);
  const [seleccionado, setSeleccionado] = useState<DocumentoArchivo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);
  const [destinos, setDestinos] = useState<Record<string, string> | null>(null);

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
    void ejecutarBusqueda({});
    // dónde guarda el almacén (para mostrar la ubicación del original en el detalle)
    estadoPipeline()
      .then((e) => setDestinos(e.destinos))
      .catch(() => setDestinos(null));
  }, [ejecutarBusqueda]);

  const aplicarFiltros = (f: Filtros) => {
    setFiltros(f);
    void ejecutarBusqueda(f);
  };

  const filtrarPorFaceta = (campo: "tipo_real" | "extension" | "disco_id", valor: string) => {
    const f = { ...filtros, [campo]: filtros[campo] === valor ? undefined : valor };
    aplicarFiltros(f);
  };

  return (
    <section className="busqueda-pagina">
      {error && (
        <div className="banner-error">
          {error} — ¿está arriba la API? (<code>norm api</code>)
        </div>
      )}
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
          <Detalle
            doc={seleccionado}
            destinos={destinos}
            onCerrar={() => setSeleccionado(null)}
          />
        )}
      </main>
    </section>
  );
}
