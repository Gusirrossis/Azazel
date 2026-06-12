// Inventario de contenedores PRESERVADOS sin explorar (cifrados, corruptos,
// formatos pendientes, guards anti-bomba) — nada se pierde, esta vista lo enseña.

import { useState } from "react";
import { formatearBytes, preservados } from "../../api";
import type { RespuestaPreservados } from "../../tipos";

// Motivos del inventario "preservados sin explorar", en cristiano
const MOTIVO_PRESERVADO: Record<string, string> = {
  contenedor_corrupto: "corrupto o con contraseña",
  formato_no_soportado: "formato aún no soportado (RAR sin herramienta, imagen de disco…)",
  contenedor_sin_explorar: "pendiente de exploración",
  profundidad_maxima: "anidación más honda que el tope",
};

export function etiquetaMotivo(motivo: string): string {
  if (motivo.startsWith("zip_bomb_sospechoso:")) return "sospecha de bomba (guard)";
  return MOTIVO_PRESERVADO[motivo] ?? motivo;
}

export default function Preservados() {
  const [datos, setDatos] = useState<RespuestaPreservados | null>(null);
  const [abierto, setAbierto] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cargar = () => {
    preservados()
      .then((r) => {
        setDatos(r);
        setAbierto(true);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  };

  return (
    <div className="preservados">
      <div className="preservados-barra">
        <button className="enlace" onClick={() => (abierto ? setAbierto(false) : cargar())}>
          {abierto ? "🔒 preservados sin explorar ▴" : "🔒 preservados sin explorar ▾"}
        </button>
        {abierto && datos && (
          <span className="sin-sub">
            {datos.total.toLocaleString()} contenedores — nada se pierde: íntegros en el
            almacén o en frío reversible
          </span>
        )}
      </div>
      {error && <div className="banner-error">{error}</div>}
      {abierto && datos && datos.total === 0 && (
        <div className="sin-sub">ninguno — todos los contenedores se exploraron 🎉</div>
      )}
      {abierto && datos && datos.total > 0 && (
        <>
          <div className="preservados-conteos">
            {Object.entries(datos.por_motivo).map(([motivo, n]) => (
              <span key={motivo} className="chip medio">
                {etiquetaMotivo(motivo)}: <b>{n.toLocaleString()}</b>
              </span>
            ))}
          </div>
          <div className="preservados-lista">
            {datos.archivos.map((a) => (
              <div key={`${a.disco_id}:${a.ruta}`} className="preservado-fila" title={a.ruta}>
                <span className="preservado-nombre">📦 {a.nombre}</span>
                <span className="preservado-tamano">{formatearBytes(a.tamano)}</span>
                <span className="preservado-motivo">{etiquetaMotivo(a.motivo)}</span>
                <span className={a.estado === "COLD" ? "chip medio" : "chip ok"}>
                  {a.estado === "COLD" ? "en frío (reversible)" : "íntegro en almacén"}
                </span>
              </div>
            ))}
            {datos.total > datos.archivos.length && (
              <div className="sin-sub">
                mostrando los {datos.archivos.length} más grandes de {datos.total.toLocaleString()}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
