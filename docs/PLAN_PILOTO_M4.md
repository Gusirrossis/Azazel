# Plan del piloto en Mac M4 Max (64 GB) — y arranque del T4

Fecha: 2026-06-10 · Estado del código: M1-M6 completos, lista blanca v3, destino elegible desde el front.

---

## 1. La máquina, con números reales

| Recurso | M4 Max | Qué significa para el pipeline |
|---|---|---|
| CPU | **16 núcleos** (12 rendimiento + 4 eficiencia) | 10-12 workers en paralelo cómodos, dejando núcleos para Postgres/OpenSearch |
| RAM | 64 GB unificada (546 GB/s de ancho de banda) | Sobra: OpenSearch 8-16 GB de heap + Postgres 4-8 GB + ~1 GB por worker |
| SSD interno | NVMe de Apple (lecturas de varios GB/s) | Ideal para Postgres + OpenSearch + MinIO (las bases, no los datos) |
| Thunderbolt 5 | **120 Gb/s** (~15 GB/s teóricos) | Los 100 TB van en cajas externas TB5/TB4; el bus no será el cuello |

**Comparado con la prueba de Windows** (WSL 4 GB, 4 cores compartidos, 1 worker, mount lento = 45.7 archivos/s ≈ 1 MB/s): el M4 Max tiene 4× los núcleos, 16× la RAM, lectura nativa sin mount, y N workers. La mejora esperable es de **1-2 órdenes de magnitud**; el piloto da el número exacto.

## 2. Topología recomendada en el Mac: HÍBRIDA

La lección de Windows fue que el mount de Docker es el cuello. En macOS Docker también corre en una VM (mounts VirtioFS — mejores, pero no gratis). Por eso:

- **En Docker** (no tocan los datos masivos): Postgres, OpenSearch, MinIO*, Prometheus, Grafana.
  - Darle a Docker Desktop ≥ 16 GB de RAM y 4 núcleos en Settings → Resources.
- **Nativos en macOS** (leen/escriben los teras): API, catálogo, precalificación, **workers ×N**.
  - `brew install uv node libmagic unar` → T1 con libmagic y RAR completos, nativo.
  - INSTALACION.md ya documenta este modo.

\* Para el piloto, mejor `NORM_ALMACEN_BACKEND=local` apuntando al disco externo de destino (o elegir destino desde el front): así MinIO no intermedia los teras y mides I/O puro. MinIO/clúster regresa en producción.

## 3. Discos: la decisión más importante

1. **Origen** (los 100 TB): caja(s) externa(s) TB5/TB4. El sistema las lee, JAMÁS las escribe.
2. **Destino** (almacén + frío): **otro disco físico** externo, NO el mismo que el origen (evita pelear lecturas contra escrituras en el mismo bus/disco).
3. **Interno del Mac**: solo las bases (Postgres/OpenSearch/MinIO) y el spool temporal.

⚠️ **Capacidad del destino**: con la regla "explorar comprimidos por completo", el almacén guarda el contenedor íntegro **y** sus piezas descomprimidas como blobs. Un 7z de 15 GB → ~215 GB en el almacén. Dimensiona el destino sobre el tamaño *descomprimido* estimado, no sobre los teras crudos. El spool temporal también necesita holgura (una pieza interna de 50 GB se materializa antes de procesarse).

## 4. El piloto: 1-2 TB representativos

**Elegir la muestra** (importa más que el tamaño): que incluya 2-3 de los 7z grandes (15 GB), una mezcla real de PDFs/SQL/DBF/correo, y carpetas "basura" típicas. Cuanto más se parezca al total, mejor extrapola.

**Secuencia** (todo desde la carpeta del backend, con `.env` apuntando a los contenedores):

```bash
# 1. Infra en Docker (sin perfil app: la app va nativa)
docker compose -f deploy/docker-compose.dev.yml --profile full up -d --wait
uv run alembic upgrade head && uv run norm aplicar-indice

# 2. API + front nativos (para ver progreso y buscar en vivo)
uv run norm api &                       # :8000
cd ../normalizacion-front && npm run dev &   # :5173

# 3. TODO el ciclo desde el FRONT: botón "Indexar carpeta…" →
#    origen + destino + workers (Automático = núcleos-2; en el M4 Max: 14)
#    …o por CLI, equivalente:
uv run norm pipeline /Volumes/ORIGEN/muestra-piloto --workers 12
```

**Los workers los decide el sistema en automático (núcleos − 2) y se pueden fijar por corrida desde el front** (modal de indexación → "Workers en paralelo") o con `--workers N`. Son PROCESOS reales: cada uno con sus conexiones, la cola reparte sin duplicar. Para la curva de escalado del piloto, corre la misma muestra con 4, 8 y 12 fijos. Grafana en :3000 grafica backlog/errores en vivo.

**Qué medir** (sale de la tabla `corridas` + Grafana + `norm estado-disco`):

| Métrica | Para qué |
|---|---|
| archivos/s y MB/s por fase (catálogo, filtro, worker, verificación) | el número que extrapola a 100 TB |
| escalado de workers: corrida con 4 vs 8 vs 12 | dónde deja de escalar (CPU vs disco) |
| % HOT vs frío con la lista blanca v3 | cuánto trabajo real hay en tus datos |
| factor de expansión de comprimidos (bytes almacén / bytes origen) | dimensionar el destino definitivo |
| ratio de dedup | cuánto almacén se ahorra |
| errores por motivo + RARs sin herramienta | sorpresas del corpus real |
| crecimiento de Postgres y OpenSearch por millón de archivos | dimensionar las bases para miles de millones |

**Extrapolación**: si el piloto da X MB/s sostenidos, 100 TB ≈ `100·1024·1024 / X / 86400` días. (Ej.: 200 MB/s → ~6 días; 500 MB/s → ~2.4 días.) Con eso sale la decisión: ¿basta el Mac solo, o se compra el clúster de la Fase 7?

**Límites honestos del Mac como prototipo**: una sola máquina hace TODO (leer, extraer, indexar, escribir) — funciona y es perfecto para validar, pero en producción el diseño separa workers de OpenSearch justamente para que no compitan. El Mac es el piloto y posiblemente un primer prototipo operativo; los 100 TB completos con holgura son la Fase 7.

## 5. T4 en paralelo: qué es y qué NO es

**No es un modelo de lenguaje.** Son dos piezas pequeñas:

1. **magika (Google)**: red diminuta (~1 MB, Apache 2.0) que detecta el TIPO leyendo 1024 bytes del inicio + 1024 del final. **Ya entrenada** — se integra como capa extra del T1 sin entrenar nada. ~1 día.
2. **El clasificador útil/no-útil (el T4 real)**: **LightGBM** (árboles, entrena en minutos en laptop). Features: head+tail bytes + las señales que el filtro ya guarda (entropía, tipo, tamaño…). Lo único que falta es **el criterio de negocio etiquetado** — eso no se descarga, lo defines tú.

**Plan de etiquetado** (puede empezar HOY con el corpus ya indexado):

- Muestra estratificada por tipo real (~200-300 por tipo, 2,000-5,000 total).
- Plantilla CSV: `archivo_id, nombre, ruta, tipo_real, puntaje_reglas, etiqueta, comentario`
  - `etiqueta` ∈ {`util`, `no_util`, `duda`} — las `duda` valen oro para la banda de revisión.
- Regla práctica: etiqueta el ARCHIVO, no el tema ("¿quisiera encontrar esto buscando a una persona/empresa?").
- Con la primera tanda: entreno LightGBM, calibro umbral por tipo (perillas K9 ya existen), `ml_version_modelo` versionado en carpeta. El active learning (banda de revisión) lo mejora con el uso.

**Cronograma realista del T4**: 1-2 días de ingeniería + tus horas de etiquetado + ~1 semana de calibración = **1-2 semanas de calendario**. No bloquea el piloto: la lista blanca v3 ya enruta sola.

## 6. Checklist de arranque en el Mac

- [ ] brew: `uv node git libmagic unar` + Docker Desktop (Resources: 16 GB / 4 CPU)
- [ ] Clonar los 2 repos hermanos; `uv sync --extra workers --extra api`; `cp .env.ejemplo .env`
- [ ] Discos: origen TB (ro) + destino TB (rw) + verificar espacio para expansión de comprimidos
- [ ] Infra Docker `--profile full` → alembic → aplicar-indice
- [ ] Humo: indexar una carpeta chica desde el front (origen + destino elegibles) → buscar contenido
- [ ] Piloto 1-2 TB con 10 workers → llenar la tabla de métricas → BENCHMARKS.md
- [ ] En paralelo: primera tanda de etiquetado T4
