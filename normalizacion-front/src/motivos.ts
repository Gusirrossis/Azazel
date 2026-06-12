// Diccionario de MOTIVOS del sistema en lenguaje humano — la columna vertebral
// de la UX: cada código que produce el backend se traduce a qué pasó, por qué,
// y qué hacer. Los códigos vienen de reglas.py / precalificador / workers.
//
// `tono` controla el color: ok (oro/verde: funcionó), frio (azul: a frío,
// reversible), gris (incertidumbre), alerta (ámbar: recuperable), critico
// (rojo: requiere atención humana).

export type Tono = "ok" | "frio" | "gris" | "alerta" | "critico";

export interface InfoMotivo {
  titulo: string; // corto, para chips y columnas
  explicacion: string; // la frase completa: qué pasó y por qué
  tono: Tono;
  accion?: string; // qué hacer al respecto (solo si hay algo que hacer)
}

// ---- decisiones del filtro (columna `motivo`) ----

const MOTIVOS: Record<string, InfoMotivo> = {
  // T0: kill-rules (descartado por metadatos, sin abrir el archivo)
  kill_t0: {
    titulo: "basura conocida (T0)",
    explicacion:
      "Descartado sin abrirlo: nombre, extensión o ruta de basura conocida (thumbs.db, caches, temporales).",
    tono: "frio",
  },
  // Lista blanca / negra
  fuera_de_lista_blanca: {
    titulo: "fuera de la lista blanca",
    explicacion:
      "Su TIPO REAL no está en la lista de tipos de interés — va a frío reversible.",
    tono: "frio",
    accion: "Si el tipo sí interesa: añádelo en la pestaña Filtro y usa «Re-puntuar frío».",
  },
  tipo_no_objetivo: {
    titulo: "tipo no objetivo",
    explicacion: "Su tipo real está en la lista negra (multimedia, ejecutables…).",
    tono: "frio",
  },
  // Señales dominantes del puntaje (HOT o COLD según el número)
  tabular: {
    titulo: "datos tabulares",
    explicacion: "Domina la señal tabular: columnas consistentes (CSV/hoja) — alto valor.",
    tono: "ok",
  },
  estructurado: {
    titulo: "datos estructurados",
    explicacion: "Domina la señal de estructura: JSON/XML/SQL válidos.",
    tono: "ok",
  },
  documento: {
    titulo: "documento",
    explicacion: "Domina la señal de documento (PDF/Office) con texto.",
    tono: "ok",
  },
  texto_legible: {
    titulo: "texto legible",
    explicacion: "Texto plano legible (entropía baja, mayoría de caracteres imprimibles).",
    tono: "ok",
  },
  tamano_contextual: {
    titulo: "tamaño razonable",
    explicacion: "El tamaño es coherente con un archivo de datos útil.",
    tono: "ok",
  },
  extension_coincide: {
    titulo: "extensión honesta",
    explicacion: "La extensión coincide con el tipo real detectado.",
    tono: "ok",
  },
  comprimido_cifrado: {
    titulo: "comprimido o cifrado",
    explicacion:
      "Entropía muy alta (>umbral): bytes que parecen comprimidos o cifrados, sin estructura legible.",
    tono: "frio",
  },
  ruido_binario: {
    titulo: "ruido binario",
    explicacion: "Bytes sin estructura reconocible ni texto: sin valor aparente.",
    tono: "frio",
  },
  minusculo_para_dato: {
    titulo: "demasiado pequeño",
    explicacion: "Tan chico que difícilmente contiene información útil.",
    tono: "frio",
  },
  // Franja gris (sin T4: va a HOT por recall)
  gris_sin_t4: {
    titulo: "franja gris",
    explicacion:
      "Puntaje entre los umbrales frío/HOT: el filtro NO está seguro. Sin el T4 (ML) va a HOT por recall.",
    tono: "gris",
    accion: "Esta zona es la que el etiquetado del T4 va a resolver — candidata a revisión.",
  },
  // Contenedores (T3)
  contenedor_pendiente_t3: {
    titulo: "contenedor en cola",
    explicacion: "Detectado como contenedor; su exploración T3 está pendiente.",
    tono: "gris",
  },
  contenedor_explorado: {
    titulo: "contenedor explorado",
    explicacion:
      "Contenedor abierto por completo: sus piezas internas se re-encolaron como archivos propios.",
    tono: "ok",
  },
  contenedor_corrupto: {
    titulo: "corrupto o con contraseña",
    explicacion:
      "No se pudo abrir (dañado o cifrado con contraseña). Se PRESERVA íntegro — nada se pierde.",
    tono: "alerta",
    accion: "Si conoces la contraseña o una herramienta mejor: está intacto en el almacén.",
  },
  contenedor_sin_explorar: {
    titulo: "preservado sin explorar",
    explicacion: "Contenedor guardado íntegro, pendiente de exploración.",
    tono: "gris",
  },
  formato_no_soportado: {
    titulo: "formato aún no soportado",
    explicacion:
      "Formato sin herramienta de exploración (RAR sin unar, imagen de disco ISO/VHD…). Preservado íntegro.",
    tono: "alerta",
  },
  profundidad_maxima: {
    titulo: "anidación al tope",
    explicacion: "Cajas dentro de cajas más hondo que el guard K4 — preservado sin seguir bajando.",
    tono: "alerta",
    accion: "Subir t3_profundidad_max + re-puntuar frío lo explora.",
  },
  zip_bomb_sospechoso: {
    titulo: "sospecha de zip-bomb",
    explicacion:
      "Violó un guard de seguridad (ratio de compresión, tamaño descomprimido o nº de entradas). A frío reversible.",
    tono: "alerta",
    accion: "Si es legítimo: subir el guard K4 en .env + «Re-puntuar frío» lo explora completo.",
  },
  sin_decidir: {
    titulo: "aún sin decidir",
    explicacion: "Catalogado pero el doble filtro aún no lo evalúa.",
    tono: "gris",
  },
};

// ---- errores (columna `error_motivo`) ----
// Cinco familias, cada una con su acción. El prefijo (antes de ':') clasifica.

const ERRORES: Record<string, InfoMotivo> = {
  agotado: {
    titulo: "reintentos agotados",
    explicacion:
      "Fallo TRANSITORIO (almacén/índice caído, I/O intermitente) que agotó sus reintentos con backoff.",
    tono: "alerta",
    accion: "Recuperable: restaura la dependencia (¿MinIO/OpenSearch arriba?) y Reprocesar.",
  },
  io_ilegible: {
    titulo: "disco origen ilegible",
    explicacion: "El disco de ORIGEN dio error de I/O al leer este archivo (sector dañado o desconexión).",
    tono: "alerta",
    accion: "Verifica el montaje/cable del disco origen y Reprocesar. Si reincide: sector dañado real.",
  },
  precalificacion_fallida: {
    titulo: "archivo envenenado (filtro)",
    explicacion:
      "Bytes hostiles reventaron el análisis (zip malformado, encoding imposible…). La corrida siguió sin él.",
    tono: "critico",
    accion: "Revisión manual: probablemente reincida si solo se reprocesa.",
  },
  worker_fallido: {
    titulo: "archivo envenenado (worker)",
    explicacion:
      "El procesamiento (extracción/copia) reventó con este archivo. La corrida siguió sin él.",
    tono: "critico",
    accion: "Revisión manual: probablemente reincida si solo se reprocesa.",
  },
  contenedor: {
    titulo: "contenedor inseguro",
    explicacion: "El contenedor violó una regla de seguridad de forma permanente.",
    tono: "critico",
    accion: "Revisión manual antes de cualquier reproceso.",
  },
  indexado_rechazado: {
    titulo: "rechazado por el índice",
    explicacion: "OpenSearch rechazó ESTE documento (suele ser un campo que no cabe en el mapping).",
    tono: "critico",
    accion: "Corregir el mapping del índice y Reprocesar.",
  },
  verificacion_fallida: {
    titulo: "CORRUPCIÓN detectada",
    explicacion:
      "El blob guardado NO coincide con su hash esperado: corrupción silenciosa en destino.",
    tono: "critico",
    accion: "NO reprocesar a ciegas: revisa el disco DESTINO y vuelve a copiar del origen ANTES de desecharlo.",
  },
  verificacion_sin_hash: {
    titulo: "sin hash que verificar",
    explicacion: "La fila llegó a verificación sin hash registrado (inconsistencia de estado).",
    tono: "critico",
    accion: "Reprocesar para que vuelva a pasar por el worker.",
  },
  verificacion_io: {
    titulo: "almacén ilegible al verificar",
    explicacion: "No se pudo leer el blob del almacén para verificarlo (transitorio).",
    tono: "alerta",
    accion: "Restaurar el almacén y Reprocesar.",
  },
  io_frio: {
    titulo: "fallo copiando a frío",
    explicacion: "El movimiento al almacén frío falló (espacio/permisos/desconexión).",
    tono: "alerta",
    accion: "Revisar el destino frío y Reprocesar — bloquea la puerta del disco.",
  },
  almacen: {
    titulo: "almacén no disponible",
    explicacion: "El almacén permanente (MinIO/carpeta) no respondió al guardar.",
    tono: "alerta",
    accion: "Restaurar el almacén y Reprocesar.",
  },
  io_fuente: {
    titulo: "I/O del origen",
    explicacion: "Error de lectura del disco origen durante el procesamiento.",
    tono: "alerta",
    accion: "Verificar montaje del origen y Reprocesar.",
  },
  indice: {
    titulo: "índice no disponible",
    explicacion: "OpenSearch no respondió al indexar (transitorio).",
    tono: "alerta",
    accion: "Restaurar OpenSearch y Reprocesar.",
  },
  prueba: {
    titulo: "error de prueba",
    explicacion: "Fila sembrada manualmente para demostración.",
    tono: "gris",
  },
};

const DESCONOCIDO: InfoMotivo = {
  titulo: "motivo no catalogado",
  explicacion: "Código que la UI aún no conoce — el valor crudo está en el detalle.",
  tono: "gris",
};

function prefijo(codigo: string): string {
  return codigo.split(":", 1)[0].trim();
}

export function describirMotivo(motivo: string | null | undefined): InfoMotivo {
  if (!motivo) return MOTIVOS.sin_decidir;
  return MOTIVOS[prefijo(motivo)] ?? { ...DESCONOCIDO, titulo: prefijo(motivo) };
}

export function describirError(error: string | null | undefined): InfoMotivo | null {
  if (!error) return null;
  return ERRORES[prefijo(error)] ?? { ...DESCONOCIDO, titulo: prefijo(error), tono: "critico" };
}

// Para las tarjetas-resumen: una clave de `por_causa` puede ser motivo O error
export function describirCausa(clave: string, contextoError: boolean): InfoMotivo {
  if (contextoError) return describirError(clave) ?? DESCONOCIDO;
  return ERRORES[clave] ?? describirMotivo(clave);
}

// ¿La clave de una causa pertenece a la familia de errores? (decide qué filtro aplicar)
export function esCausaDeError(clave: string): boolean {
  return clave in ERRORES;
}

export function etiquetaTipo(mime: string): string {
  if (mime === "sin_tipificar") return "sin tipificar";
  const CORTOS: Record<string, string> = {
    // documentos
    "application/pdf": "PDF",
    "application/rtf": "RTF",
    "application/x-ole-storage": "Office legado (doc/xls/ppt/msg)",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word (docx)",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel (xlsx)",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint (pptx)",
    "application/vnd.oasis.opendocument.text": "OpenDocument texto (odt)",
    "application/vnd.oasis.opendocument.spreadsheet": "OpenDocument hoja (ods)",
    "application/vnd.oasis.opendocument.presentation": "OpenDocument presentación (odp)",
    "application/epub+zip": "libro EPUB",
    "application/vnd.wordperfect": "WordPerfect (wpd)",
    "image/vnd.djvu": "escaneo DjVu",
    "application/vnd.ms-xpsdocument": "XPS",
    "application/x-iwork": "iWork (Pages/Numbers/Keynote)",
    // correos
    "message/rfc822": "correo (eml/mbox)",
    "application/vnd.ms-outlook-pst": "buzón Outlook (pst/ost)",
    "application/x-dbx": "Outlook Express (dbx)",
    "application/x-nsf": "Lotus Notes (nsf)",
    // datos
    "application/json": "JSON",
    "application/x-ndjson": "NDJSON (json por línea)",
    "application/xml": "XML",
    "application/sql": "dump SQL",
    "application/vnd.sqlite3": "base SQLite (db)",
    "application/x-msaccess": "Access (mdb/accdb)",
    "application/vnd.apache.parquet": "Parquet",
    "application/x-dbf": "dBase/FoxPro (dbf)",
    "application/x-pgdump": "respaldo pg_dump",
    "application/x-mssql-backup": "respaldo SQL Server (bak)",
    "application/avro": "Avro",
    "application/orc": "ORC",
    // texto / web
    "text/plain": "texto plano",
    "text/csv": "CSV",
    "text/html": "HTML",
    // contenedores y binarios
    "application/zip": "ZIP",
    "application/x-7z-compressed": "7z",
    "application/vnd.rar": "RAR",
    "application/x-tar": "tar",
    "application/gzip": "gzip",
    "application/octet-stream": "binario",
    "application/x-dosexec": "ejecutable Windows",
    "application/x-executable": "ejecutable Linux",
    "text/": "familia texto (txt, csv, logs…)",
  };
  return CORTOS[mime] ?? mime.replace(/^application\//, "").replace(/^text\//, "");
}
