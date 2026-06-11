import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// El proxy evita CORS en dev: el front llama /api/* y Vite lo reenvía a la API
// (`norm api`, puerto 8000). En prod, el reverse proxy hace lo mismo.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (ruta) => ruta.replace(/^\/api/, ""),
      },
    },
  },
});
