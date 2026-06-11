import { useEffect, useRef, useState } from "react";
import { autocompletar } from "../api";

export interface Filtros {
  texto?: string;
  tipo_real?: string;
  extension?: string;
  disco_id?: string;
  puntaje_min?: number;
}

interface Props {
  filtros: Filtros;
  onBuscar: (f: Filtros) => void;
  cargando: boolean;
}

export default function Buscador({ filtros, onBuscar, cargando }: Props) {
  const [texto, setTexto] = useState(filtros.texto ?? "");
  const [puntajeMin, setPuntajeMin] = useState<string>(
    filtros.puntaje_min?.toString() ?? "",
  );
  const [sugerencias, setSugerencias] = useState<string[]>([]);
  const temporizador = useRef<number | undefined>(undefined);

  // Autocompletar con debounce (250 ms) contra GET /autocompletar
  useEffect(() => {
    window.clearTimeout(temporizador.current);
    if (texto.trim().length < 2) {
      setSugerencias([]);
      return;
    }
    temporizador.current = window.setTimeout(() => {
      autocompletar(texto.trim())
        .then(setSugerencias)
        .catch(() => setSugerencias([]));
    }, 250);
    return () => window.clearTimeout(temporizador.current);
  }, [texto]);

  const enviar = (e: React.FormEvent) => {
    e.preventDefault();
    onBuscar({
      ...filtros,
      texto: texto.trim() || undefined,
      puntaje_min: puntajeMin ? Number(puntajeMin) : undefined,
    });
  };

  return (
    <form className="buscador" onSubmit={enviar}>
      <input
        className="campo-texto"
        list="sugerencias"
        placeholder="Buscar por nombre de archivo o por CONTENIDO… (ej. el nombre de una persona)"
        value={texto}
        onChange={(e) => setTexto(e.target.value)}
        autoFocus
      />
      <datalist id="sugerencias">
        {sugerencias.map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>
      <input
        className="campo-puntaje"
        type="number"
        min={0}
        max={100}
        placeholder="puntaje ≥"
        value={puntajeMin}
        onChange={(e) => setPuntajeMin(e.target.value)}
        title="Puntaje mínimo del filtro de precalificación (1-100)"
      />
      <button type="submit" disabled={cargando}>
        {cargando ? "Buscando…" : "Buscar"}
      </button>
    </form>
  );
}
