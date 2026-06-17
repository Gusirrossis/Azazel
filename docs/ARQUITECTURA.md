# Arquitectura de Azazel (estado actual)

Sistema de normalización e indexación masiva de archivos. Este documento refleja
lo **ya construido y funcionando** (hitos M1–M6). Para el trabajo futuro ver
[`PLAN_ENTIDADES.md`](PLAN_ENTIDADES.md) y [`PLAN_PILOTO_M4.md`](PLAN_PILOTO_M4.md).

---

## 1. Qué resuelve y principios

Catalogar, filtrar, deduplicar e indexar volúmenes enormes de archivos (escala de
100+ TB) que llegan en discos **desechables**: el sistema no es un índice sobre
discos ajenos, es un **repositorio propio** que copia lo valioso a un almacén
permanente antes de que el disco se deseche.

Principios de diseño (todos implementados):

- **Regla de oro:** cada archivo se **lee una vez**, se procesa una vez, y al
  reanudar no se reprocesa nada.
- **Solo se lee el head para filtrar** (no el archivo completo) — el I/O es el
  cuello físico.
- **Dedup por contenido** (`sha256(bytes)`): el mismo contenido nunca se copia dos veces.
- **Nada se borra:** lo no interesante va a **frío reversible** (se preserva y se
  puede re-evaluar), jamás se elimina.
- **Puerta de integridad sagrada:** un disco es "seguro para desechar" solo cuando
  el 100 % de sus archivos está a salvo (verificado o movido a frío).
- **Sin números mágicos:** todo parámetro vive en config tipada y versionada (perillas K1–K14).

---

## 2. El pipeline (6 fases)

```
catálogo → precalificación → worker → mover-frío → verificación → puerta
(scandir)  (doble filtro)    (blobs+    (COLD a      (re-hash vs   (¿seguro
                              índice)    frío)        esperado)     desechar?)
```

- **Catálogo:** `os.scandir` en BFS (sin recursión), solo metadata → filas en cola. Idempotente (re-catalogar no duplica). Sanea nombres no-UTF8 de volúmenes Latin-1.
- **Precalificación:** el doble filtro (§4) puntúa y enruta a HOT o COLD.
- **Worker:** lectura única — `sha256` al vuelo + dedup + copia del blob + extracción de texto (L1) + documento al índice. Corre **en paralelo con el filtro** (los resultados son buscables a segundos de iniciar).
- **Mover-frío:** los COLD también se copian a un almacén frío (reversible) antes de la puerta.
- **Verificación:** re-lee cada blob y compara su `sha256` con el esperado (detecta corrupción silenciosa).
- **Puerta:** veredicto por disco, sin override manual.

**Máquina de estados:** `PENDIENTE → PRECALIFICADO → EN_PROCESO → INDEXADO →
VERIFICADO → HECHO`; `COLD` reversible (↔ PENDIENTE/PRECALIFICADO); `ERROR`
reprocesable. `COLD → ERROR` si un frío no se puede respaldar (bloquea la puerta).

---

## 3. Cola durable e identidades

- **Postgres** como plano de control: `claim` atómico con `FOR UPDATE SKIP LOCKED`
  + **leases** por reloj (`clock_timestamp()`), heartbeat, y recuperación de
  huérfanos (worker muerto → su fila vuelve sola). N workers concurrentes sin contención.
- **Dos hashes que no se confunden:**
  - `archivo_id = sha256(disco_id:ruta_rel | tamaño | mtime)` — clave de **trabajo** (idempotencia de la cola y del índice).
  - `hash_contenido = sha256(bytes)` — clave del **almacén** (dedup, verificación).
- **Robustez por archivo (dead-letter):** un archivo envenenado (bytes hostiles,
  lib de compresión que revienta, decode raro) va a `ERROR` con su motivo y el
  pipeline **sigue**. Ningún archivo individual detiene una corrida automática.

---

## 4. El doble filtro (precalificación T0–T4)

Decide, leyendo solo el head, si un archivo entra al embudo caro (**HOT**) o va a
**frío** reversible. Puntaje 1–100 → router: **≥65 HOT, 35–64 franja gris, <35 COLD**.
Versión actual: `reglas-v3-lista-blanca`.

- **T0 — kill-rules** (sin abrir): tamaño 0, nombres basura (`thumbs.db`…),
  extensiones desechables, rutas de caché.
- **T1 — tipo real** (cascada, la extensión JAMÁS decide): detectores
  **estructurales primero** (OLE streams, entradas ZIP-Office, DBF por header),
  luego firmas binarias, luego libmagic si está disponible.
- **T2 — estructura del head** (64 KB): entropía de Shannon (**<3.5** texto,
  **>7.5** comprimido/cifrado), `csv.Sniffer` + consistencia de columnas,
  JSON/NDJSON/XML/SQL, correos (cabeceras RFC 822), HTML.

### Lista blanca (decisión de negocio)

Solo los **tipos de interés** entran al embudo; el resto va a frío reversible
(ampliar la lista + `rescore-frio` rescata lo excluido). Incluye:

- **Texto y documentos:** `text/*` (txt, csv, tsv, logs…), PDF, RTF, Office legado
  (OLE) y moderno (docx/xlsx/pptx), OpenDocument, EPUB, WordPerfect, DjVu, XPS, iWork.
- **Correos:** `.eml`/mbox (rfc822), `.pst`, Outlook Express, Lotus Notes, `.msg`.
- **Datos:** JSON/NDJSON/XML, SQL (dumps), SQLite (`.db`), Access (`.mdb/.accdb`),
  Parquet, DBF, pg_dump, SQL Server `.bak`, Avro, ORC.
- **Excluido a propósito:** HTML (markup sin información orgánica). **Multimedia,
  ejecutables y fuentes → frío.**

### T3 — contenedores (prioridad alta)

Los comprimidos tienen **prioridad** (la mayoría de lo útil viene dentro) y se
**exploran por completo**. Formatos explorables: **ZIP, 7z, RAR, tar, gz, bz2, xz**
(cadenas anidadas multi-formato). Las **imágenes de disco** (ISO/VHD/VHDX/VMDK/
QCOW/E01) se **preservan íntegras** sin explorar (por ahora).

Guards anti zip-bomb (de **seguridad**, no de capacidad — un 7z de 15 GB que abre a
200 GB se explora entero): profundidad ≤10, **ratio ≤300:1**, **≤1 TB** descomprimido,
**≤1 000 000 entradas**, timeout **1800 s**. Una violación va a frío reversible
(nunca se pierde; subir la perilla + `rescore-frio` lo explora).

### T4 — ML (pendiente)

Solo afinaría la franja gris. Diseño: **magika** (red preentrenada de Google, ~1 MB,
detección de tipo por head+tail) como capa extra de T1, y **LightGBM** útil/no-útil
con features de señales T0-T2 + head+tail. Bloqueado por el **set etiquetado** del
usuario. Mientras tanto, la franja gris va a HOT (calibrado a recall).

---

## 5. Almacén e índice

- **Almacén permanente content-addressed:** blobs por `sha256` con layout
  `ab/cd/abcd…`, inmutable, deduplicado. Interfaz agnóstica del backend
  (**MinIO/S3** en producción; carpeta local en piloto/dev). Almacén **frío**
  separado para los COLD.
- **Índice OpenSearch:** metadatos + texto buscable. Búsqueda por **nombre**
  (`wildcard` field — decisión de costo: ~4 TB vs ~15–25 TB de n-gram completo) y
  por **contenido** (`texto_indexable`). `_id = archivo_id` → reindexar sobrescribe,
  nunca duplica. Bulk con triple trigger de flush + retry/backoff + dead-letter.

---

## 6. API y front

- **API FastAPI** (el front nunca toca OpenSearch directo): búsqueda con filtros,
  facetas y paginación profunda (`search_after` + PIT); descarga del original desde
  el almacén; **pipeline desde el front** con carpeta **origen** y **destino**
  elegibles y **nº de workers** configurable (auto = núcleos − 2); inventario de
  **preservados sin explorar**; estadísticas. Auth por API key, rate-limit,
  esquemas `extra="forbid"` (el cliente nunca manda DSL).
- **Front React + Vite** (tema oscuro): buscador, facetas, resultados con resaltado,
  detalle/descarga, e ingesta con progreso por fase en vivo (filtro ∥ worker).

---

## 7. Perillas de ajuste (K1–K14)

Toda la conducta del sistema se cambia por config tipada y versionada
(`core/config.py`, sobre-escribible por entorno `NORM_*`), no tocando código:
K1 kill-rules · K2 bytes T1 · K3 lista blanca · K4 guards T3 · K5 head T2 ·
K6 umbrales de entropía · K7 pesos del puntaje · K8 umbrales HOT/COLD ·
K9 ML por tipo · K10 claim/lease · K11 límites de extractores · K12 chequeos de
calidad · K13 triggers de flush · K14 reintentos.

---

## 8. Escalado (producción) y estado

- **Topología de producción:** workers en máquinas **aparte** de OpenSearch (para
  no competir por RAM); clúster multi-nodo (master-eligible + data HOT/WARM, heap
  ≤31 GB/nodo, tiering hot-warm-cold con ISM); Postgres en su máquina; almacén
  MinIO/Ceph (la línea de hardware más cara). Escalar = añadir máquinas, sin reescribir.
- **Estado actual:** M1–M6 alcanzados (pipeline completo, búsqueda por nombre y
  contenido, robustez, workers paralelos). **Fase 2 (entidades) en marcha:**
  resolución de personas por ancla (CURP/RFC), recetas de proyección por sistema
  consumidor (incl. el archivo Fz1 completo) y backfill desde el índice ya existente
  — ver [PLAN_ENTIDADES.md](PLAN_ENTIDADES.md). **Pendiente:** T4 (falta etiquetado),
  el **piloto en Mac M4** para medir escala real, y de Fase 2: resolución difusa,
  NER sobre documentos, grafo de relaciones y control de acceso por campo (PII).
