import { useEffect, useState } from "react";
import { actualizarUsuario, crearUsuario, listarUsuarios } from "../api";
import { useSesion } from "../contexto/Sesion";
import type { UsuarioPanel } from "../tipos";

const ROLES = ["lector", "operador", "admin"] as const;

const QUE_PUEDE: Record<string, string> = {
  lector: "Buscar, ver tableros y entidades, descargar originales.",
  operador: "Lo del lector + lanzar corridas, reprocesar y editar el filtro.",
  admin: "Todo + usuarios, claves de API, recetas y recursos.",
};

/**
 * Alta y administración de las personas que entran al panel. Solo visible para
 * `admin` — el backend lo impone igualmente en cada endpoint.
 *
 * La contraseña inicial la elige quien da de alta, así que la cuenta nace con
 * `debe_cambiar`: hasta que el dueño la cambie no es un secreto de nadie.
 */
export default function Usuarios() {
  const { identidad } = useSesion();
  const [usuarios, setUsuarios] = useState<UsuarioPanel[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const [nuevo, setNuevo] = useState({ usuario: "", contrasena: "", rol: "lector", nombre: "" });
  const [creando, setCreando] = useState(false);

  const cargar = () =>
    listarUsuarios()
      .then(setUsuarios)
      .catch((e) => setError(limpiar(e)));

  useEffect(() => {
    cargar();
  }, []);

  async function crear() {
    setError(null);
    setAviso(null);
    setCreando(true);
    try {
      await crearUsuario(nuevo);
      setAviso(
        `Usuario "${nuevo.usuario}" creado. Tendrá que cambiar la contraseña al entrar.`,
      );
      setNuevo({ usuario: "", contrasena: "", rol: "lector", nombre: "" });
      cargar();
    } catch (e) {
      setError(limpiar(e));
    } finally {
      setCreando(false);
    }
  }

  async function cambiar(u: UsuarioPanel, cambios: Parameters<typeof actualizarUsuario>[1]) {
    setError(null);
    setAviso(null);
    try {
      await actualizarUsuario(u.id, cambios);
      cargar();
    } catch (e) {
      setError(limpiar(e));
    }
  }

  async function resetear(u: UsuarioPanel) {
    const nueva = prompt(
      `Contraseña nueva para "${u.usuario}" (mínimo 12 caracteres).\n` +
        "Se cerrarán todas sus sesiones y tendrá que cambiarla al entrar.",
    );
    if (!nueva) return;
    cambiar(u, { contrasena: nueva });
  }

  const soyYo = (u: UsuarioPanel) => u.usuario === identidad?.usuario;
  const adminsActivos = usuarios.filter((u) => u.rol === "admin" && u.activo).length;
  // Espeja la protección del backend, para deshabilitar el control en vez de
  // dejar que el usuario lo pulse y reciba un 409.
  const esUltimoAdmin = (u: UsuarioPanel) => u.rol === "admin" && u.activo && adminsActivos === 1;

  return (
    <div className="config-atributos">
      <h3>Usuarios del panel</h3>
      <p className="panel-nota">
        Cada persona entra con su <b>usuario y contraseña</b>. El rol decide qué puede
        hacer, y el servidor lo comprueba en cada petición — esconder un botón no es la
        protección, solo la cortesía. Un usuario no se borra: se <b>desactiva</b>, para
        conservar la traza de lo que hizo.
      </p>

      {error && <div className="banner-error">{error}</div>}
      {aviso && <div className="banner-aviso">{aviso}</div>}

      <table className="tabla-claves" style={{ width: "100%", maxWidth: 900, marginTop: 14 }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Usuario</th>
            <th style={{ textAlign: "left" }}>Rol</th>
            <th style={{ textAlign: "left" }}>Último acceso</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {usuarios.map((u) => (
            <tr key={u.id} style={{ opacity: u.activo ? 1 : 0.5 }}>
              <td>
                {u.usuario}
                {soyYo(u) && <span className="panel-nota"> · tú</span>}
                {u.nombre && <div className="panel-nota">{u.nombre}</div>}
                {!u.activo && <div className="panel-nota">desactivado</div>}
                {u.debe_cambiar && u.activo && (
                  <div className="panel-nota">pendiente de cambiar contraseña</div>
                )}
              </td>
              <td>
                <select
                  value={u.rol}
                  title={QUE_PUEDE[u.rol]}
                  disabled={esUltimoAdmin(u)}
                  onChange={(e) => cambiar(u, { rol: e.target.value })}
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </td>
              <td className="mono">
                {u.ultimo_acceso ? u.ultimo_acceso.replace("T", " ").slice(0, 16) : "nunca"}
              </td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <button className="boton" onClick={() => resetear(u)}>
                  contraseña
                </button>{" "}
                <button
                  className="boton"
                  disabled={esUltimoAdmin(u)}
                  title={
                    esUltimoAdmin(u)
                      ? "Es el único admin activo: asciende a otro antes de desactivarlo."
                      : undefined
                  }
                  onClick={() => cambiar(u, { activo: !u.activo })}
                >
                  {u.activo ? "desactivar" : "activar"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4 style={{ marginTop: 28 }}>Dar de alta</h4>
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          margin: "12px 0",
          flexWrap: "wrap",
          maxWidth: 900,
        }}
      >
        <input
          value={nuevo.usuario}
          placeholder="usuario"
          autoCapitalize="none"
          onChange={(e) => setNuevo({ ...nuevo, usuario: e.target.value })}
        />
        <input
          value={nuevo.nombre}
          placeholder="nombre visible (opcional)"
          onChange={(e) => setNuevo({ ...nuevo, nombre: e.target.value })}
        />
        <input
          type="password"
          value={nuevo.contrasena}
          placeholder="contraseña inicial (mín. 12)"
          autoComplete="new-password"
          onChange={(e) => setNuevo({ ...nuevo, contrasena: e.target.value })}
        />
        <select
          value={nuevo.rol}
          title={QUE_PUEDE[nuevo.rol]}
          onChange={(e) => setNuevo({ ...nuevo, rol: e.target.value })}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button
          className="boton-primario"
          disabled={creando || !nuevo.usuario || nuevo.contrasena.length < 12}
          onClick={crear}
        >
          {creando ? "Creando…" : "Crear usuario"}
        </button>
      </div>
      <p className="panel-nota">{QUE_PUEDE[nuevo.rol]}</p>
    </div>
  );
}

/** FastAPI ya manda el motivo en `detail`; `pedir` lo antepone con "API 4xx:". */
function limpiar(e: unknown): string {
  return String((e as Error).message ?? e).replace(/^API \d+:\s*/, "");
}
