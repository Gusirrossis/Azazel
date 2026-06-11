import { useCallback, useEffect, useRef, useState } from "react";
import {
  carpetas,
  crearCarpeta,
  ejecutarPipeline,
  estadoPipeline,
  formatearBytes,
  formatearDuracion,
  preservados,
  type AmbitoCarpetas,
} from "../api";
import type { Corrida, EstadoPipeline, FaseEjecutada, RespuestaPreservados } from "../tipos";

const NOMBRES_FASE: Record<string, string> = {
  catalogo: "Catálogo",
  precalificacion: "Doble filtro",
  worker: "Blobs + índice",
  mover_frio: "Mover a frío",
  verificacion: "Verificación",
  puerta: "Puerta",
};
const ORDEN_FASES = Object.keys(NOMBRES_FASE);

function resumenMetricas(m: Record<string, unknown>): string {
  const interesantes = [
    "archivos_vistos", "archivos_nuevos", "procesados", "procesos", "hot", "cold",
    "re_encolados", "blobs_nuevos", "deduplicados", "movidos", "verificados", "errores",
    "transitorios", "hechos", "pendientes",
  ];
  return interesantes
    .filter((k) => typeof m[k] === "number" && (m[k] as number) > 0)
    .map((k) => `${k.replace(/_/g, " ")}: ${m[k] as number}`)
    .join(" · ");
}

function FilaFase({ f }: { f: FaseEjecutada }) {
  return (
    <div className="fase-fila hecha">
      <span className="fase-check">✓</span>
      <span className="fase-nombre">{NOMBRES_FASE[f.fase] ?? f.fase}</span>
      <span className="fase-dur">
        {formatearDuracion(f.duracion_s)}
        {f.archivos_por_segundo ? ` · ${f.archivos_por_segundo} archivos/s` : ""}
      </span>
      <span className="fase-metricas">{resumenMetricas(f.metricas)}</span>
    </div>
  );
}

function duracionCorrida(corrida: Corrida): string | null {
  const inicio = new Date(corrida.iniciada_en).getTime();
  const fin = corrida.terminada_en ? new Date(corrida.terminada_en).getTime() : Date.now();
  if (Number.isNaN(inicio) || Number.isNaN(fin)) return null;
  return formatearDuracion((fin - inicio) / 1000);
}

function VistaCorrida({
  corrida,
  progreso,
}: {
  corrida: Corrida;
  progreso?: Record<string, number> | null;
}) {
  const hechas = new Set(corrida.fases.map((f) => f.fase));
  const total = progreso ? Object.values(progreso).reduce((a, b) => a + b, 0) : 0;
  const duracion = duracionCorrida(corrida);
  return (
    <div className="corrida">
      <div className="corrida-titulo">
        <b>{corrida.ruta}</b>
        {duracion && (
          <span className="corrida-tiempo">
            {corrida.estado === "EN_CURSO" ? `lleva ${duracion}` : `duró ${duracion}`}
          </span>
        )}
        {corrida.estado === "EN_CURSO" && <span className="chip medio">en curso</span>}
        {corrida.estado === "COMPLETADA" && <span className="chip ok">completada</span>}
        {corrida.estado === "FALLIDA" && <span className="chip bajo">fallida</span>}
        {corrida.seguro_para_desechar === true && (
          <span className="chip ok">✓ seguro para desechar</span>
        )}
        {corrida.seguro_para_desechar === false && (
          <span className="chip medio">aún no seguro</span>
        )}
      </div>
      {corrida.destino && (
        <div className="corrida-destino" title={corrida.destino}>
          destino: <code>{corrida.destino}</code>
        </div>
      )}
      {corrida.fases.map((f) => (
        <FilaFase key={f.fase} f={f} />
      ))}
      {corrida.estado === "EN_CURSO" &&
        (() => {
          // El doble filtro y los blobs+índice corren EN PARALELO: cuando la fase
          // actual es "worker", la precalificación sigue trabajando a la vez (y los
          // resultados ya son buscables). La UI lo refleja en vez de aparentar espera.
          const enParalelo = corrida.fase_actual === "worker";
          const activas = new Set(
            enParalelo ? ["precalificacion", "worker"] : [corrida.fase_actual ?? ""],
          );
          return (
            <>
              {ORDEN_FASES.filter((f) => !hechas.has(f)).map((f) => {
                const activa = activas.has(f);
                const paralela = enParalelo && (f === "precalificacion" || f === "worker");
                return (
                  <div key={f} className={activa ? "fase-fila activa" : "fase-fila"}>
                    <span className="fase-check">{activa ? "⟳" : "·"}</span>
                    <span className="fase-nombre">{NOMBRES_FASE[f]}</span>
                    {activa && <span className="fase-dur">trabajando…</span>}
                    {paralela && <span className="fase-paralelo">∥ paralelo</span>}
                  </div>
                );
              })}
              {enParalelo && (
                <div className="nota-paralelo">
                  filtro y blobs+índice avanzan a la vez — los resultados ya son buscables
                  mientras corre
                </div>
              )}
            </>
          );
        })()}
      {corrida.estado === "EN_CURSO" && progreso && total > 0 && (
        <div className="progreso-vivo">
          {Object.entries(progreso)
            .sort(([, a], [, b]) => b - a)
            .map(([estado, n]) => (
              <span key={estado}>
                {estado}: <b>{n.toLocaleString()}</b>
              </span>
            ))}
          <span className="progreso-total">de {total.toLocaleString()} en cola</span>
        </div>
      )}
      {corrida.error && <div className="corrida-error">{corrida.error}</div>}
    </div>
  );
}

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

// Motivos del inventario "preservados sin explorar", en cristiano
const MOTIVO_PRESERVADO: Record<string, string> = {
  contenedor_corrupto: "corrupto o con contraseña",
  formato_no_soportado: "formato aún no soportado (RAR sin herramienta, imagen de disco…)",
  contenedor_sin_explorar: "pendiente de exploración",
  profundidad_maxima: "anidación más honda que el tope",
};

function etiquetaMotivo(motivo: string): string {
  if (motivo.startsWith("zip_bomb_sospechoso:")) return "sospecha de bomba (guard)";
  return MOTIVO_PRESERVADO[motivo] ?? motivo;
}

function Preservados() {
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

const CLAVE_DESTINO = "norm_destino_elegido";
const CLAVE_WORKERS = "norm_workers_elegidos";

function ModalIndexar({
  destinoEligible,
  workersAuto,
  onLanzar,
  onCerrar,
}: {
  destinoEligible: boolean;
  workersAuto: number;
  onLanzar: (origen: string, destino: string | null, workers: number | null) => void;
  onCerrar: () => void;
}) {
  const [origen, setOrigen] = useState<string | null>(null);
  const [destino, setDestino] = useState<string | null>(
    () => localStorage.getItem(CLAVE_DESTINO) || null,
  );
  const [usarDestino, setUsarDestino] = useState<boolean>(() =>
    Boolean(localStorage.getItem(CLAVE_DESTINO)),
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
}: {
  onCompletada: () => void;
  onProgreso?: () => void;
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
        if (habiaEnCurso.current && !e.en_curso) onCompletada(); // terminó → refrescar búsqueda
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
          {desplegado ? "ocultar detalle ▴" : "fases e historial ▾"}
        </button>
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
          <div className="destinos">
            <h4>Destinos (configurables con variables NORM_* en .env)</h4>
            {Object.entries(estado.destinos).map(([k, v]) => (
              <div key={k} className="destino">
                <span>{k.replace(/_/g, " ")}</span>
                <code>{v}</code>
              </div>
            ))}
          </div>
          <Preservados />
          {estado.en_curso && (
            <VistaCorrida corrida={estado.en_curso} progreso={estado.progreso} />
          )}
          {estado.historial.length > 0 && (
            <>
              <h4>Historial de corridas</h4>
              {estado.historial.map((c) => (
                <VistaCorrida key={c.id} corrida={c} />
              ))}
            </>
          )}
        </div>
      )}

      {selectorAbierto && (
        <ModalIndexar
          destinoEligible={estado?.destino_eligible ?? false}
          workersAuto={estado?.workers_auto ?? 1}
          onLanzar={lanzar}
          onCerrar={() => setSelectorAbierto(false)}
        />
      )}
    </section>
  );
}
