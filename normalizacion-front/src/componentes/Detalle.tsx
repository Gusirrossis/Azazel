import { formatearBytes, urlContenido } from "../api";
import type { DocumentoArchivo } from "../tipos";
import Senales from "./Senales";
import Veredicto from "./Veredicto";
import UbicacionOriginal from "./UbicacionOriginal";

interface Props {
  doc: DocumentoArchivo;
  destinos?: Record<string, string> | null;
  onCerrar: () => void;
}

function Fila({ etiqueta, valor }: { etiqueta: string; valor: React.ReactNode }) {
  if (valor === null || valor === undefined || valor === "") return null;
  return (
    <div className="detalle-fila">
      <span className="detalle-etiqueta">{etiqueta}</span>
      <span className="detalle-valor">{valor}</span>
    </div>
  );
}

export default function Detalle({ doc, destinos, onCerrar }: Props) {
  const calidad = doc.perfil_calidad;
  return (
    <aside className="detalle">
      <div className="detalle-encabezado">
        <h2 title={doc.nombre}>{doc.nombre}</h2>
        <button className="cerrar" onClick={onCerrar} aria-label="Cerrar">
          ×
        </button>
      </div>

      <a className="boton-descargar" href={urlContenido(doc.archivo_id)} download={doc.nombre}>
        ⬇ Descargar original
      </a>
      <p className="nota-descarga">
        El original baja del almacén permanente — el disco físico ya fue desechado.
      </p>

      <Veredicto
        rutaDecision={doc.ruta_decision}
        puntaje={doc.puntaje}
        motivo={doc.motivo}
        tier={typeof doc.senales.tier === "string" ? doc.senales.tier : null}
      />

      <Fila etiqueta="Tipo real" valor={doc.tipo_real} />
      <Fila etiqueta="Ruta original" valor={doc.ruta_original} />
      <Fila etiqueta="Tamaño" valor={formatearBytes(doc.tamano)} />
      <Fila etiqueta="Modificado" valor={new Date(doc.mtime).toLocaleString()} />
      <Fila etiqueta="Puntaje del filtro" valor={doc.puntaje} />
      <Fila etiqueta="Motivo" valor={doc.motivo} />
      <Fila etiqueta="Versión del filtro" valor={doc.version_filtro} />
      <Fila etiqueta="Hash (sha256)" valor={<code>{doc.hash_contenido?.slice(0, 16)}…</code>} />
      {doc.limites_alcanzados.length > 0 && (
        <Fila etiqueta="Avisos" valor={doc.limites_alcanzados.join(", ")} />
      )}

      <UbicacionOriginal
        hash={doc.hash_contenido}
        rutaDecision={doc.ruta_decision}
        destinos={destinos ?? null}
      />

      {Object.keys(doc.senales).length > 0 && (
        <>
          <h3>Señales del filtro</h3>
          <Senales senales={doc.senales} />
        </>
      )}

      {Object.keys(doc.campos_extraidos).length > 0 && (
        <>
          <h3>Campos extraídos</h3>
          <pre>{JSON.stringify(doc.campos_extraidos, null, 2)}</pre>
        </>
      )}
      {calidad && (
        <>
          <h3>Perfil de calidad</h3>
          <pre>{JSON.stringify(calidad, null, 2)}</pre>
        </>
      )}
      {doc.texto_indexable && (
        <>
          <h3>Texto extraído</h3>
          <pre className="texto-extraido">{doc.texto_indexable.slice(0, 2000)}</pre>
        </>
      )}
    </aside>
  );
}
