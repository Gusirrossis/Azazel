// Tarjeta KPI del tablero: un número grande que se entiende en medio segundo.
// `tono` marca la severidad (los errores gritan en rojo, lo sano queda sobrio).

import type { Tono } from "../../motivos";

export default function Kpi({
  valor,
  etiqueta,
  sub,
  tono = "gris",
  onClick,
}: {
  valor: string;
  etiqueta: string;
  sub?: string;
  tono?: Tono;
  onClick?: () => void;
}) {
  const Cuerpo = (
    <>
      <span className={`kpi-valor kpi-${tono}`}>{valor}</span>
      <span className="kpi-etiqueta">{etiqueta}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </>
  );
  if (onClick) {
    return (
      <button className={`kpi kpi-clicable tono-${tono}`} onClick={onClick}>
        {Cuerpo}
      </button>
    );
  }
  return <div className={`kpi tono-${tono}`}>{Cuerpo}</div>;
}
