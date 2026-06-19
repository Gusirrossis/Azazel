# PLAN_BACKEND_CENTRAL.md — Servicio de Datos de Entidades (SDE)

**Estado:** diseño para aprobación · 2026-06-17 · (nombre/repo definitivos por confirmar)
**Una frase:** un backend **de solo datos** (almacén + consulta) que **Azazel alimenta** y del que
**varios sistemas OSINT (FLUX, Gotham, Fz1 y futuros) consultan** la información que necesitan, cada
uno en **su forma**, sin que el backend cargue lógica de aplicación de ninguno.

---

## 1. Alcance (qué SÍ y qué NO)

**SÍ (responsabilidad del SDE):**
- Almacenar de forma escalable las **entidades resueltas** que Azazel produce (personas ancla CURP,
  relaciones, evidencias, procedencia) y datasets masivos (padrón, leaks) como capa servida.
- Exponer una **API de consulta** estable, versionada y segura, con **proyección por sistema**
  (cada consumidor recibe su estructura) configurable como **dato (manifest/receta), no código**.
- Ingesta **push/ETL** desde Azazel, idempotente y trazable.
- Seguridad de grado producción: autenticación máquina-a-máquina + usuario, acceso por campo/rol
  (PII/LFPDPPP), TLS, bitácora de procedencia y de acceso.

**NO (queda fuera, vive en cada sistema):**
- Lógica de aplicación de los consumidores (grafo de Gotham, watchlist, cámaras, transforms,
  facecheck, render 3D de FLUX, etc.). Cada sistema **conserva su backend nativo** y solo consulta.
- **Motor de resolución de identidad** (decisión del usuario: "resolución en cada consumidor"). El
  SDE **no** fusiona ni deduplica por su cuenta: almacena lo que Azazel empuja (ya resuelto en Fase 2,
  ancla CURP) y sirve; cada consumidor aplica su propio matching downstream.
- Enriquecimiento OSINT en vivo (Maigret, etc.) — eso es de Gotham.

> Principio: **el SDE es una "fuente de verdad servida", no una aplicación.** Hace una cosa y la
> hace excelente: guardar y servir entidades, de forma dinámica y segura.

---

## 2. Decisiones tomadas (con el usuario, 2026-06-17)

| Decisión | Valor |
|---|---|
| Fundación | Backend **nuevo y dedicado**, solo consultar+almacenar; los sistemas conservan su back y consultan |
| Ingesta | **Push/ETL** desde Azazel + **API pull** para consumidores |
| Resolución | **En cada consumidor** (el SDE no resuelve; Azazel resuelve upstream y empuja lo resuelto) |
| Store | **Clúster central compartido** que todos leen (sin copiar 100+ TB) |
| Stack | FastAPI + Postgres + ClickHouse + Redis + Caddy + Docker (consistente con Azazel y Gotham) |
| Patrones | Se **adoptan** los de Gotham (Registry/LookupKey/GenericRecord, ClickHouse, arq/Redis/WS) sin su lógica de app |

---

## 3. Arquitectura

```
 PRODUCTOR                         SERVICIO DE DATOS DE ENTIDADES (VPS de datos)            CONSUMIDORES
 ┌─────────┐   push/ETL           ┌──────────────────────────────────────────────┐
 │ Azazel  │  (idempotente        │  INGESTA (ETL) ── valida ── upsert por         │   (cada uno con su
 │ indexa+ │   por external_id)   │   external_id ── procedencia                   │    propio backend,
 │ resuelve│ ───────────────────▶ │                                                │    solo consultan):
 │ (Fase 2)│                      │  STORE                                         │
 └─────────┘                      │   · Postgres: canónico (persona, relación,     │◀── Gotham  (registry/GenericRecord)
                                  │     evidencia, procedencia, auth, manifests)   │◀── FLUX    (nodo plano + edges + inject)
                                  │   · ClickHouse: masivo (personas a escala,     │◀── Fz1     (su esquema)
                                  │     padrón, leaks) — particionado, ancla CURP  │◀── (futuro = otro manifest)
                                  │   · Blob store (MinIO/S3): fotos, PDFs          │
                                  │                                                │
                                  │  API CONSULTA (REST /v1, versionada)           │
                                  │   · Registry/LookupKey + GenericRecord         │
                                  │   · PROYECCIÓN por sistema (manifest/receta)   │
                                  │   · cursor pagination · jobs (arq+Redis+WS)    │
                                  │  SEGURIDAD: API-key M2M + JWT + RBAC campo/rol  │
                                  │   + TLS(Caddy) + rate-limit + audit            │
                                  └──────────────────────────────────────────────┘
```

**Separación productor/almacén/consumidor:** Azazel sigue indexando en su lago (OpenSearch) y
resolviendo; el SDE es la **capa servida** (lo resuelto, listo para consumo); los sistemas son
clientes. Esto permite escalar cada parte por separado y evita acoplar el almacén a ninguna app.

---

## 4. Modelo de datos

### 4.1 Vocabulario común (interoperabilidad)
- **EntityKind** (StrEnum, extensible): `person, company, school, business, place, vehicle, address,
  document, email, phone, domain, ip, rfc, curp, btc, eth_addr, telegram_handle, …` (se adopta el de
  Gotham para que su proyección sea directa).
- **LookupKey** (claves de búsqueda normalizadas): `curp, rfc, name, phone, email, cp, address,
  placas, folio, cve, …`.
- **GenericRecord** (envoltura uniforme de salida): `{ record_id, kind, category, display_title,
  fields:{…}, identifiers:{LookupKey→valor}, provenance:[…], confidence }`.

### 4.2 Postgres (canónico, relacional, fuente de verdad transaccional)

> **CORRECCIÓN CRÍTICA (del review): la clave de identidad NO puede ser el `entidad_id` de Azazel.**
> Ese id = `sha256(ancla:valor)` y **CAMBIA** cuando el ancla mejora (RFC→CURP). Si lo usáramos como
> id estable, al re-anclar una persona se "movería" su identidad y los consumidores (FLUX inyecta por
> `external_id`) quedarían con referencias rotas. **Modelo correcto:**
- `entidades`: **`id` propio del SDE, opaco y estable (ULID)** — nunca cambia. `kind`. `campos`
  JSONB. `identifiers` JSONB (LookupKey→valor). Índices por curp/rfc, GIN sobre campos, keyset por id.
- `entidad_external_ids`: **N:1** — una entidad puede tener varios `external_id` de Azazel a lo largo
  del tiempo (re-anclaje). El upsert de ingesta resuelve external_id→id_SDE.
- `entidad_alias` (redirección): `id_viejo → id_nuevo` para **merge/split** de entidades (Azazel
  re-resuelve aguas arriba); los consumidores resuelven referencias viejas sin romperse.
- **Procedencia y conflicto POR CAMPO** (no por registro): cada campo guarda `valor + confianza +
  version_origen + fuente + timestamp`. Esto habilita la **política de conflicto** (last-write-wins
  por recencia/confianza, o multi-valor con procedencia) que Azazel hoy deja sin resolver.
- **Borrado real, no solo `activo`**: el soft-delete no basta para LFPDPPP (ver §8 ARCO).
- `relaciones`: `origen_id`, `destino_id`, `tipo` (familia/pareja/laboral/…), `etiqueta`, `data`
  JSONB, `direccion`. (Aristas tipadas — cubre lo que FLUX pone en relCategory y lo que Gotham pinta.)
- `evidencias`: `id`, `entidad_id`, `tipo` (foto/pdf/video), `url` (al blob store), `meta` JSONB,
  `procedencia`.
- `procedencia` / `fuentes`: de qué archivo/dataset/dump salió cada dato (heredado de Azazel) —
  auditoría y derecho a saber el origen.
- `consumidores` (sistemas): `clave`, `nombre`, `api_key_hash`, `scopes`, `activo`.
- `manifests` (proyecciones/registries por sistema y por fuente): definición JSONB editable.
- `auth` (usuarios/sesiones si aplica passthrough de usuario), `audit_log` (append-only).

### 4.3 Capa masiva (frontera de datos — DECISIÓN FUNDACIONAL, ver §15)

> **CORRECCIÓN (del review): no duplicar 100+ TB.** El plan v1 ponía "personas" en PG *y* en
> ClickHouse, mientras Azazel YA indexa todo en **OpenSearch**. Hay que fijar la frontera tajante:
- **PG = lo CANÓNICO y mutable**: entidades resueltas + relaciones + evidencias + auth/manifests/
  audit (decenas de millones de filas, transaccional). **Fuente de verdad.**
- **Capa MASIVA cruda (padrón ~88M, leaks)** = **se REUSA el lago OpenSearch de Azazel** (consulta
  **federada/proxy** desde el SDE), NO se copia a un ClickHouse nuevo. Recomendación por defecto:
  evitar mover/duplicar 100+ TB; el SDE federa contra el índice existente.
- **ClickHouse** queda como **opción**, solo si aparece una consulta analítica masiva que OpenSearch
  no sirva bien; entonces sería una **proyección reconstruible** desde PG/lago, no fuente de verdad.
- Idempotencia a escala: si algo se materializa en CH, vía `ReplacingMergeTree(version)` + staging;
  el borrado por LFPDPPP allí es por `ALTER … DELETE`/colapso de partición (ver §8), no trivial.

### 4.4 Blob store (MinIO/S3)
- Fotos, PDFs y media como **objetos con URL firmada**, no base64 en payload (lección de FLUX/Gotham).
  Las evidencias referencian URLs; el SDE sirve/firma el acceso con la misma auth.

---

## 5. Contrato de API de consulta (REST `/v1`, OpenAPI)

- `GET /v1/registries` — qué fuentes/proyecciones hay (accepts/emits, estado).
- `GET /v1/registries/{name}/schema` — esquema dinámico (FieldSpec).
- `POST /v1/search` — `{ lookups:{LookupKey→valor}, terms, filtros, cursor, limite }` →
  `{ records:[GenericRecord], cursor, total_aprox }`. **Paginación por cursor** (no offset).
- `GET /v1/records/{id}` — una entidad completa (con relaciones y evidencias, según scopes).
- `POST /v1/enrich` — fan-out cross-fuente por `identifiers`.
- `GET /v1/records/{id}/relaciones` — el grafo local de una entidad.
- `GET /v1/projection/{sistema}/...` — **proyección por sistema** (FLUX recibe `{nodes[],edges[]}`
  plano + endpoint compatible con su `inject`; Gotham recibe `GenericRecord/Entity`; Fz1 el suyo).
- `POST /v1/jobs/...` + `WS /v1/ws/jobs/{id}` — consultas/exports pesados (arq+Redis+WS).
- `GET /v1/export/{id}.{json|csv|...}` — export multi-formato.
- Convenciones: **versionado en la ruta** (`/v1`), errores **RFC 7807**, `ETag`/`If-None-Match`,
  `Idempotency-Key` en ingesta, límites de tamaño, contrato **OpenAPI** generado y publicado.

---

## 6. Ingesta (ETL push desde Azazel)

- **Mecanismo**: Azazel empuja lotes de entidades resueltas (o un worker del SDE las jala del lago
  por evento). **Idempotente** por `external_id` (= `entidad_id` de Azazel): re-enviar **actualiza**,
  no duplica. `Idempotency-Key` por lote.
- **Semántica de actualización**: configurable por campo — por defecto **rellena/actualiza sin
  borrar** (merge no destructivo, como FLUX), con procedencia por campo; borrar requiere intención
  explícita (LFPDPPP).
- **Trazabilidad**: cada registro/campo conserva su procedencia (archivo/dump/fuente) y `version_origen`.
- **Escala**: lotes a ClickHouse para lo masivo; Postgres para lo canónico/relacional. Backpressure
  y reintentos (cola).

---

## 7. Proyección dinámica por sistema (el corazón del dinamismo)

- Cada sistema (y cada fuente) se declara con un **manifest/receta** (dato JSONB/TOML editable):
  qué LookupKeys acepta/emite, el mapeo de campos canónicos → su forma, y el endpoint/contrato que
  expone. **Unifica** las recetas de proyección de Azazel + los registries de Gotham en un concepto.
- **Agregar un sistema o una fuente = un manifest**, sin reescribir ni redesplegar el core
  (arquitectura **puertos-adaptadores**: el core no conoce a los consumidores; los adaptadores sí).
- Versionado de cada manifest/contrato; pruebas de contrato por consumidor.

---

## 8. Seguridad, PII y cumplimiento (LFPDPPP) — el riesgo aquí es PENAL, no solo técnico

> El review (lente seguridad) fue contundente: concentrar **padrón electoral + leaks + biométricos**
> en un punto es exactamente lo que LFPDPPP sanciona (arts. 10/19/67-69) si no hay base de
> legitimación, finalidad declarada y tratamiento diferenciado. Esto se **diseña antes de B1**, no se
> parchea al final. **Asesoría legal LFPDPPP previa al despliegue.**

- **Legalidad de cada fuente (control, no solo trazabilidad):** la procedencia de cada dato lleva un
  **estatus legal** obligatorio (lícita-con-base · lícita-restringida-por-finalidad · ilícita/leak).
  Los **leaks** (datos obtenidos ilícitamente por terceros) se marcan **ultra-restringidos** o se
  **excluyen del store servido** si no hay base legal. Finalidad declarada por fuente.
- **Datos de menores:** flag derivado de CURP/fecha → categoría **más restrictiva por defecto NO
  servible**; acceso solo con justificación y registro reforzado (o exclusión total). Property-test de
  que ningún manifest expone menores sin scope.
- **RBAC por CAMPO en el CORE, deny-by-default (NO en el manifest):** un catálogo de clasificación de
  campos (público/restringido/sensible/menor) **versionado en código**; el filtro se aplica en la capa
  de query/serialización **antes** de proyectar — si un campo no está explícitamente permitido, **no
  sale aunque el manifest lo pida**. CURP/RFC **enmascarados por defecto**, revelados solo con scope
  explícito y registro de cada revelación. El **mismo filtro** aplica en `enrich`, `jobs` y `export`
  (no se elude por otro endpoint).
- **Cifrado en reposo + gestión de claves:** volúmenes cifrados (PG, OpenSearch/CH, blobs, backups);
  cifrado a nivel columna/app para CURP/RFC/biométricos; claves separadas del store, con rotación y
  custodia (no en el `.env`). Buckets privados + **URLs firmadas de vida corta**.
- **Autenticación:** **API-key M2M** por sistema (canal de ingesta de Azazel con key dedicada o
  **mTLS**) + **JWT con passthrough de usuario** para auditar por persona. Ciclo de vida de
  credenciales (rotación/expiración/revocación).
- **Derecho al olvido (ARCO) end-to-end:** (1) **tombstone/lista de supresión** que impide la
  **re-ingesta** del `external_id` borrado (anti-resurrección); (2) borrado físico/anonimización real
  en PG + mutación en CH/lago, no solo `activo`; (3) purga del blob + invalidación de caché; (4)
  **propagación/notificación a Azazel y a consumidores** que ya jalaron copias; (5) SLA de atención.
- **Bitácora:** `audit_log` **append-only con integridad** (encadenado/hash); registra
  sistema+usuario, cuándo, **qué registro (ID/hash, NO el PII en claro)**, qué campos sensibles se
  revelaron y el scope invocado. Minimización: el audit no debe perpetuar el dato.
- **Transporte:** TLS/HSTS por Caddy. **Validación** estricta de entrada (`extra="forbid"` en ENTRADA;
  en SALIDA NO, para tolerancia forward de nuevos EntityKind/LookupKey — ver §18).
- **Respuesta a brechas:** plan de notificación de vulneraciones a titulares (obligación LFPDPPP).

---

## 9. Jobs asíncronos + tiempo real

- Consultas/exports pesados → **arq + Redis Streams + WebSocket** de progreso (`started/progress/
  done/error`), patrón ya probado en Gotham. Respuestas rápidas síncronas; pesadas a worker.

---

## 10. Escalabilidad

- **ClickHouse particionado** + índices bloom/ngram para lo masivo (88M+ filas, leaks).
- **Cursor pagination** (no offset) en todo.
- **Caché** (Redis) de consultas calientes; **read replicas** de Postgres si hace falta.
- **Workers** y API escalan independientemente (imágenes Docker separadas).
- Un **solo clúster** que todos leen → sin duplicar 100+ TB.

---

## 11. Stack y estructura de repo

- **Lenguaje**: Python 3.12, **FastAPI**, **Pydantic v2**, SQLAlchemy async + **Alembic**, **arq**
  (Redis), **clickhouse-connect**, **MinIO/boto3**, **Caddy**, **Docker Compose**.
- **Estructura** (hexagonal/puertos-adaptadores):
  ```
  sde/
    core/        # dominio: entidad, lookupkey, genericrecord, manifest (sin I/O)
    store/       # adaptadores: postgres/, clickhouse/, blobs/
    api/         # FastAPI routers (v1), auth, errores, openapi
    ingest/      # ETL desde Azazel
    projection/  # adaptadores por sistema (flux, gotham, fz1) — data-driven por manifest
    jobs/        # worker arq, eventos, ws
    config/      # settings 12-factor
  alembic/  tests/  deploy/  pyproject.toml
  ```

---

## 12. Mejores prácticas aplicadas (no negociables)

- **Contratos primero**: OpenAPI + JSON Schema publicados; **pruebas de contrato** por consumidor
  (FLUX/Gotham/Fz1) en CI → si cambiamos un campo, los consumidores lo saben antes de romperse.
- **Type-safety total**: `mypy --strict`, `ruff`, Pydantic en bordes.
- **Migraciones** versionadas (Alembic) idempotentes; ClickHouse DDL versionado.
- **Tests**: unidad (dominio puro) + integración (DB real en Docker) + contrato + property-based para
  la proyección (ida-y-vuelta sin pérdida). Cobertura de los caminos de PII.
- **Observabilidad**: logs estructurados, métricas (Prometheus), trazas (OpenTelemetry), `GET /health`
  y `GET /ready`.
- **12-factor**: config por entorno, sin estado en el proceso, logs a stdout, paridad dev/prod.
- **Idempotencia** en ingesta; **versionado** de API y de manifests; **errores** RFC 7807.
- **Seguridad** desde el diseño (no parche): least-privilege, validación, secretos gestionados, audit.
- **CI/CD**: lint+type+test en cada push; despliegue reproducible (Docker) a VPS.

---

## 13. Plan por fases (entregables + criterio de aceptación)

| Fase | Entregable | Aceptación |
|---|---|---|
| **B0 — Cimientos** | Repo, scaffolding hexagonal, CI (lint/type/test), Docker Compose (PG+CH+Redis+MinIO+Caddy), settings 12-factor, `/health` | `make dev` levanta todo; CI verde |
| **B1 — Núcleo de datos** | Esquema Postgres canónico (entidades/relaciones/evidencias/procedencia) + Alembic; tablas ClickHouse; blob store | migraciones aplican; tests de modelo |
| **B2 — Ingesta ETL** | Push idempotente desde Azazel (por `external_id`) + procedencia + reintentos | re-ingestar no duplica; E2E con datos de Azazel |
| **B3 — API de consulta** | `/v1` Registry/LookupKey/GenericRecord + search/enrich + cursor + OpenAPI | contrato publicado; pruebas de contrato |
| **B4 — Seguridad** | API-key M2M + JWT + RBAC por campo + TLS + rate-limit + audit | un sistema solo ve lo permitido; audit registra |
| **B5 — Proyección por sistema** | Manifests + adaptadores FLUX (nodo plano+edges+inject) y Gotham (registry); plantilla para Fz1/futuros | FLUX y Gotham leen del SDE sin cambiar su core |
| **B6 — Jobs + tiempo real + export** | arq+Redis+WS para pesados; export multiformato | job largo emite progreso; export OK |
| **B7 — Endurecer + desplegar** | observabilidad, cuotas, DR/backups, derecho al olvido, despliegue VPS | corre en VPS con TLS; runbook |

---

## 14. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Contratos divergen y rompen consumidores | OpenAPI + pruebas de contrato en CI; versionado `/v1`; manifests versionados |
| PII expuesta (padrón/leaks) | RBAC por campo + audit + TLS + least-privilege desde B4 (no al final) |
| Doble normalización (Azazel y consumidores) | Resolución upstream en Azazel; el SDE no resuelve; consumidores solo matching propio |
| ClickHouse mal particionado a escala | Adoptar el diseño probado de Gotham (partición por estado/origen, índices bloom/ngram) |
| "Todo el universo" como en FLUX | API por cursor/filtro; nunca cargar/reemplazar todo |
| Acoplar el SDE a una app | Puertos-adaptadores; el core no conoce consumidores |
| Media inflando payloads | Blob store + URLs firmadas, no base64 |

---

## 15. Qué falta por definir (decisiones abiertas)

**Bloqueantes para B0/B1 (necesitan decisión del usuario):**
1. **Repo + nombre** del SDE (nuevo repo propio recomendado).
2. **Frontera de la capa masiva** (§4.3): reusar OpenSearch de Azazel por federación (recomendado,
   cero copia) vs ClickHouse propio. Define todo el stack de datos.
3. **Topología física/red** (§17): cuántos VPS, dónde vive cada store, dónde corre el lago/CAS de
   Azazel (¿Mac M4?, ¿VPS?), red privada/VPN entre productor↔SDE↔consumidores.
4. **Legitimación LFPDPPP por fuente** (§8): base legal y finalidad de padrón vs leaks vs Azazel; si
   los leaks pueden servirse legalmente o se excluyen. **Asesoría legal antes del despliegue.**
5. **Modelo de acceso**: ¿RBAC por sistema, por rol, por usuario (passthrough JWT)? ¿single-tenant
   compartido con RBAC-por-campo (recomendado) vs multi-tenant con aislamiento por fila?
6. **Alcance de EntityKind en v1**: ¿solo persona (recomendado, el resto roadmap) o también
   empresa/vehículo/dirección…? Cada kind no-persona necesita ancla/normalizador/productor.

**Decisiones de diseño a cerrar en el plan (las aterrizo, confirmas):**
7. **Política de conflicto por campo** cuando dos fuentes difieren (recencia/confianza/multi-valor) —
   hoy Azazel la ignora; el SDE la decide (§4.2).
8. **Contrato de ingesta**: push de Azazel (recomendado, vía **outbox durable** reusando su cola
   Postgres+SKIP LOCKED) vs pull del SDE; evento = upsert + delete/tombstone con `version_origen/seq`
   monótono por external_id (§6).
9. **Gramática del manifest de proyección** (§7): su JSON Schema + clases (por-ítem, colección, grafo
   nodes+edges) **extendiendo** la receta ya construida en Azazel (PLAN_ENTIDADES §10), no inventando.
10. **Pruebas de contrato consumer-driven** (§18): FLUX/Gotham aportan su fixture; Fz1 se congela como
    *golden file* del `fz1_bundle` hasta que publique su esquema.
11. **Push de cambios a consumidores** (§18): solo pull vs webhooks/endpoint **delta** (changes-since)
    para invalidar copias stale en FLUX/Gotham.
12. **DR/backup/retención** (§17): RPO/RTO por store, cifrado de respaldos, derecho al olvido en CH/lago.
13. **Volumetría/SLO objetivo**: nº de entidades canónicas (PG) vs filas masivas, throughput de
    ingesta, latencia objetivo de consultas calientes — sin números, partición/índices/caché es adivinar.

## 16. Puntos clave de funcionalidad y cómo se resuelven

| # | Funcionalidad | Cómo se resuelve |
|---|---|---|
| 1 | **Identidad estable** (no romper referencias al re-anclar) | `id` ULID propio del SDE + `external_id` N:1 + tabla de alias (merge/split) (§4.2) |
| 2 | **Ingesta idempotente y trazable** | Push outbox + upsert por external_id→id + evento upsert/tombstone + `version_origen` (§6) |
| 3 | **Conflicto de actualización** | Procedencia **por campo** (valor+confianza+versión+fuente) + política recencia/confianza (§4.2) |
| 4 | **Consulta unificada por identificador** | API `Registry/LookupKey/GenericRecord` + `search/enrich` + cursor opaco por motor (§5) |
| 5 | **Proyección por sistema, sin reescribir** | Manifests con **gramática versionada** (por-ítem/colección/grafo) + puertos-adaptadores (§7) |
| 6 | **Acceso por campo / anti-fuga (PII)** | RBAC **en el core, deny-by-default**, no en el manifest; CURP/RFC enmascarados (§8) |
| 7 | **Legalidad / leaks / menores** | Estatus legal por fuente + leaks ultra-restringidos + flag menor no-servible (§8) |
| 8 | **Derecho al olvido (ARCO)** | Tombstone anti-resurrección + borrado real PG/CH + purga blob + propagación (§8) |
| 9 | **Cifrado en reposo** | Volúmenes + columna (CURP/RFC) + backups con claves separadas (§8) |
| 10 | **Escala / no duplicar 100+TB** | PG canónico + federar el lago OpenSearch de Azazel; cursor; caché (§4.3, §10) |
| 11 | **Grafo a escala** | `/relaciones` = solo vecindario 1-salto con cursor; multi-hop lo arma el consumidor (§5) |
| 12 | **Consultas/exports pesados** | Jobs `arq + Redis + WebSocket` con progreso (§9) |
| 13 | **Estabilidad de contratos** | OpenAPI + **pruebas consumer-driven** + versionado por eje (§18) |
| 14 | **Push de cambios / cache stale** | Endpoint delta (changes-since) o webhooks reusando arq+Redis (§18) |
| 15 | **Media/evidencia** | Blob store (o reusar el CAS sha256 de Azazel) + URLs firmadas, no base64 (§4.4) |
| 16 | **Operación seria** | Topología/red, DR/backup, migraciones online, CD/rollback, SLOs, runbooks (§17) |

---

## 17. Topología, red y operación (SRE)

- **Topología multi-VPS:** **Caddy/API** en VPS-borde (única superficie pública :443, TLS); **PG +
  Redis + blobs** en VPS-datos; **lago OpenSearch/CH** en host analítico aislado; **fronts** en su
  VPS. Tráfico interno por **red privada/VPN (WireGuard)** — PG/Redis/lago **nunca** públicos.
- **Reconciliación dev-Win/prod-Mac/VPS:** decidir dónde vive el store central frente a los 100+ TB
  que ya tiene la Mac M4 (¿el SDE federa contra la Mac/lago, o se monta junto al lago en el VPS de
  datos?). Define latencia, egress y la viabilidad del "cero copia".
- **ETL:** patrón **push con outbox durable** del lado Azazel (reusa cola Postgres + SKIP LOCKED +
  leases ya probada) → entrega al-menos-una-vez con idempotencia; alerta de lag.
- **Backup/DR:** PG con **PITR** (WAL a object storage offsite) + dump lógico; lago/CH con BACKUP
  incremental por partición; blobs con versioning/replicación; **prueba de restore documentada**.
  RPO/RTO por store. Qué es reproducible desde Azazel (lago) y qué no.
- **Migraciones online:** PG **expand/contract** (nunca rename destructivo en un paso; índices
  `CONCURRENTLY`; `lock_timeout`); DDL del lago/CH versionado; backup-antes-de-migrar.
- **CD/rollout/rollback:** imágenes a un registry con tag inmutable; deploy con gate de **pruebas de
  contrato** antes de prod; rollback = redeploy del tag anterior; manifests con activación reversible.
- **Cuotas/aislamiento:** rps, tamaño de página/export y jobs concurrentes por consumidor; exports
  pesados en cola arq separada con circuit breaker. Dimensionamiento + costo por VPS.
- **Secrets:** secret manager concreto (no `.env` suelto); rotación de API-keys M2M y credenciales DB.
- **Observabilidad:** SLOs (lag de ingesta, latencia/errores por consumidor, saturación del lago,
  cola arq, disco) + métricas/trazas + runbooks por alerta (estilo `RUNBOOKS.md` de Azazel).

## 18. Versionado, contratos y ciclo de vida de la entidad

- **Versionado por eje (no solo `/v1`):** (a) contrato HTTP por ruta `/v1` (cambios breaking); (b)
  esquema de salida `GenericRecord`/proyección con **versión propia** negociable por consumidor
  (cabecera/`schema_version`); (c) cada **manifest** con su versión. Regla aditivo-vs-breaking;
  **tolerancia forward** en el cliente (por eso la SALIDA no usa `extra="forbid"`).
- **Pruebas de contrato consumer-driven:** cada consumidor aporta un fixture ejecutable de lo que
  espera; el SDE las corre en CI y **falla** si las rompe. Fz1 = *golden file* del `fz1_bundle` real.
- **Ciclo de vida de entidad:** eventos `created/updated/merged/split/deactivated/deleted` con id
  superviviente + tabla de redirección (los consumidores resuelven ids viejos) + tombstones.
- **Evolución de esquema/manifest:** versionado semántico, soporte simultáneo de N versiones, ventana
  de deprecación `/v1→/v2`, y **backfill/recompute** cuando cambia una receta.

---

## 19. Contratos de los consumidores (FLUX · Gotham · Fz1) — el contrato cierra

> **Recordatorio de alcance:** el SDE **solo alimenta con las entidades que Azazel produce**. Cada
> sistema conserva SU backend para SUS funcionalidades (Gotham sus transforms, Fz1 su Nexus API y sus
> bases locales DBF/XLSX, FLUX su grafo) — **ahí no nos metemos**. El SDE expone **un adaptador por
> sistema** que traduce su contrato ↔ el modelo canónico. Con el contrato de consulta de Fz1
> (`ejemplo-consultas-locales-fz1.json`, Nexus API :3001) los **tres consumidores quedan definidos**:

| Sistema | Cómo PIDE (request) | Cómo RECIBE (response) | Tiempo real |
|---|---|---|---|
| **FLUX** | Inyección masiva (push `inject`) + lee el universo por caso | **Nodos planos + aristas** (esquema `NODE_COLS`); upsert por `external_id` | — |
| **Gotham** | `registry.search` por **LookupKey**; transforms (Entity in→out) | `GenericRecord` / `Entity{kind,value,metadata}` | **WebSocket** (jobs) |
| **Fz1** | `POST /api/search {query, mode, categories[], filters{…}}`; `POST /api/advanced-search {nombre,paterno,materno,estado}`; por **identificador** (CURP/placa/tel autodetectado) | Resultados por **categoría** (INE/PLACAS/BANCOS) | **SSE** (`/api/search/stream`) |

### 19.1 Modelo de consulta CANÓNICO (lo que el SDE entiende internamente)
Todos los adaptadores mapean su request a este modelo, y la respuesta canónica a su forma:
- **Texto** libre **+ identificador** con **autodetección** (CURP/RFC/placa/teléfono → ruta a la
  fuente/registry correcto y, como Fz1, desactiva ranking por relevancia en lookup exacto a escala).
- **Filtros estructurados** (unión de lo que piden los tres): `nombre/paterno/materno`, `estado`,
  `genero`, `edad_min/edad_max`, `anio_nacimiento`, `direccion`, `telefono`, `cp`, `email`.
- **Categorías/fuentes = registries**: INE/padrón, placas, bancos, leaks, **entidades resueltas de
  Azazel**… cada una un registry con su `accepts/emits` (LookupKey).
- **Paginación por cursor** + **streaming** sobre **una sola capa de eventos** que sirve **SSE**
  (Fz1) **y WebSocket** (Gotham): eventos `results/status/done/error` (Fz1) ≡ `progress/done/error`
  (Gotham) — mismo bus (Redis), dos transportes.

### 19.2 Adaptadores (uno por sistema, en `projection/`)
- `projection/flux/` — proyecta a `NODE_COLS` + aristas; expone el contrato `inject`/`universe`.
- `projection/gotham/` — expone un **registry** que Gotham monta como fuente (search por LookupKey →
  `GenericRecord`).
- `projection/fz1/` — expone `/api/search`, `/api/advanced-search`, `/api/search/stream` con los
  `filters` y `categories` de Fz1, mapeados al modelo canónico.
- **Agregar un 4º sistema = un adaptador + su manifest, sin tocar el core** (puertos-adaptadores).

> **Construcción profesional, no "vibecoding":** el SDE NO inventa un formato y obliga a todos; expone
> el **modelo canónico** y N adaptadores delgados. El core (dominio + store + seguridad) no conoce a
> ningún consumidor; los adaptadores viven en el borde y se prueban con **contratos consumer-driven**
> (§18). Por eso un cambio en un consumidor no toca el core, y el core no puede filtrar PII por un
> manifest mal hecho (§8).
