import { formatearBytes } from "../api";
import type { DocumentoArchivo } from "../tipos";

interface Props {
  documentos: DocumentoArchivo[];
  total: number;
  hayMas: boolean;
  cargando: boolean;
  onSeleccionar: (doc: DocumentoArchivo) => void;
  onCargarMas: () => void;
}

function Fragmento({ texto }: { texto: string }) {
  // Los marcadores ⟪…⟫ vienen del servidor; aquí se vuelven <mark> SIN innerHTML
  // (el contenido de archivos jamás se interpreta como HTML)
  const partes = texto.split(/(⟪[^⟫]*⟫)/g);
  return (
    <span>
      {partes.map((p, i) =>
        p.startsWith("⟪") ? <mark key={i}>{p.slice(1, -1)}</mark> : <span key={i}>{p}</span>,
      )}
    </span>
  );
}

function ChipCalidad({ doc }: { doc: DocumentoArchivo }) {
  const score = doc.perfil_calidad?.["quality_score"];
  if (typeof score !== "number") return null;
  const clase = score >= 80 ? "chip ok" : score >= 50 ? "chip medio" : "chip bajo";
  return <span className={clase}>calidad {score}</span>;
}

export default function Resultados({
  documentos,
  total,
  hayMas,
  cargando,
  onSeleccionar,
  onCargarMas,
}: Props) {
  return (
    <section className="resultados">
      <div className="resultados-resumen">
        <b>{total.toLocaleString()}</b> resultados
      </div>
      <table>
        <thead>
          <tr>
            <th>Nombre</th>
            <th>Tipo real</th>
            <th>Tamaño</th>
            <th>Puntaje</th>
            <th>Calidad</th>
            <th>Disco</th>
          </tr>
        </thead>
        <tbody>
          {documentos.map((doc) => (
            <tr key={doc.archivo_id} onClick={() => onSeleccionar(doc)}>
              <td className="celda-nombre">
                {doc.nombre}
                {doc._resaltado && doc._resaltado.length > 0 && (
                  <div className="resaltados">
                    {doc._resaltado.map((f, i) => (
                      <div key={i} className="resaltado">
                        …<Fragmento texto={f} />…
                      </div>
                    ))}
                  </div>
                )}
                {doc.senales["extension_miente"] === true && (
                  <span className="chip alerta" title="La extensión no corresponde al contenido">
                    ext. miente
                  </span>
                )}
                {doc.limites_alcanzados.length > 0 && (
                  <span className="chip aviso" title={doc.limites_alcanzados.join(", ")}>
                    ⚑
                  </span>
                )}
              </td>
              <td className="celda-tipo">{doc.tipo_real ?? "—"}</td>
              <td>{formatearBytes(doc.tamano)}</td>
              <td>{doc.puntaje ?? "—"}</td>
              <td>
                <ChipCalidad doc={doc} />
              </td>
              <td>{doc.disco_id}</td>
            </tr>
          ))}
          {documentos.length === 0 && !cargando && (
            <tr>
              <td colSpan={6} className="vacio">
                Sin resultados.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {hayMas && (
        <button className="cargar-mas" onClick={onCargarMas} disabled={cargando}>
          {cargando ? "Cargando…" : "Cargar más"}
        </button>
      )}
    </section>
  );
}
