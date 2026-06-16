# PLAN_FASE2_ENTIDADES.md — Proyección del lago a entidades canónicas (esquema Fz1)

**Estado:** diseño · 2026-06-15
**Construye sobre:** [`PLAN_ENTIDADES.md`](PLAN_ENTIDADES.md) (lo aterriza con rigor de ingeniería),
[`ARQUITECTURA.md`](ARQUITECTURA.md) (Fase 1, lo ya construido).
**Fundamentado en:** análisis de 7 proyectos de referencia clonados en
`../fase2-referencias/` (Splink, dedupe, Zingg, REBEL, explosion/projects,
Awesome-Entity-Resolution, data-matching-software).

---

## 1. Problema y objetivo

Azazel (Fase 1) ya cataloga, filtra, deduplica por contenido e indexa el lago:
de cada archivo tenemos en OpenSearch metadatos, `texto_indexable`,
`campos_extraidos` (columnas tabulares perfiladas), `perfil_calidad`, `senales` y
**procedencias** (de qué archivo salió cada dato). Es un **lago buscable**.

Lo que falta es **estructurar** ese lago en **entidades canónicas de persona**,
deduplicadas y vinculadas en un grafo, con la forma que pide un sistema
consumidor. La primera receta concreta es **Fz1** (fichas de persona con ancla
CURP, campos normalizados y relaciones).

**Principio rector — DINÁMICO POR RECETA, no por código.** Fz1 es la *primera*
receta de salida; mañana otro sistema consumidor será *otra receta*
(configuración versionada, cero código nuevo). El motor de resolución y
proyección es genérico; el esquema de salida y los anclas son datos.

**Invariantes (heredados de Azazel, no negociables):** cada etapa es
**idempotente, versionada, reanudable, auditable y reversible**; toda perilla
vive en config tipada (sin números mágicos); una entidad envenenada va a
dead-letter sin tumbar la corrida; cada dato conserva su **procedencia**.

---

## 2. Decisión de herramientas (build vs buy) — y la regla de licencias

La regla es la misma que vetó `patool` (GPLv3): **una dependencia de un sistema
propietario debe tener licencia permisiva (MIT/Apache/BSD).** Copyleft fuerte
(GPL/AGPL) o "no comercial" → **solo referencia de algoritmos, jamás dependencia.**

| Proyecto | Licencia | ¿Dependencia? | Rol | Qué tomamos |
|---|---|---|---|---|
| **Splink** (Min. Justicia UK) | **MIT** | ✅ Sí | **Motor de scoring** | Record linkage probabilístico Fellegi-Sunter, blocking rules, comparadores, EM training, clustering por componentes conectados. Corre sobre **DuckDB / Postgres** (no exige Spark). |
| **dedupe** | **MIT** | ✅ Sí | **Banda de revisión** | Patrón de *active learning* (etiquetado interactivo de pares ambiguos) para la fusión difusa con humano en el bucle. |
| **explosion/projects** (spaCy) | **MIT** | ✅ Sí | **NER + RE** | Pipelines entrenables de NER y extracción de relaciones para sacar entidades del texto no estructurado (PDFs, correos). |
| **Zingg** | **AGPL-3.0** | ❌ **NO** | Referencia | ER a escala con Spark + LSH blocking + label propagation. Ideas, no código. Además exige Spark (no encaja con nuestra cola+workers). |
| **REBEL** (Babelscape) | **CC BY-NC-SA 4.0** | ❌ **NO** (no comercial) | Referencia | Extracción de relaciones end-to-end seq2seq. Modelo/dataset NO comercial → solo inspiración del enfoque. |
| Awesome-ER / data-matching-software | CC-BY-SA / s.l. | — | Mapa del campo | Catálogo de técnicas y **benchmarks** (FEBRL, fastLink, Census BigMatch) para calibrar. |

**Decisiones de motor:**

1. **Scoring → Splink** (MIT) con backend **DuckDB embebido** (un solo proceso,
   sin servidor, maneja ~100M de registros en una máquina — encaja con el piloto
   M4 y con escalar después). Elegido sobre Zingg por **licencia + transparencia
   auditable** (Fellegi-Sunter da un *match weight* en bits, explicable a un
   auditor) + por no exigir Spark. Si algún día hay >1.000M de registros, Splink
   también corre sobre Spark sin reescribir el modelo.
2. **Banda de revisión humana → patrón de dedupe** (MIT): la fusión difusa
   pregunta al operador sobre los pares dudosos y aprende. Reusamos el front
   actual para la UI de aprobación (no metemos la dependencia entera si basta el
   patrón).
3. **NER/RE → spaCy** (MIT, vía explosion/projects), **NO REBEL** (no comercial).
   Para los casos difíciles, la **Claude API** como extractor opcional de alto
   valor (decisión de costo, por lote).
4. **Anclas (CURP/RFC) → construir** normalizadores propios (validación con
   dígito verificador) — son lógica de negocio mexicana, no hay librería que
   reemplace tests contra el catálogo INEGI.

---

## 3. Arquitectura: el pipeline de proyección a entidades

Una **etapa NUEVA** que consume la salida de Fase 1. Reutiliza el chasis de
Azazel — no se reinventa nada:

| Pieza de Azazel | Reúso en Fase 2 |
|---|---|
| Cola durable Postgres (`FOR UPDATE SKIP LOCKED` + leases + huérfanos) | La proyección es **un worker más**, reanudable; nueva máquina de estados. |
| `version_filtro` (versionado/reproceso) | `version_receta` + `version_resolucion`: reproyectar sin re-leer discos. |
| Almacén content-addressed (sha256) | Una entidad canónica (JSON por `entidad_id`) inmutable y deduplicada. |
| OpenSearch (índice `archivos`) | Índice **nuevo `entidades`**, mismo bulk+retry+backoff+dead-letter. |
| Procedencias por dato | Cada dato de cada entidad hereda **de qué archivo/celda salió**. |
| Config tipada (`PerillasFiltro`…) | `PerillasReceta` + `PerillasResolucion` (umbrales m/u, confianza mínima…). |
| Registro de extractores (plugins) | Mismo mecanismo para normalizadores (curp, rfc, telefono_mx, nombre, direccion). |

**Máquina de estados de la proyección** (espejo de la de Fase 1):
`PENDIENTE_PROYECCION → MAPEADO → NORMALIZADO → RESUELTO → GRAFO → INDEXADO_ENT
→ HECHO`; `ERROR` reprocesable; fusiones `REVERSIBLE`.

### Las fases (A–I), cada una idempotente · versionada · reanudable · auditable

```
ARCHIVO INDEXADO (Fase 1)
   │  campos_extraidos / texto_indexable / procedencias
   ▼
(A) MAPEO  columna→campo de la receta  (semiautomático: "propone y confirmo")
   ▼
(B) NORMALIZACIÓN + VALIDACIÓN  (CURP✓, RFC✓, teléfono, nombre, dirección)
   ▼
(C) BLOCKING  (anclas fuertes exactas + bloques difusos)  ← reduce O(n²)
   ▼
(D) COMPARACIÓN de campos  (Jaro-Winkler / Levenshtein / exacto / derivado-CURP)
   ▼
(E) SCORING Fellegi-Sunter  (m&u, EM, match_weight en bits, umbral + banda gris)
   ▼
(F) CLUSTERING → entidad_id  (componentes conectados; fusiones reversibles)
   ▼
(G) GRAFO de relaciones  (vincular_con explícito + inferidas)
   ▼
(H) PROYECCIÓN al esquema Fz1  (incl. campos normalizados derivados de CURP)
   ▼
(I) API PULL  (genérica por tipo de entidad; control de acceso + bitácora)
```

**(A) Mapeo columna→campo (semiautomático).** El sistema **propone** el mapeo
combinando (i) diccionario de sinónimos del nombre de columna (`e_mail`,
`correo_e` → `email`) y (ii) validación del **contenido** (una columna que valida
como CURP en >X% de filas → `curp`). El operador **confirma una vez por "forma"
de dataset**; el mapeo aprobado se guarda como *receta de mapeo* reutilizable
(tabla `mapeos_aprobados`). Sin esto, el resto es basura entra-basura sale.

**(B) Normalización + validación.** Cada valor pasa por el normalizador de su
tipo (§4). Lo que no valida se conserva como **valor crudo + bandera** (jamás se
descarta — mismo principio que el frío reversible). Salida: campos canónicos +
confianza por campo + procedencia.

**(C) Blocking** (de Splink). Reduce las comparaciones de O(n²) a O(n·k):
- **Bloques por ancla fuerte exacta:** mismo CURP / mismo RFC / mismo teléfono /
  mismo email → candidatos. (Casi todo el valor está aquí: el CURP resuelve sin
  ambigüedad.)
- **Bloques difusos:** mismo `apellido1` + misma fecha de nacimiento; mismo CP +
  primeras letras del nombre. Para registros sin CURP o con CURP corrupto.
- *Salting* de Splink para no crear "súper-bloques" sesgados (todos los "JUAN").

**(D) Comparación.** Por campo, niveles de acuerdo: exacto > Jaro-Winkler≥0.92 >
JW≥0.88 > … > distinto. Nombres y direcciones **plegando acentos** (NFKD +
minúsculas) antes de comparar. Igualdad **derivada de CURP** (mismas posiciones
5–10 = misma fecha) como nivel determinista.

**(E) Scoring Fellegi-Sunter** (Splink). Cada nivel de cada campo tiene una
probabilidad **m** (acuerdan | son la misma) y **u** (acuerdan | NO son la
misma). El *match weight* combinado es `log2(BF_prior · ∏ BF_campo)` en **bits**
— explicable y auditable. Umbral: `match` si peso ≥ K bits; **banda gris** entre
dos umbrales → revisión humana (§5). m/u se estiman por **EM** (no supervisado) y
se ajustan por **frecuencia de término** (un apellido "García" pesa menos que uno
raro). *Trampa documentada de Splink:* los comparadores deben ser
**condicionalmente independientes** (no meter CP y municipio por separado sin
combinarlos) o el modelo oscila.

**(F) Clustering → `entidad_id`.** Grafo de pares con peso ≥ umbral →
**componentes conectados** (algoritmo SQL iterativo de Splink). `entidad_id =
hash(ancla más fuerte)` (CURP → RFC → email) para **idempotencia**: reprocesar no
duplica. Cada **fusión** queda en `auditoria_fusiones` con su porqué y su
`version_resolucion`, y se puede **deshacer** (marca, nunca borra).

**(G) Grafo de relaciones.** Aristas en `entidades_vinculos`:
1. **`vincular_con` explícito / determinista:** comparten CURP/RFC/email/teléfono
   → arista de alta confianza.
2. **Inferidas por contacto compartido:** mismo teléfono/email normalizado.
3. **Inferidas por domicilio compartido:** misma dirección canónica.
4. **Inferidas por co-ocurrencia:** dos personas aparecen juntas en N documentos
   (NER + ventana de texto) — confianza menor.
5. **Parentesco (futuro, NER):** patrones "padre de", "hermano/a de".
   Cada arista guarda **procedencia + confianza + `version_resolucion`**.

**(H) Proyección al esquema Fz1.** La receta mapea los campos canónicos a la
forma de salida, **derivando** los `normalized_*` (de la CURP, §4) y armando el
JSON por entidad. Reproyectar a otra receta = otro mapeo, sin re-resolver.

**(I) API pull genérica.** `GET /entidades?tipo=persona&curp=…`,
`GET /entidades/{id}` (con todas sus procedencias y confianzas),
`GET /entidades/tipos`. Mismas garantías que la API actual (`extra="forbid"`, API
key, rate-limit) **más control de acceso por campo y bitácora** (§6).

### Modelo de datos nuevo (Postgres + OpenSearch)

- `entidades` (entidad_id PK, tipo, campos JSONB, confianza, version_receta,
  version_resolucion, activo, procedencias JSONB, estado).
- `entidades_vinculos` (origen_id, destino_id, tipo_arista, confianza,
  procedencia, version_resolucion).
- `mapeos_aprobados` (huella_columnas PK, asignacion JSONB, version).
- `auditoria_fusiones` (fusion_id, entidad_id, miembros[], motivo,
  version_resolucion, deshecho_en).
- `entidades_consultas` (consulta_id, usuario, rol, entidad_id, campos_accedidos,
  ts, ip, resultado) — **obligatoria por LFPDPPP** (§6).
- `roles_permisos` (rol, tipo_entidad, campos_permitidos).
- Índice OpenSearch **`entidades`** (`_id = entidad_id`): nombre, alias, CURP,
  RFC, dirección, contacto, redes, figura, vinculado_con[], confianza,
  procedencias — campos críticos con subcampo hash para búsqueda exacta.

---

## 4. La CURP como ancla determinista (+ RFC) y el papel del NER

La **CURP** (18 caracteres) es el ancla de oro: **deriva datos sin ambigüedad**.

| Posiciones | Deriva | Uso |
|---|---|---|
| 5–10 (`AAMMDD`) | **Fecha de nacimiento** | `normalized_dob`, `edad` (hoy − dob) |
| 11 (`H`/`M`) | **Sexo** | `normalized_sex` |
| 12–13 (01–32 INEGI) | **Estado de nacimiento** | `normalized_estado` |
| 18 | **Dígito verificador** (mod 97 sobre los 17 previos) | **VALIDAR** la CURP |

Esto vuelve `edad/dob/sexo/estado` **gratis y confiables** cuando hay CURP — y un
nivel de comparación determinista en (D). El **RFC** (13 chars, persona física)
deriva fecha de nacimiento (pos 7–12) y se valida con su dígito (mod 11).

> **Construir bien estos validadores es media batalla.** Van como funciones puras
> con tests contra el catálogo INEGI de entidades y casos límite (homoclaves,
> palabras altisonantes filtradas, fechas inválidas). Una CURP que no valida →
> valor crudo + bandera, nunca se descarta.

**NER sobre texto no estructurado.** Los millones de PDFs/correos guardan
personas que no están en columnas. **spaCy** (MIT, `es_core_news` + reglas +
componente de RE entrenable de explosion/projects) extrae `nombre/email/teléfono/
CURP/RFC` del `texto_indexable` y los decora como candidatos de entidad. Esto
desbloquea (E5) el grafo de co-ocurrencia y parentesco. **REBEL queda fuera** por
su licencia no comercial; su enfoque seq2seq es referencia.

---

## 5. Escalabilidad y a-prueba-de-errores

- **Escala:** el **blocking** convierte O(n²) (inviable a miles de millones) en
  O(n·k). Con CURP como ancla, la inmensa mayoría resuelve por igualdad exacta
  (un índice, no una comparación difusa). DuckDB embebido lleva ~100M en una
  máquina; **no usamos Spark/Zingg** (licencia AGPL + no encaja con cola+workers
  Postgres). Si el volumen lo exige, Splink corre sobre Spark **sin reescribir el
  modelo** — la migración es de infraestructura, no de lógica.
- **Reanudable / sin pérdida:** misma cola durable; un worker muerto suelta su
  lease y otro retoma; reproyectar es incremental por `version_receta`.
- **Dead-letter por entidad envenenada:** un registro hostil (encoding raro,
  CURP imposible) va a `ERROR` con su motivo; la corrida sigue.
- **Fusiones reversibles y auditables:** toda fusión se puede deshacer marcando,
  sin perder historial (`auditoria_fusiones`). Esto evita el peor error de ER:
  **sobre-fusionar** dos personas distintas sin vuelta atrás.
- **Calibración (precision/recall):** medir contra datasets de benchmark del
  campo (FEBRL/Febrl4, los de Census BigMatch, los de Splink) y, cuando exista,
  contra un *ground truth* propio etiquetado. La banda gris alimenta el active
  learning (dedupe): el humano resuelve los dudosos y el modelo mejora.
- **Banda de revisión humana:** los pares en la franja gris de (E) no se fusionan
  solos — van a una cola de revisión (UI en el front), con fusión reversible.

---

## 6. Gobernanza y PII — requisito, no adorno

> **Nota de sensibilidad.** Fz1 maneja datos personales mexicanos (CURP, RFC,
> domicilio, contacto, redes) en un contexto de inteligencia/investigación. El
> tratamiento debe tener **base legal y autorización** documentadas, y el acceso
> debe ser **restringido y auditado**. Esto no es opcional para un sistema
> "profesional y a prueba de errores": es parte del diseño desde E1.

Bajo **LFPDPPP** (Ley Federal de Protección de Datos Personales en Posesión de
Particulares), hay que construir:

1. **Control de acceso por campo:** tabla `roles_permisos` + middleware
   `@auth_entidad(campos=[…])`. Por defecto los campos **críticos** (CURP, RFC,
   foto, domicilio completo, teléfono) se **ocultan**; solo roles autorizados y
   endpoints especiales los exponen.
2. **Bitácora obligatoria:** `entidades_consultas` registra **cada** acceso a PII
   (quién, qué campos, cuándo, IP), con retención larga (auditable por el
   regulador).
3. **Cifrado:** campos críticos cifrados en reposo (pgcrypto/KMS) y, en
   OpenSearch, **hash para búsqueda exacta** + cifrado para visualización; TLS en
   tránsito.
4. **Minimización y propósito:** recolectar CURP solo si es **estrictamente
   necesario** para el caso de uso declarado. (Decisión pendiente contigo:
   ¿propósito lícito = investigación autorizada / cumplimiento / anti-fraude?)
5. **Supresión y portabilidad:** *soft-delete* (`activo=false`, nunca borrar) +
   endpoint de solicitud de supresión; export de una entidad como JSON.
6. **`photo_url` por CURP:** el ejemplo sugiere resolver una foto por
   `${CURP}.jpg`. **Decisión de gobernanza, no se implementa por defecto:**
   traer fotos por CURP (p. ej. de RENAPO) requiere base legal explícita; sin
   ella, el campo queda oculto/no poblado.

---

## 7. Fases de implementación (E1 → E7)

| Fase | Alcance | Definition of Done |
|---|---|---|
| **E1 — Cimientos** | Modelo `Entidad` + tablas (migración) + máquina de estados de proyección + normalizadores núcleo (CURP✓, RFC✓, teléfono, nombre con plegado, dirección) con tests. **Decidir propósito lícito + categorización PII.** | Normalizadores con cobertura de casos INEGI; entidad se persiste y reproyecta sin duplicar. |
| **E2 — Mapeo tabular** | Loader de recetas + mapeo columna→campo "propone y confirmo" + `mapeos_aprobados`. | Un padrón/DBF real produce campos canónicos con procedencia. |
| **E3 — Resolución por ancla** | Blocking exacto + `entidad_id = hash(CURP)` + idempotencia. | Mismo CURP en dos fuentes = una entidad; reprocesar no duplica. |
| **E4 — Scoring + clustering difuso** | Integrar **Splink** (DuckDB) para m/u + EM + componentes conectados + banda gris + fusiones reversibles. | Precision/recall medidos en benchmark; fusiones auditables y reversibles. |
| **E5 — Grafo de relaciones** | `entidades_vinculos`: `vincular_con` explícito + inferidas (contacto/domicilio/co-ocurrencia). | "¿Quién está vinculado a X?" responde con procedencia por arista. |
| **E6 — Proyección Fz1 + API** | Proyección al esquema Fz1 (incl. `normalized_*` derivados) + índice OpenSearch `entidades` + API pull. | Un sistema externo consume Fz1 por pull. |
| **E7 — Gobernanza** | `roles_permisos` + `@auth_entidad` + `entidades_consultas` + cifrado de campos críticos + supresión/portabilidad. | Acceso a PII restringido por rol y 100% auditado. |
| **E8 — NER (texto)** | spaCy NER+RE sobre `texto_indexable` → entidades desde PDFs/correos. | Personas no tabulares entran al grafo. |

**Rebanada de alto valor:** E1+E2+E3 (tabular de punta a punta con ancla CURP) ya
produce entidades útiles y deduplicadas. E4/E5 añaden el matching difuso y el
grafo; E6 la salida; E7 la gobernanza (que para producción es bloqueante); E8 los
documentos.

---

## 8. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| **Sobre-fusión** (juntar dos personas distintas) | Umbral conservador + banda gris a revisión humana + **fusiones reversibles y auditadas**. |
| Mapeo de columnas equivocado | "Propone y confirmo" (no automático ciego) + todo reproyectable. |
| Falsos positivos de PII (un número que parece CURP) | Validación por **dígito verificador**, no solo formato; confianza por campo. |
| Dependencia con licencia tóxica | Regla explícita: solo MIT/Apache/BSD como dependencia; **Zingg (AGPL) y REBEL (no comercial) solo referencia**. |
| Independencia condicional de comparadores (Splink) | No meter campos correlacionados por separado (CP+municipio) — combinarlos. |
| Fuga de PII (breach de OpenSearch/Postgres) | Cifrado de críticos + hash para búsqueda + control de acceso por campo + bitácora. |
| Escala mayor a lo previsto | Splink migra a Spark sin reescribir el modelo; el blocking ya acota el costo. |
| Uso indebido / sin base legal | Propósito lícito documentado, autorización, acceso por rol y auditoría desde E1 (§6). |

---

## 9. Decisiones pendientes (contigo)

1. **Propósito lícito** del tratamiento (investigación autorizada / cumplimiento
   / anti-fraude) y si se puede **minimizar** (¿omitir CURP en algún flujo?).
2. ¿La aprobación de mapeos y la banda de revisión viven en el **front** (vista
   nueva) o en CLI al principio?
3. **`photo_url`/RENAPO**: ¿hay base legal para traer fotos por CURP, o se deja
   oculto?
4. ¿La API de entidades vive en el **mismo servicio** que la búsqueda o en uno
   aparte? ¿Se comparte Fz1 con terceros (requiere acuerdo de confidencialidad)?
