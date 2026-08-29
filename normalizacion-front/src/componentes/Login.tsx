import { useEffect, useRef, useState } from "react";
import { useSesion } from "../contexto/Sesion";

/**
 * Puerta de entrada al panel.
 *
 * Es la primera cosa que se ve de Azazel, así que respeta la misma guía que el
 * resto: carbón, oro viejo solo en el título y los filetes, Cinzel en mayúsculas.
 * Nada brilla más que el título.
 *
 * Detalles que parecen menores y no lo son:
 *   * Es un `<form>` de verdad, con `autocomplete` — sin eso los gestores de
 *     contraseñas no ofrecen guardar ni rellenar, y la gente acaba eligiendo
 *     contraseñas que pueda teclear de memoria.
 *   * El error nunca distingue "no existe" de "contraseña mala": el backend ya
 *     devuelve un mensaje único, y aquí no se le añade detalle.
 *   * El bloqueo por intentos se muestra distinto del error de credenciales,
 *     porque son dos situaciones distintas y reintentar solo sirve en una.
 */
export default function Login() {
  const { entrar } = useSesion();
  const [usuario, setUsuario] = useState("");
  const [contrasena, setContrasena] = useState("");
  const [verClave, setVerClave] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [espera, setEspera] = useState(0);
  const [enviando, setEnviando] = useState(false);
  const refUsuario = useRef<HTMLInputElement>(null);

  useEffect(() => {
    refUsuario.current?.focus();
  }, []);

  // Cuenta atrás del bloqueo: un número que baja explica la espera mejor que un
  // botón inerte sin motivo aparente.
  useEffect(() => {
    if (espera <= 0) return;
    const t = setTimeout(() => setEspera((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [espera]);

  async function enviar(evento: React.FormEvent) {
    evento.preventDefault();
    if (enviando || espera > 0) return;
    setError(null);
    setEnviando(true);
    try {
      await entrar(usuario, contrasena);
    } catch (e) {
      const mensaje = String((e as Error).message ?? e);
      // El backend manda "429: … Reintenta en N s."; se extrae para la cuenta atrás.
      const bloqueo = mensaje.match(/Reintenta en (\d+)/);
      if (bloqueo) {
        setEspera(Number(bloqueo[1]));
        setError(null);
      } else {
        setError(
          mensaje.includes("401")
            ? "Usuario o contraseña incorrectos."
            : "No se pudo conectar con el servidor.",
        );
      }
      setContrasena("");
    } finally {
      setEnviando(false);
    }
  }

  const bloqueado = espera > 0;

  return (
    <div className="login-fondo">
      <main className="login-tarjeta">
        <header className="login-marca">
          <h1>Azazel</h1>
          <span className="login-filete" aria-hidden="true" />
          <p className="login-subtitulo">Normalización masiva de datos</p>
        </header>

        <form onSubmit={enviar} noValidate>
          <label className="login-etiqueta" htmlFor="usuario">
            Usuario
          </label>
          <input
            id="usuario"
            ref={refUsuario}
            className="login-campo"
            value={usuario}
            onChange={(e) => setUsuario(e.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            disabled={enviando || bloqueado}
            required
          />

          <label className="login-etiqueta" htmlFor="contrasena">
            Contraseña
          </label>
          <div className="login-campo-clave">
            <input
              id="contrasena"
              className="login-campo"
              type={verClave ? "text" : "password"}
              value={contrasena}
              onChange={(e) => setContrasena(e.target.value)}
              autoComplete="current-password"
              disabled={enviando || bloqueado}
              required
            />
            <button
              type="button"
              className="login-ojo"
              onClick={() => setVerClave((v) => !v)}
              // Sin esto un lector de pantalla solo anuncia "botón": hay que decir
              // qué hace Y en qué estado está.
              aria-label={verClave ? "Ocultar contraseña" : "Mostrar contraseña"}
              aria-pressed={verClave}
              tabIndex={-1}
            >
              {verClave ? "ocultar" : "ver"}
            </button>
          </div>

          {/* `role="alert"` para que el error se anuncie al aparecer, no solo se pinte. */}
          {error && (
            <p className="login-error" role="alert">
              {error}
            </p>
          )}
          {bloqueado && (
            <p className="login-espera" role="alert">
              Demasiados intentos fallidos. Reintenta en {espera} s.
            </p>
          )}

          <button
            type="submit"
            className="login-boton"
            disabled={enviando || bloqueado || !usuario || !contrasena}
          >
            {enviando ? "Entrando…" : "Entrar"}
          </button>
        </form>
      </main>
    </div>
  );
}
