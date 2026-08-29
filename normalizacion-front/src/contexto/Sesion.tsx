import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  alPerderSesion,
  cambiarContrasena,
  cerrarSesion,
  iniciarSesion,
  quienSoy,
} from "../api";
import type { Identidad } from "../tipos";

/**
 * Quién está usando el panel, para toda la aplicación.
 *
 * La sesión vive en una cookie `httpOnly` que este código NO puede leer — ese es el
 * punto: si no la puede leer React, tampoco la puede leer un script inyectado. Así
 * que la fuente de verdad es el servidor, y aquí solo se guarda la respuesta de
 * `GET /auth/yo`.
 */

type EstadoSesion = {
  identidad: Identidad | null;
  /** Primera comprobación en curso: ni logueado ni deslogueado todavía. */
  cargando: boolean;
  entrar: (usuario: string, contrasena: string) => Promise<void>;
  salir: () => Promise<void>;
  cambiar: (actual: string, nueva: string) => Promise<void>;
};

const Contexto = createContext<EstadoSesion | null>(null);

export function ProveedorSesion({ children }: { children: ReactNode }) {
  const [identidad, setIdentidad] = useState<Identidad | null>(null);
  const [cargando, setCargando] = useState(true);

  useEffect(() => {
    // Al cargar la página la cookie ya puede venir en el request; hay que
    // preguntarle al servidor si sigue siendo válida.
    quienSoy()
      .then(setIdentidad)
      .catch(() => setIdentidad(null))
      .finally(() => setCargando(false));
  }, []);

  // Cualquier 401 desde cualquier punto de la app cae aquí: la sesión pudo caducar
  // o ser revocada desde otro dispositivo mientras esta pestaña seguía abierta.
  // Sin esto el usuario se queda mirando errores sueltos sin entender por qué.
  useEffect(() => alPerderSesion(() => setIdentidad(null)), []);

  const entrar = useCallback(async (usuario: string, contrasena: string) => {
    setIdentidad(await iniciarSesion(usuario, contrasena));
  }, []);

  const salir = useCallback(async () => {
    try {
      await cerrarSesion();
    } finally {
      // Aunque el servidor falle, aquí se sale: dejar la sesión pintada cuando el
      // usuario pidió salir es la peor de las dos equivocaciones posibles.
      setIdentidad(null);
    }
  }, []);

  const cambiar = useCallback(async (actual: string, nueva: string) => {
    await cambiarContrasena(actual, nueva);
    setIdentidad(await quienSoy());
  }, []);

  return (
    <Contexto.Provider value={{ identidad, cargando, entrar, salir, cambiar }}>
      {children}
    </Contexto.Provider>
  );
}

export function useSesion(): EstadoSesion {
  const valor = useContext(Contexto);
  if (!valor) throw new Error("useSesion fuera de <ProveedorSesion>");
  return valor;
}

/** ¿El rol actual llega al mínimo pedido? Espeja `roles.alcanza` del backend. */
const NIVEL: Record<string, number> = { lector: 0, operador: 1, admin: 2 };

export function alcanza(rol: string | undefined, minimo: "lector" | "operador" | "admin"): boolean {
  if (!rol || !(rol in NIVEL)) return false;
  return NIVEL[rol] >= NIVEL[minimo];
}
