// Pestaña Filtro: la lista blanca y los umbrales VISIBLES y editables.
// Lo editado se guarda como overrides en el servidor y aplica a la SIGUIENTE
// corrida (sin reiniciar). El frío ya decidido se re-evalúa con "Re-puntuar frío".

import { useCallback, useEffect, useState } from "react";
import { guardarFiltro, obtenerFiltro, rescoreFrio, restablecerFiltro } from "../api";
import { etiquetaTipo } from "../motivos";
import { desnormalizarEntropia, formatearEntropia, normalizarEntropia } from "../entropia";
import type { FiltroVisible, RespuestaFiltro, SolicitudFiltro } from "../tipos";

// Lista editable en FILAS (no chips): los tipos MIME largos
// (application/vnd.openxmlformats-…) se leen completos — nombre humano a la
// izquierda, código entero abajo en mono, quitar a la derecha.
function ListaEditable({
  valores,
  placeholder,
  conNombreHumano = false,
  onCambiar,
}: {
  valores: string[];
  placeholder: string;
  conNombreHumano?: boolean;
  onCambiar: (nuevos: string[]) => void;
}) {
  const [nuevo, setNuevo] = useState("");
  const agregar = () => {
    const v = nuevo.trim().toLowerCase();
    if (v && !valores.includes(v)) onCambiar([...valores, v].sort());
    setNuevo("");
  };
  return (
    <div className="chips-editables">
      <div className="lista-tipos">
        {valores.map((v) => (
          <div key={v} className="fila-tipo">
            <span className="fila-tipo-texto">
              {conNombreHumano && <span className="fila-tipo-humano">{etiquetaTipo(v)}</span>}
              <code className="fila-tipo-codigo">{v}</code>
            </span>
            <button
              className="chip-quitar"
              title={`quitar ${v}`}
              onClick={() => onCambiar(valores.filter((x) => x !== v))}
            >
              ×
            </button>
          </div>
        ))}
        {valores.length === 0 && <span className="sin-sub">ninguno</span>}
      </div>
      <div className="chips-agregar">
        <input
          value={nuevo}
          placeholder={placeholder}
          onChange={(e) => setNuevo(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && agregar()}
        />
        <button className="secundario" disabled={!nuevo.trim()} onClick={agregar}>
          ➕ Añadir
        </button>
      </div>
    </div>
  );
}

function CampoNumero({
  etiqueta,
  valor,
  paso = 1,
  onCambiar,
}: {
  etiqueta: string;
  valor: number;
  paso?: number;
  onCambiar: (n: number) => void;
}) {
  return (
    <label className="campo-numero">
      <span>{etiqueta}</span>
      <input
        type="number"
        step={paso}
        value={valor}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (!Number.isNaN(n)) onCambiar(n);
        }}
      />
    </label>
  );
}

// Campo de umbral de ENTROPÍA: el usuario lo ve y edita en 0–1, pero hacia el
// estado/backend SIEMPRE viaja en bits 0–8. La conversión vive solo aquí —
// `valorBits` entra crudo y `onCambiarBits` devuelve crudo, así el guardado del
// filtro no cambia y la config nunca se corrompe (ver entropia.ts).
function CampoEntropia({
  etiqueta,
  valorBits,
  onCambiarBits,
}: {
  etiqueta: string;
  valorBits: number;
  onCambiarBits: (bits: number) => void;
}) {
  return (
    <label className="campo-numero">
      <span>{etiqueta}</span>
      <input
        type="number"
        step={0.01}
        min={0}
        max={1}
        value={Number(normalizarEntropia(valorBits).toFixed(2))}
        onChange={(e) => {
          const norm = Number(e.target.value);
          if (!Number.isNaN(norm)) onCambiarBits(desnormalizarEntropia(norm));
        }}
      />
    </label>
  );
}

export default function Filtro() {
  const [base, setBase] = useState<RespuestaFiltro | null>(null);
  const [edicion, setEdicion] = useState<FiltroVisible | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const [extNueva, setExtNueva] = useState("");

  const aplicar = useCallback((r: RespuestaFiltro) => {
    setBase(r);
    setEdicion(JSON.parse(JSON.stringify(r.efectivo)) as FiltroVisible);
  }, []);

  useEffect(() => {
    obtenerFiltro().then(aplicar).catch((e) => setError(String(e)));
  }, [aplicar]);

  if (!edicion || !base) {
    return (
      <section className="filtro-pagina">
        {error ? <div className="banner-error">{error}</div> : <div className="sin-sub">cargando…</div>}
      </section>
    );
  }

  const editar = (cambios: Partial<FiltroVisible>) =>
    setEdicion({ ...edicion, ...cambios });

  const guardar = () => {
    // Solo viaja lo que de verdad cambió respecto al filtro efectivo cargado
    const cambios: SolicitudFiltro = {};
    for (const clave of Object.keys(edicion) as (keyof FiltroVisible)[]) {
      const antes = JSON.stringify(base.efectivo[clave]);
      const ahora = JSON.stringify(edicion[clave]);
      if (antes !== ahora) (cambios as Record<string, unknown>)[clave] = edicion[clave];
    }
    // La versión la deriva el servidor (+ov-<huella>) salvo que se edite a mano
    if (Object.keys(cambios).length === 0) {
      setAviso("no hay cambios que guardar");
      return;
    }
    setError(null);
    guardarFiltro(cambios)
      .then((r) => {
        aplicar(r);
        setAviso(
          `guardado — versión ${r.efectivo.version_filtro}. Aplica a la SIGUIENTE corrida; ` +
            "para re-evaluar lo ya enviado a frío usa «Re-puntuar frío».",
        );
      })
      .catch((e) => setError(String(e)));
  };

  const restablecer = () => {
    if (!window.confirm("¿Borrar todos los cambios y volver a la config base (.env)?")) return;
    restablecerFiltro()
      .then((r) => {
        aplicar(r);
        setAviso("filtro restablecido a la config base");
      })
      .catch((e) => setError(String(e)));
  };

  const rescore = () => {
    if (
      !window.confirm(
        "¿Re-puntuar TODO el frío con el filtro vigente? (COLD → PENDIENTE; se procesa en la siguiente corrida)",
      )
    )
      return;
    rescoreFrio()
      .then((r) =>
        setAviso(
          r.re_encolados === 0
            ? "no había nada en frío"
            : `${r.re_encolados.toLocaleString()} archivos del frío re-encolados — re-indexa la carpeta para puntuarlos de nuevo`,
        ),
      )
      .catch((e) => setError(String(e)));
  };

  const agregarExtension = () => {
    const ext = extNueva.trim().toLowerCase();
    if (!ext) return;
    const clave = ext.startsWith(".") ? ext : `.${ext}`;
    editar({ prioridad_extensiones: { ...edicion.prioridad_extensiones, [clave]: 100 } });
    setExtNueva("");
  };

  return (
    <section className="filtro-pagina">
      {error && <div className="banner-error">{error}</div>}
      {aviso && (
        <div className="banner-aviso">
          {aviso} <button className="enlace" onClick={() => setAviso(null)}>×</button>
        </div>
      )}

      <div className="filtro-barra">
        <span className="chip medio">
          versión vigente: <b>{base.efectivo.version_filtro}</b>
          {base.hay_overrides ? " (con cambios desde la UI)" : " (config base)"}
        </span>
        <span className="sin-sub">
          lo que guardes aplica a la SIGUIENTE corrida — nada de lo ya decidido se pierde
        </span>
      </div>

      <div className="panel-cuerpo filtro-tarjetas">
        <article className="panel-tarjeta">
          <h3>Lista {edicion.modo_lista}</h3>
          <label className="opcion-destino">
            <input
              type="radio"
              checked={edicion.modo_lista === "blanca"}
              onChange={() => editar({ modo_lista: "blanca" })}
            />
            Blanca: SOLO los tipos de interés avanzan; el resto va a frío reversible
          </label>
          <label className="opcion-destino">
            <input
              type="radio"
              checked={edicion.modo_lista === "negra"}
              onChange={() => editar({ modo_lista: "negra" })}
            />
            Negra (legado): bloquear solo los tipos no objetivo
          </label>
          <h4>Prefijos de interés (familias completas)</h4>
          <p className="panel-nota">
            Todo tipo que EMPIECE así entra (text/ cubre txt, csv, logs…), salvo los
            excluidos de abajo.
          </p>
          <ListaEditable
            valores={edicion.tipos_interes_prefijos}
            placeholder="prefijo MIME (text/)…"
            onCambiar={(v) => editar({ tipos_interes_prefijos: v })}
          />
          <h4>Excluidos (excepciones a los prefijos)</h4>
          <ListaEditable
            valores={edicion.tipos_excluidos}
            placeholder="tipo MIME (text/html)…"
            conNombreHumano
            onCambiar={(v) => editar({ tipos_excluidos: v })}
          />
        </article>

        <article className="panel-tarjeta">
          <h3>Tipos de interés ({edicion.tipos_interes.length})</h3>
          <p className="panel-nota">
            Decisión sobre el TIPO REAL, jamás la extensión. Quitar un tipo manda lo
            nuevo de ese tipo a frío (reversible); añadirlo + re-puntuar frío rescata
            lo ya excluido.
          </p>
          <ListaEditable
            valores={edicion.tipos_interes}
            placeholder="tipo MIME (application/pdf)…"
            conNombreHumano
            onCambiar={(v) => editar({ tipos_interes: v })}
          />
        </article>

        <article className="panel-tarjeta">
          <h3>Umbrales</h3>
          <h4>Entropía (Shannon, 0–1) — la señal de T2</h4>
          <p className="panel-nota">
            &lt; {formatearEntropia(edicion.entropia_texto_max)} = texto plano · &gt;{" "}
            {formatearEntropia(edicion.entropia_comprimido_min)} = comprimido/cifrado. Lo
            intermedio se decide con las demás señales. (Escala 0–1.)
          </p>
          <CampoEntropia
            etiqueta="Texto si entropía <"
            valorBits={edicion.entropia_texto_max}
            onCambiarBits={(bits) => editar({ entropia_texto_max: bits })}
          />
          <CampoEntropia
            etiqueta="Comprimido si entropía >"
            valorBits={edicion.entropia_comprimido_min}
            onCambiarBits={(bits) => editar({ entropia_comprimido_min: bits })}
          />
          <CampoNumero
            etiqueta="Mínimo de imprimibles (0–1)"
            valor={edicion.ratio_imprimibles_min}
            paso={0.05}
            onCambiar={(n) => editar({ ratio_imprimibles_min: n })}
          />
          <h4>Router HOT / frío (puntaje 1–100)</h4>
          <p className="panel-nota">
            ≥ {edicion.umbral_hot} HOT · &lt; {edicion.umbral_cold} frío · en medio:
            franja gris (hoy va a HOT, calibrado a recall).
          </p>
          <CampoNumero
            etiqueta="HOT si puntaje ≥"
            valor={edicion.umbral_hot}
            onCambiar={(n) => editar({ umbral_hot: n })}
          />
          <CampoNumero
            etiqueta="Frío si puntaje <"
            valor={edicion.umbral_cold}
            onCambiar={(n) => editar({ umbral_cold: n })}
          />
        </article>

        <article className="panel-tarjeta">
          <h3>Orden de procesamiento por extensión</h3>
          <p className="panel-nota">
            Solo ordena la cola (mayor = primero), JAMÁS decide el tipo ni la ruta.
            Contenedores genéricos: {edicion.prioridad_contenedores}; el resto usa su
            puntaje (0–100).
          </p>
          {Object.entries(edicion.prioridad_extensiones)
            .sort(([, a], [, b]) => b - a)
            .map(([ext, prio]) => (
              <div key={ext} className="fila-prioridad">
                <code>{ext}</code>
                <input
                  type="number"
                  value={prio}
                  onChange={(e) => {
                    const n = Number(e.target.value);
                    if (!Number.isNaN(n))
                      editar({
                        prioridad_extensiones: { ...edicion.prioridad_extensiones, [ext]: n },
                      });
                  }}
                />
                <button
                  className="chip-quitar"
                  title="quitar"
                  onClick={() => {
                    const { [ext]: _, ...resto } = edicion.prioridad_extensiones;
                    editar({ prioridad_extensiones: resto });
                  }}
                >
                  ×
                </button>
              </div>
            ))}
          <div className="chips-agregar">
            <input
              value={extNueva}
              placeholder="extensión nueva (.pdf)…"
              onChange={(e) => setExtNueva(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && agregarExtension()}
            />
            <button className="secundario" disabled={!extNueva.trim()} onClick={agregarExtension}>
              ➕ Añadir
            </button>
          </div>
        </article>
      </div>

      <div className="filtro-acciones">
        <button className="primario" onClick={guardar}>
          Guardar cambios
        </button>
        <button className="secundario" onClick={restablecer} disabled={!base.hay_overrides}>
          Restablecer a config base
        </button>
        <button className="secundario" onClick={rescore}>
          ↻ Re-puntuar frío con este filtro
        </button>
      </div>
    </section>
  );
}
