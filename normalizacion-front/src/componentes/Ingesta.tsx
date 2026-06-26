import { useCallback, useEffect, useRef, useState } from "react";
import {
  carpetas,
  crearCarpeta,
  ejecutarPipeline,
  estadoPipeline,
  type AmbitoCarpetas,
} from "../api";
import type { EstadoPipeline } from "../tipos";
import VistaCorrida, { NOMBRES_FASE } from "./corridas/VistaCorrida";
import Recursos from "./Recursos";

function SelectorCarpeta({
  ambito,
  titulo,
  accion,
  inicial,
  onSeleccionar,
  onCerrar,
}: {
  ambito: AmbitoCarpetas;
  titulo: string;
  accion: string;
  inicial?: string | null;
  onSeleccionar: (ruta: string) => void;
  onCerrar: () => void;
}) {
  const [ruta, setRuta] = useState<string>("");
  const [padre, setPadre] = useState<string | null>(null);
  const [hijas, setHijas] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [nombreNueva, setNombreNueva] = useState("");

  const aplicar = useCallback((r: { ruta: string; padre: string | null; carpetas: string[] }) => {
    setRuta(r.ruta);
    setPadre(r.padre);
    setHijas(r.carpetas);
    setError(null);
  }, []);

  const navegar = useCallback(
    (destino?: string) => {
      carpetas(destino, ambito)
        .then(aplicar)
        .catch((e) => setError(String(e)));
    },
    [ambito, aplicar],
  );

  // Arranca donde el usuario eligió la última vez (si sigue siendo navegable)
  useEffect(() => {
    if (inicial) {
      carpetas(inicial, ambito)
        .then(aplicar)
        .catch(() => navegar());
    } else {
      navegar();
    }
  }, [ambito, inicial, aplicar, navegar]);

  const crear = () => {
    crearCarpeta(ruta, nombreNueva.trim())
      .then((r) => {
        aplicar(r); // el servidor devuelve ya el listado DENTRO de la nueva carpeta
        setNombreNueva("");
      })
      .catch((e) => setError(String(e)));
  };

  // El separador correcto lo decide el SERVIDOR (Mac/Linux "/", Windows "\")
  const separador = ruta.includes("\\") ? "\\" : "/";

  return (
    <div className="modal-fondo" onClick={onCerrar}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{titulo}</h3>
        <div className="ruta-actual" title={ruta}>
          {ruta}
        </div>
        {error && <div className="banner-error">{error}</div>}
        <ul className="lista-carpetas">
          {padre && (
            <li>
              <button onClick={() => navegar(padre)}>⬆ ..</button>
            </li>
          )}
          {hijas.map((nombre) => (
            <li key={nombre}>
              <button onClick={() => navegar(`${ruta}${separador}${nombre}`)}>📁 {nombre}</button>
            </li>
          ))}
          {hijas.length === 0 && <li className="sin-sub">sin subcarpetas</li>}
        </ul>
        {ambito === "destino" && (
          <div className="crear-carpeta">
            <input
              value={nombreNueva}
              placeholder="nombre de carpeta nueva…"
              onChange={(e) => setNombreNueva(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && nombreNueva.trim() && crear()}
            />
            <button className="secundario" disabled={!nombreNueva.trim()} onClick={crear}>
              ➕ Crear aquí
            </button>
          </div>
        )}
        <div className="modal-acciones">
          <button className="secundario" onClick={onCerrar}>
            Cancelar
          </button>
          <button className="primario" onClick={() => onSeleccionar(ruta)}>
            {accion}
          </button>
        </div>
      </div>
    </div>
  );
}

const CLAVE_DESTINO = "norm_destino_elegido";
const CLAVE_WORKERS = "norm_workers_elegidos";

function ModalIndexar({
  destinoEligible,
  workersAuto,
  origenInicial,
  destinoInicial,
  onLanzar,
  onCerrar,
}: {
  destinoEligible: boolean;
  workersAuto: number;
  // De la última corrida (Postgres): precargan el modal para no re-elegir todo.
  origenInicial?: string | null;
  destinoInicial?: string | null;
  onLanzar: (origen: string, destino: string | null, workers: number | null) => void;
  onCerrar: () => void;
}) {
  // Origen: el de la última corrida; Destino: el de la última corrida o, si no,
  // el último elegido en este navegador (localStorage).
  const [origen, setOrigen] = useState<string | null>(origenInicial ?? null);
  const [destino, setDestino] = useState<string | null>(
    () => destinoInicial ?? localStorage.getItem(CLAVE_DESTINO) ?? null,
  );
  const [usarDestino, setUsarDestino] = useState<boolean>(() =>
    Boolean(destinoInicial ?? localStorage.getItem(CLAVE_DESTINO)),
  );
  const [workers, setWorkers] = useState<number | null>(() => {
    const guardado = Number(localStorage.getItem(CLAVE_WORKERS));
    return guardado >= 1 ? guardado : null; // null = automático
  });
  const [navegando, setNavegando] = useState<"origen" | "destino" | null>(null);

  if (navegando === "origen") {
    return (
      <SelectorCarpeta
        ambito="datos"
        titulo="Carpeta a observar (origen)"
        accion="Usar esta carpeta"
        inicial={origen}
        onSeleccionar={(r) => {
          setOrigen(r);
          setNavegando(null);
        }}
        onCerrar={() => setNavegando(null)}
      />
    );
  }
  if (navegando === "destino") {
    return (
      <SelectorCarpeta
        ambito="destino"
        titulo="Carpeta de destino (almacén + frío)"
        accion="Guardar aquí"
        inicial={destino}
        onSeleccionar={(r) => {
          setDestino(r);
          setUsarDestino(true);
          setNavegando(null);
        }}
        onCerrar={() => setNavegando(null)}
      />
    );
  }

  const lanzar = () => {
    if (!origen) return;
    const elegido = usarDestino && destino ? destino : null;
    if (elegido) localStorage.setItem(CLAVE_DESTINO, elegido);
    else localStorage.removeItem(CLAVE_DESTINO);
    if (workers) localStorage.setItem(CLAVE_WORKERS, String(workers));
    else localStorage.removeItem(CLAVE_WORKERS);
    onLanzar(origen, elegido, workers);
  };

  return (
    <div className="modal-fondo" onClick={onCerrar}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Nueva indexación</h3>

        <div className="campo-carpeta">
          <span className="campo-etiqueta">Origen (carpeta a observar)</span>
          <div className="campo-valor">
            <code title={origen ?? ""}>{origen ?? "sin elegir"}</code>
            <button className="secundario" onClick={() => setNavegando("origen")}>
              Examinar…
            </button>
          </div>
        </div>

        <div className="campo-carpeta">
          <span className="campo-etiqueta">Destino del indexado (almacén + frío)</span>
          {destinoEligible ? (
            <>
              <label className="opcion-destino">
                <input
                  type="radio"
                  checked={!usarDestino}
                  onChange={() => setUsarDestino(false)}
                />
                Almacén configurado (recomendado)
              </label>
              <label className="opcion-destino">
                <input
                  type="radio"
                  checked={usarDestino}
                  onChange={() => setUsarDestino(true)}
                />
                Carpeta elegida:
                <code title={destino ?? ""}>{destino ?? "sin elegir"}</code>
                <button className="secundario" onClick={() => setNavegando("destino")}>
                  Examinar…
                </button>
              </label>
            </>
          ) : (
            <div className="campo-valor">
              <code>almacén configurado (.env)</code>
              <span className="sin-sub">
                monta NORM_CARPETA_DESTINO para poder elegir carpeta
              </span>
            </div>
          )}
        </div>

        <div className="campo-carpeta">
          <span className="campo-etiqueta">Workers en paralelo (procesos)</span>
          <label className="opcion-destino">
            <input type="radio" checked={workers === null} onChange={() => setWorkers(null)} />
            Automático ({workersAuto} — según los núcleos del servidor)
          </label>
          <label className="opcion-destino">
            <input
              type="radio"
              checked={workers !== null}
              onChange={() => setWorkers(workers ?? workersAuto)}
            />
            Fijo:
            <input
              type="number"
              min={1}
              max={64}
              className="numero-workers"
              value={workers ?? workersAuto}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (n >= 1 && n <= 64) setWorkers(n);
              }}
            />
          </label>
        </div>

        <div className="modal-acciones">
          <button className="secundario" onClick={onCerrar}>
            Cancelar
          </button>
          <button
            className="primario"
            disabled={!origen || (usarDestino && !destino)}
            onClick={lanzar}
          >
            Indexar
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Ingesta({
  onCompletada,
  onProgreso,
  onIrACorridas,
}: {
  onCompletada?: () => void;
  onProgreso?: () => void;
  onIrACorridas?: () => void;
}) {
  const [estado, setEstado] = useState<EstadoPipeline | null>(null);
  const [selectorAbierto, setSelectorAbierto] = useState(false);
  const [desplegado, setDesplegado] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const habiaEnCurso = useRef(false);
  const ticks = useRef(0);

  const refrescar = useCallback(() => {
    estadoPipeline()
      .then((e) => {
        setEstado(e);
        if (habiaEnCurso.current && !e.en_curso) onCompletada?.(); // terminó → avisar
        if (e.en_curso && onProgreso) {
          // Mientras se indexa, la búsqueda se refresca sola (~cada 5s):
          // los documentos van apareciendo conforme el worker los indexa
          ticks.current += 1;
          if (ticks.current % 2 === 0) onProgreso();
        }
        habiaEnCurso.current = e.en_curso !== null;
      })
      .catch(() => setEstado(null));
  }, [onCompletada, onProgreso]);

  useEffect(() => {
    refrescar();
    const intervalo = window.setInterval(refrescar, 2500);
    return () => window.clearInterval(intervalo);
  }, [refrescar]);

  const lanzar = (ruta: string, destino: string | null = null, workers: number | null = null) => {
    setSelectorAbierto(false);
    setError(null);
    setDesplegado(true);
    ejecutarPipeline(ruta, destino, workers)
      .then(() => refrescar())
      .catch((e) => setError(String(e)));
  };

  const ultima = estado?.historial[0];
  return (
    <section className="ingesta">
      <div className="ingesta-barra">
        <button className="primario" onClick={() => setSelectorAbierto(true)}>
          📂 Indexar carpeta…
        </button>
        {ultima && estado && !estado.en_curso && (
          <button
            className="secundario"
            onClick={() => lanzar(ultima.ruta, ultima.destino)}
            title="Carpeta viva: solo lo nuevo/cambiado genera trabajo (mismo destino)"
          >
            ↻ Re-indexar {ultima.ruta.split(/[\\/]/).pop()}
          </button>
        )}
        <button className="enlace" onClick={() => setDesplegado(!desplegado)}>
          {desplegado ? "ocultar detalle ▴" : "detalle de la corrida ▾"}
        </button>
        {onIrACorridas && (
          <button className="enlace" onClick={onIrACorridas}>
            historial completo →
          </button>
        )}
        {estado?.en_curso && (
          <span className="chip medio">
            ⟳ corriendo: {NOMBRES_FASE[estado.en_curso.fase_actual ?? ""] ?? "…"} — los
            resultados de búsqueda se actualizan en vivo
          </span>
        )}
      </div>
      {error && <div className="banner-error">{error}</div>}

      {desplegado && estado && (
        <div className="ingesta-detalle">
          <Recursos />
          {estado.en_curso ? (
            <VistaCorrida corrida={estado.en_curso} progreso={estado.progreso} />
          ) : (
            <div className="sin-sub">
              sin corrida en curso — el historial, destinos y preservados viven en la
              pestaña Corridas
            </div>
          )}
        </div>
      )}

      {selectorAbierto && (
        <ModalIndexar
          destinoEligible={estado?.destino_eligible ?? false}
          workersAuto={estado?.workers_auto ?? 1}
          origenInicial={ultima?.ruta}
          destinoInicial={ultima?.destino}
          onLanzar={lanzar}
          onCerrar={() => setSelectorAbierto(false)}
        />
      )}
    </section>
  );
}
