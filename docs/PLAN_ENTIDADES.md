# PLAN_ENTIDADES.md — Proyección a entidades para alimentar otros sistemas

**Estado:** EN MARCHA — diseño 2026-06-10, primera rebanada construida 2026-06-16.
**Decisiones tomadas con el usuario:** contrato de salida = **API que el consumidor consulta (pull)** + **proyección por receta de salida** (cada sistema, su estructura) + **exportación de archivo completo**; resolución de identidad = **la hace Azazel**; primera fuente = **tabular** (padrones, DBF, SQL) y **el propio índice ya existente**; mapeo de columnas = **propone y yo confirmo** (semiautomático); tipos de entidad **dinámicos**; criterio de "doc = persona" en el backfill = **trae CURP o RFC válida**.

---

## Estado de implementación (2026-06-16)

Lo que sigue (§1–§9) es el diseño aprobado. Esto es lo CONSTRUIDO contra ese diseño;
el detalle de lo nuevo está en §10–§12.

| Capacidad | Estado | Dónde |
|---|---|---|
| Recetas + normalizadores núcleo (CURP, RFC, email, teléfono, nombre) | ✅ construido | `entidades/normalizadores.py`, `receta.py` |
| Mapeo tabular semiautomático (propone columna→campo) | ✅ construido | `entidades/mapeo.py`; `POST /entidades/mapeo/proponer` |
| Almacén de entidades + API pull | ✅ construido | tabla `entidades` (mig. 0006); `GET /entidades`, `/entidades/{id}`, `/entidades/estadisticas` |
| Resolución por anclas fuertes (dedup exacto, idempotente) | ✅ construido | `entidades/pipeline.py` (`_upsert`, `_fusionar_campos`) |
| **Proyección de salida por receta editable** (cada sistema, su estructura) | ✅ construido | `entidades/proyeccion.py`, `recetas_db.py` (tabla `recetas`, mig. 0007); ver **§10** |
| **Export del archivo completo** (p. ej. el Fz1 entero) | ✅ construido | receta de colección `fz1_bundle`; `GET /entidades/exportar`; ver **§10** |
| Contingencia (soft-delete reversible, LFPDPPP) | ✅ construido | `POST /entidades/{id}/activo` (flag `activo`) |
| **Backfill desde el índice ya existente** (CURP/RFC del texto indexado) | ✅ construido | `entidades/backfill.py`; `norm backfill-entidades`; `POST /entidades/backfill`; ver **§12** |
| UI: pestaña Entidades (Personas + Recetas, ver-como-receta, export, backfill) | ✅ construido | `normalizacion-front/.../Entidades.tsx` |
| Resolución difusa (Splink, sin ancla exacta) | ⏳ pendiente | §4 fase E4 |
| NER completo sobre documentos | ⏳ pendiente | §7 fase E5 (el backfill §12 es el primer puente: solo anclas por regex) |
| Grafo de relaciones (`vincular_con`) | ⏳ pendiente | E5 |
| Control de acceso por campo + bitácora de consultas (PII / LFPDPPP) | ⏳ pendiente (bloqueante para producción) | §9 decisión #3 |
| Modo continuo (resolver al indexar, no solo en lote) | ⏳ pendiente | §12 "limitaciones" |

---

## 1. Qué problema resuelve

Azazel hoy es un **lago de contenido buscable**: captura todo, extrae texto de documentos, perfila columnas de lo tabular y guarda la **procedencia** de cada cosa. Pero un sistema externo (de nombres, de correos, de empresas, de negocios…) no quiere "archivos buscables": quiere **registros estructurados** de un tipo concreto, por un **contrato estable**.

Este plan agrega una capa: **proyectar** el lago hacia **entidades canónicas** de cualquier tipo y exponerlas por API. No reemplaza nada — se monta encima de lo que ya existe.

**Principio rector — DINÁMICO POR RECETA, no por código:** un "tipo de entidad" es **configuración versionada**, no programación. Agregar `Empresa` o `Negocio` mañana = escribir una receta nueva, sin tocar el sistema ni redesplegar.

---

## 2. Lo que ya existe y se reutiliza (no se reinventa)

| Pieza existente | Cómo la aprovecha esta capa |
|---|---|
| `campos_extraidos` + `perfil_calidad` (tabular) | Las columnas ya vienen perfiladas → base del mapeo automático |
| `texto_indexable` (documentos) | Insumo del NER en la fase posterior de documentos |
| `procedencias` (de qué archivo salió cada cosa) | Se hereda tal cual: cada dato de cada entidad sabe su origen |
| Cola durable + máquina de estados (`SKIP LOCKED`, leases) | La proyección es **un worker más**, reanudable y a prueba de fallas |
| `version_filtro` (versionado + reproceso) | Mismo patrón: `version_receta`, `version_resolucion` → reproyectar sin re-leer discos |
| Almacén content-addressed (blobs por sha256) | Re-extraer con mejores reglas sin volver a tocar el disco origen |
| OpenSearch | Un índice nuevo `entidades` para consultas rápidas por la API |

---

## 3. El modelo de RECETA (el núcleo dinámico)

Una receta declara un tipo de entidad como datos. Ejemplo conceptual:

```yaml
tipo: empresa
version: 1
campos:
  - nombre: razon_social   tipo: texto      normalizador: nombre_propio   requerido: true
  - nombre: rfc            tipo: rfc        normalizador: rfc             ancla: true
  - nombre: correo         tipo: correo     normalizador: correo
  - nombre: telefono       tipo: telefono   normalizador: telefono_mx
  - nombre: domicilio      tipo: texto      normalizador: direccion
anclas_fuertes: [rfc]          # identifican sin ambigüedad
resolucion:
  exacta_por: [rfc, correo]    # mismo valor = misma entidad (fase 1)
  difusa_por: [razon_social]   # matching aproximado (fase 2, opcional)
```

- **`campos`**: el esquema canónico. Cambiarlo = nueva `version` de la receta.
- **`normalizador`**: función reutilizable por tipo de dato (ver §5), no por entidad.
- **`ancla`/`anclas_fuertes`**: IDs que identifican sin ambigüedad (RFC, CURP, correo). Son la base de la resolución confiable.
- Una entidad nueva (`persona`, `negocio`, `correo`…) = un archivo de receta nuevo. **Cero código.**

---

## 4. El proceso, fase por fase

```
   [lo que ya hay]                      [capa nueva: proyección a entidades]
ARCHIVO INDEXADO ──▶ (A) MAPEO ──▶ (B) NORMALIZACIÓN ──▶ (C) RESOLUCIÓN ──▶ (D) ALMACÉN ──▶ (E) API
 (campos/texto)      fuente→receta     campo a canónico    juntar la misma     entidades       pull
                                                            entidad             canónicas
```

Cada fase es idempotente, versionada y reanudable (igual que el pipeline actual).

### (A) Mapeo de la fuente a la receta — SEMIAUTOMÁTICO ("propone y confirmo")

Es el punto de arranque porque lo tabular ya está perfilado. El flujo elegido:

1. **El sistema PROPONE** el mapeo columna→campo de cada dataset, combinando:
   - **Nombre de columna** (`e_mail`, `correo_e`, `email` → `correo`) por diccionario de sinónimos.
   - **Contenido muestreado** (la columna valida como RFC/CURP/correo/teléfono en >X% de filas → ese tipo) usando los mismos validadores de §5.
   - La **confianza** de cada sugerencia.
2. **Tú confirmas o ajustas UNA vez por "forma" de dataset.** El padrón A y el padrón B con las mismas columnas comparten el mapeo aprobado.
3. El mapeo aprobado se guarda como **receta de mapeo** (huella de columnas → asignación), reutilizable automáticamente en datasets con la misma forma.

> Documentos (PDF/escaneos) usan otra estrategia de extracción (NER + patrones) en una fase posterior; **caen en el mismo almacén y la misma API**.

### (B) Normalización de campos

Cada valor pasa por el normalizador de su tipo (§5): correo a minúsculas y validado, RFC/CURP con dígito verificador, teléfono a formato único, nombres con acentos plegados para comparar. Lo que no valida se conserva como **valor crudo + bandera** (jamás se descarta — mismo principio que el frío reversible).

### (C) Resolución de identidad — POR FASES (la hace Azazel)

Juntar la misma entidad de muchos archivos en un `entidad_id` estable.

- **Fase 1 — solo anclas fuertes (exacto):** mismo RFC = misma empresa. Sin riesgo, alto valor, se entrega rápido.
- **Fase 2 — difuso (opcional, después):** nombre + contexto con umbral de similitud y banda de revisión humana (mismo patrón de active learning que el T4). Se activa cuando quieras más alcance.

Cada fusión es **auditable y reversible**: se guarda por qué dos registros son la misma entidad y se puede deshacer.

### (D) Almacén de entidades

Tabla nueva `entidades` (Postgres) + índice `entidades` (OpenSearch para la API):

| Campo | Qué es |
|---|---|
| `entidad_id` | ID estable de la entidad resuelta |
| `tipo` | `persona` / `empresa` / … (de la receta) |
| `campos` | los valores canónicos normalizados |
| `procedencias` | TODOS los archivos/blobs que respaldan cada dato (heredado) |
| `confianza` | por campo y global |
| `version_receta`, `version_resolucion` | para auditar y reprocesar |

Reprocesable: mejoras la receta → reproyectas → mejores entidades, **sin re-leer los discos** (sale de lo ya indexado).

### (E) API de entidades — el CONTRATO (pull)

Endpoints **genéricos sobre el tipo** (el mismo contrato sirve para cualquier entidad):

- `GET /entidades?tipo=empresa&rfc=...&desde=...` — consulta con filtros + paginación profunda (mismo `search_after`+PIT de hoy).
- `GET /entidades/{entidad_id}` — la entidad con TODAS sus procedencias y confianzas.
- `GET /entidades/tipos` — qué tipos hay disponibles (las recetas activas).
- Mismas garantías que la API actual: `extra="forbid"`, API key, rate-limit, el consumidor **nunca** toca OpenSearch directo.

Tu sistema externo hace *pull* de lo que necesita, cuando lo necesita.

---

## 5. Normalizadores y validadores (reutilizables, no por entidad)

Catálogo inicial (crece con el tiempo); cada uno valida + normaliza + da confianza:

- `correo` — minúsculas, sintaxis RFC 5322, dominio plausible.
- `rfc` — formato MX + dígito verificador (persona física/moral).
- `curp` — 18 caracteres + dígito verificador + fecha/entidad válidas.
- `telefono_mx` — 10 dígitos, lada válida, formato único.
- `nombre_propio` — capitalización, plegado de acentos para comparar (conserva el original).
- `direccion` — partido en componentes cuando se pueda (CP, estado).
- `fecha`, `monto`, `texto` — genéricos.

Los mismos validadores alimentan el **mapeo automático** de la fase A (una columna "valida como RFC" → se sugiere mapear a un campo `rfc`).

---

## 6. Cómo encaja en lo que ya hay

- **Nada se rompe ni se mueve.** El pipeline actual (catálogo → filtro → worker → frío → verificación → puerta) sigue igual. La proyección es una etapa nueva que consume su salida.
- **Misma robustez:** worker con `SKIP LOCKED` + leases + dead-letter por entidad envenenada (igual que el blindaje recién hecho). Una fila mala no tumba la proyección.
- **Mismo versionado:** `version_receta`/`version_resolucion` como `version_filtro` → auditar y reprocesar.
- **Mismo front (opcional):** una vista para aprobar mapeos y revisar la banda difusa, reusando el modal/tabla actuales.

---

## 7. Fases de implementación sugeridas

| Fase | Alcance | Valor | Estado |
|---|---|---|---|
| **E1 — Recetas + mapeo tabular semiautomático** | Modelo de receta, proponer/confirmar columna→campo, normalizadores núcleo | Arranca con padrones/DBF/SQL reales | ✅ construido |
| **E2 — Almacén de entidades + API pull** | Tabla `entidades`, endpoints genéricos | Otro sistema ya puede consumir | ✅ construido |
| **E3 — Resolución por anclas fuertes** | Dedup exacto por CURP/RFC/correo | Una entidad = un registro confiable | ✅ construido |
| **E4 — Resolución difusa** | Matching aproximado + banda de revisión (Splink) | Más alcance, con control humano | ⏳ pendiente |
| **E5 — Entidades desde documentos** | NER + patrones sobre `texto_indexable` | Desbloquea los millones de PDFs | ⏳ pendiente (puente parcial: backfill por anclas, §12) |

E1+E2+E3 es la rebanada de alto valor (tabular de punta a punta) — **construida**, con dos
piezas extra que el diseño no anticipaba y resultaron clave: la **proyección de salida por
receta** (§10) y el **backfill desde el índice** (§12). E4/E5 vienen después.

---

## 8. Riesgos y cómo se mitigan

- **Mapeo equivocado de columnas** → por eso es "propone y confirmo", no automático ciego; y todo es reproyectable.
- **Falsos positivos de PII** (un número que parece RFC y no lo es) → validación con dígito verificador, no solo formato; confianza por campo.
- **Sobre-fusión de identidades** (juntar dos entidades distintas) → fase difusa con umbral conservador + banda de revisión + fusiones reversibles y auditables.
- **Privacidad / PII sensible** → la API ya tiene auth y rate-limit; pendiente decidir control de acceso por tipo/campo y bitácora de consultas (a definir contigo).

---

## 9. Decisiones pendientes (para cuando aprobemos el doc)

1. ¿La aprobación de mapeos vive en el **front** (vista nueva) o en **CLI/archivos de receta** al principio?
2. Catálogo inicial de **tipos de entidad** que de verdad te sirven (persona, empresa, correo, negocio…) — para priorizar normalizadores.
3. **Control de acceso por entidad/campo** en la API (¿todo el que tiene API key ve todo, o hay niveles?). — **bloqueante para producción**; hoy la API expone CURP/RFC sin filtro de rol ni bitácora.
4. ¿La API de entidades vive en el **mismo servicio** que la búsqueda o en uno aparte? — **resuelto:** mismo servicio (los endpoints `/entidades/*` viven en la misma API).

---

## 10. Proyección de salida: una persona, muchas estructuras (CONSTRUIDO)

El diseño separó dos cosas que el §3 mezclaba, y resultó la pieza más útil:

- **Resolución** (§11) → produce la **persona canónica ESTABLE**: siempre la misma forma interna.
- **Proyección** → transforma esa persona a **la estructura que pide cada sistema consumidor**.
  La estructura de salida es **un DATO editable (una "receta"), no código**: añadir un sistema =
  otra receta, sin tocar ni redesplegar.

### Dos clases de receta de proyección

Las recetas viven en la tabla `recetas` (migración 0007), editables desde la UI o la API. La
definición es JSON:

1. **Por-ítem** (una persona → un objeto):
   - `{ "passthrough": true }` — la canónica tal cual.
   - `{ "salida": [ { "path": …, "de" | "constante": …, "mapa"?: …, "default"?: … }, … ] }`
     - `path`: ruta de salida con puntos (anida: `contact.email`).
     - `de`: ruta de origen en la canónica; **`constante`**: valor fijo (excluyente con `de`).
     - `mapa`: traduce valores (`{"H":"male","M":"female"}`); `default`: relleno si viene vacío.

2. **Colección** (N personas → el ARCHIVO completo): `{ "sobre": {…}, "coleccion": "personas", "item": {…} }`
   - `sobre`: lo constante del nivel superior (p. ej. `_metadata`, `_mapeo_normalizacion_sistema`).
   - `coleccion`: la clave del arreglo; `item`: la receta por-ítem aplicada a cada persona.
   - Reproduce el **archivo Fz1 entero** (la semilla `fz1_bundle`).

### Semillas, endpoints y UI

- Arranca con **dos** recetas: `fz1_bundle` (colección = el archivo Fz1) y `sistema_plano`
  (ejemplo por-ítem que muestra renombrar, mapear, constante y anidar). Las demás se clonan.
- `GET /entidades/recetas` · `PUT|DELETE /entidades/recetas/{clave}` — CRUD.
- `GET /entidades/{id}/proyectar?receta=X` — la misma persona bajo una receta por-ítem
  (rechaza recetas de colección con **400**; ésas se exportan, no se proyectan de a una).
- `GET /entidades/exportar?receta=X` — arma el **archivo completo** (p. ej. `fz1_bundle`).
- UI → pestaña **Entidades**: *Recetas* (gestionar con editor JSON) y *Personas*
  ("ver como receta" en el detalle + botón **Descargar JSON** del archivo).
- `validar_definicion` rechaza rutas mal formadas, `mapa` sin `de`, **colisión de prefijo**
  (una ruta es prefijo de otra) y **datos pegados como receta** (un JSON con `personas`/`_metadata`).

> **Honesto:** los campos del Fz1 que E1–E3 aún no resuelve (`es_objetivo`, `redes`, `notas`,
> `vincular_con`) **no** se inventan; salen del NER/grafo (E5) y se agregan a la receta con una
> línea cada uno cuando existan. La receta `fz1_bundle` solo emite lo que de verdad se resuelve.

---

## 11. Resolución: invariantes y política de fusión (CONSTRUIDO)

- **Ancla** (orden de preferencia): **CURP › RFC › EMAIL › TELÉFONO**. El identificador es
  `entidad_id = sha256(tipo:ancla_tipo:ancla_valor.upper())`. **Misma ancla = misma entidad**
  (idempotente: re-ejecutar no duplica, fusiona).
- **Normalizadores con validación fuerte** (no solo formato):
  - **CURP** (18): dígito verificador + fecha/estado válidos; **deriva** sexo, fecha y estado de
    nacimiento; admite la **Ñ** (apellidos PEÑA/MUÑOZ).
  - **RFC** físico (13): formato + fecha + **dígito verificador SAT** (sin él, ~1 de cada 28
    cadenas con forma de RFC pasaría por azar — crítico para anclar desde texto).
  - email, teléfono MX (a 10 dígitos).
- **Fusión** (`_fusionar_campos`): **rellena huecos sin pisar lo ya puesto** (gana el primer dato
  no vacío), recursiva en subobjetos. **No sobrescribe** un valor existente con uno distinto: hoy
  ese conflicto se **ignora** → la política de conflicto explícita (confianza/recencia/procedencia
  **por campo**) es trabajo de E4. Las **procedencias se acumulan deduplicadas** por `archivo_id`
  (re-correr no infla las "fuentes").
- **Sin carreras:** `INSERT … ON CONFLICT DO NOTHING` + `SELECT … FOR UPDATE` antes de fusionar;
  dead-letter por fila envenenada (una fila mala no tumba la corrida).

---

## 12. Backfill desde el índice ya existente (CONSTRUIDO — primer puente a E5)

Los datos que **ya** están indexados (en la Mac) no habían pasado por entidades. El backfill
recorre **todo el índice de OpenSearch**, detecta personas y las resuelve con el motor de §11.

- **Criterio "doc = persona":** trae una **CURP o RFC válida** en su texto (`texto_indexable` +
  `campos_extraidos` aplanado). Regex liberal para encontrar candidatos + **validador estricto**
  (dígito verificador) para descartar basura.
- **Anclaje seguro en docs multi-persona:** una fila por CURP; un RFC enriquece a una CURP **solo
  si comparten los 10 primeros chars** (mismo nombre+fecha = misma persona); todo RFC sin asociar
  ancla su propia persona (no se pierde).
- **Honesto:** solo fija el **ancla** y lo derivado de la CURP. **No** saca nombre/email/teléfono
  del texto libre — asociarlos a la persona correcta es NER (E5/E8).
- **Idempotente y reanudable:** cursor por `archivo_id` en la tabla `control`; **savepoint por
  fila** (una fila envenenada no aborta el lote); **advisory lock** (un backfill a la vez).
- **Limitación (una PASADA):** el orden por `archivo_id` (hash) completa el barrido de lo ya
  indexado; para capturar docs **nuevos** se re-corre con `--reiniciar` (rescan completo,
  idempotente). El **modo continuo** (resolver al momento de indexar) es el enganche al pipeline,
  pendiente. `search_after` sin PIT queda anotado como deuda de robustez.
- **Cómo se dispara:**
  - CLI (la corrida grande, en la Mac): `uv run norm backfill-entidades [--lote N] [--max-docs N] [--reiniciar]`
  - API acotada: `POST /entidades/backfill?lote=&max_docs=&reiniciar=` (devuelve el resumen).
  - UI: pestaña Entidades → Personas → botón **"⟳ Procesar ya indexados (CURP/RFC)"**.
