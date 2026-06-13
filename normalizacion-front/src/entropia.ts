// Conversión de escala de ENTROPÍA — SOLO presentación.
//
// El backend calcula y almacena la entropía de Shannon en bits/byte (rango 0–8)
// y TODA la lógica del filtro (umbrales, decisiones, señales guardadas) sigue en
// esa escala. El usuario, sin embargo, la prefiere normalizada 0–1.
//
// Estas funciones viven SOLO en el front: se aplican al MOSTRAR (bits → 0–1) y se
// revierten al GUARDAR un umbral editado (0–1 → bits) para que el backend nunca
// vea otra cosa que su escala 0–8. No tocar el backend por esto.

export const ENTROPIA_BITS_MAX = 8;

/** bits/byte (0–8) → fracción normalizada (0–1) para mostrar. */
export const normalizarEntropia = (bits: number): number => bits / ENTROPIA_BITS_MAX;

/** fracción normalizada (0–1) → bits/byte (0–8) para enviar al backend. */
export const desnormalizarEntropia = (norm: number): number => norm * ENTROPIA_BITS_MAX;

/** Texto listo para la UI: "0.47" a partir de los bits crudos. */
export const formatearEntropia = (bits: number, decimales = 2): string =>
  normalizarEntropia(bits).toFixed(decimales);
