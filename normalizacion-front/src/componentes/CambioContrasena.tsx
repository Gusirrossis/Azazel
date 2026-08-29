import { useEffect, useRef, useState } from "react";
import { useSesion } from "../contexto/Sesion";

/** Espeja `contrasena.LONGITUD_MINIMA` del backend. Validar aquí también evita un
 *  viaje al servidor para decir algo que ya se sabe al teclear. */
const LONGITUD_MINIMA = 12;

/**
 * Cambio obligatorio de contraseña.
 *
 * Se muestra en lugar del panel cuando la identidad trae `debe_cambiar`: pasa tras
 * un alta o un reseteo, cuando la contraseña la eligió OTRA persona y por tanto no
 * es un secreto todavía. No hay forma de saltárselo salvo cerrar sesión.
 */
export default function CambioContrasena() {
  const { identidad, cambiar, salir } = useSesion();
  const [actual, setActual] = useState("");
  const [nueva, setNueva] = useState("");
  const [repetida, setRepetida] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(false);
  const refActual = useRef<HTMLInputElement>(null);

  useEffect(() => {
    refActual.current?.focus();
  }, []);

  const corta = nueva.length > 0 && nueva.length < LONGITUD_MINIMA;
  const noCoincide = repetida.length > 0 && nueva !== repetida;
  const puedeEnviar =
    !enviando && actual.length > 0 && nueva.length >= LONGITUD_MINIMA && nueva === repetida;

  async function enviar(evento: React.FormEvent) {
    evento.preventDefault();
    if (!puedeEnviar) return;
    setError(null);
    setEnviando(true);
    try {
      await cambiar(actual, nueva);
    } catch (e) {
      const mensaje = String((e as Error).message ?? e);
      setError(
        mensaje.includes("403")
          ? "La contraseña actual no es correcta."
          : mensaje.replace(/^API \d+:\s*/, "") || "No se pudo cambiar la contraseña.",
      );
      setActual("");
    } finally {
      setEnviando(false);
    }
  }

  return (
    <div className="login-fondo">
      <main className="login-tarjeta">
        <header className="login-marca">
          <h1>Azazel</h1>
          <span className="login-filete" aria-hidden="true" />
          <p className="login-subtitulo">
            Elige una contraseña nueva para <b>{identidad?.usuario}</b>
          </p>
        </header>

        <form onSubmit={enviar} noValidate>
          {/* Oculto pero presente: sin el campo de usuario, los gestores de
              contraseñas no saben a qué cuenta pertenece la que van a guardar. */}
          <input
            type="text"
            autoComplete="username"
            value={identidad?.usuario ?? ""}
            readOnly
            hidden
          />

          <label className="login-etiqueta" htmlFor="actual">
            Contraseña actual
          </label>
          <input
            id="actual"
            ref={refActual}
            className="login-campo"
            type="password"
            value={actual}
            onChange={(e) => setActual(e.target.value)}
            autoComplete="current-password"
            disabled={enviando}
            required
          />

          <label className="login-etiqueta" htmlFor="nueva">
            Contraseña nueva
          </label>
          <input
            id="nueva"
            className="login-campo"
            type="password"
            value={nueva}
            onChange={(e) => setNueva(e.target.value)}
            autoComplete="new-password"
            aria-describedby="pista-longitud"
            disabled={enviando}
            required
          />

          <label className="login-etiqueta" htmlFor="repetida">
            Repítela
          </label>
          <input
            id="repetida"
            className="login-campo"
            type="password"
            value={repetida}
            onChange={(e) => setRepetida(e.target.value)}
            autoComplete="new-password"
            disabled={enviando}
            required
          />

          <p id="pista-longitud" className="login-nota">
            {corta
              ? `Faltan ${LONGITUD_MINIMA - nueva.length} caracteres.`
              : noCoincide
                ? "Las dos contraseñas no coinciden."
                : `Mínimo ${LONGITUD_MINIMA} caracteres. Larga vale más que complicada.`}
          </p>

          {error && (
            <p className="login-error" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="login-boton" disabled={!puedeEnviar}>
            {enviando ? "Guardando…" : "Guardar y entrar"}
          </button>
        </form>

        <p className="login-nota">
          <button type="button" className="sesion-salir" onClick={salir}>
            Cerrar sesión
          </button>
        </p>
      </main>
    </div>
  );
}
