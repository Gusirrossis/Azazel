// Tarjeta KPI del tablero: un número grande que se entiende en medio segundo.
// `tono` marca la severidad; `tamano` da la jerarquía (grande = salud del
// sistema, chico = métrica secundaria). Si es clicable lo dice con una flecha y
// un hover evidente — la afford­ance no se adivina.

import type { Tono } from "../../motivos";

export default function Kpi({
  valor,
  etiqueta,
  sub,
  tono = "gris",
  tamano = "grande",
  destino,
  onClick,
  title,
}: {
  valor: string;
  etiqueta: string;
  sub?: string;
  tono?: Tono;
  tamano?: "grande" | "chico";
  destino?: string; // texto del enlace, p. ej. "ver Errores"
  onClick?: () => void;
  title?: string;
}) {
  const clases = `kpi kpi-${tamano} tono-${tono}${onClick ? " kpi-clicable" : ""}`;
  const cuerpo = (
    <>
      <span className={`kpi-valor kpi-${tono}`}>{valor}</span>
      <span className="kpi-etiqueta">{etiqueta}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
      {onClick && destino && <span className="kpi-enlace">{destino} →</span>}
    </>
  );
  if (onClick) {
    return (
      <button className={clases} onClick={onClick} title={title}>
        {cuerpo}
      </button>
    );
  }
  return (
    <div className={clases} title={title}>
      {cuerpo}
    </div>
  );
}
