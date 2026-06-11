# Instalación de principio a fin (macOS)

Guía completa para dejar el sistema funcionando en una Mac de pruebas e iterarlo.

## Opción rápida: TODO en Docker (3 comandos)

La forma más simple de probar con una carpeta REAL de contenido — idéntica en
Mac y Windows (Docker Desktop corre los mismos contenedores Linux):

```bash
git clone <repo>/normalizacion-backend && git clone <repo>/normalizacion-front
cd normalizacion-backend
NORM_CARPETA_DATOS=/Users/tu-usuario/MiCarpetaDeContenido \
  docker compose -f deploy/docker-compose.dev.yml --profile full --profile app up -d --wait --build
```

Abre **http://localhost:8080** → sección Ingesta → "📂 Indexar carpeta…" — tu
carpeta aparece montada como `/datos` (solo lectura, como un disco de origen) y
la navegación queda confinada a ella. Grafana en :3000, MinIO en :9001.

> Los dos repos deben ser carpetas hermanas (el compose construye el front desde
> `../normalizacion-front`). Para iterar código, la opción nativa de abajo
> recompila más rápido.

---

## 1. Prerrequisitos (una sola vez)

```bash
# Homebrew (si no lo tienes): https://brew.sh
brew install uv node git libmagic
# Docker Desktop para Mac: https://docker.com/products/docker-desktop (ábrelo)
```

> `libmagic` es opcional pero recomendado: le da a T1 la capa extra de detección
> (el código lo usa si está; sin él, los detectores estructurales siguen funcionando).

## 2. Clonar y preparar el backend

```bash
git clone <repo>/normalizacion-backend && cd normalizacion-backend
uv sync --group dev --extra workers --extra api    # crea .venv con Python 3.12
cp .env.ejemplo .env                               # ⚙ AQUÍ configuras los DESTINOS
```

**¿Dónde guarda lo procesado?** Tres destinos (en `.env`):

| Destino | Qué contiene | Variable |
|---|---|---|
| **Almacén HOT** | Los archivos ÚTILES, íntegros y deduplicados (clave = sha256) | `NORM_ALMACEN_BACKEND` = `minio` (bucket `NORM_MINIO_BUCKET`) o `local` (`NORM_ALMACEN_LOCAL_RAIZ`) |
| **Almacén frío** | Lo descartado por el filtro — también se copia, reversible | `NORM_MINIO_BUCKET_FRIO` / `NORM_ALMACEN_FRIO_LOCAL_RAIZ` |
| **Índice** | Metadatos + texto buscable (NO archivos) | `NORM_OPENSEARCH_URL` |

El front muestra estos destinos en la sección "Ingesta" (vienen de `GET /pipeline/estado`).

## 3. Levantar los servicios

```bash
docker compose -f deploy/docker-compose.dev.yml --profile full up -d --wait
# Postgres :5432 · OpenSearch :9200 · MinIO :9000 (consola :9001) ·
# Prometheus :9090 · Grafana :3000 (admin / norm, dashboard ya provisionado)

uv run alembic upgrade head     # esquema de la cola
uv run norm aplicar-indice      # template + ISM + índice con alias
```

## 4. Arrancar API + front

```bash
# Terminal 1:
uv run norm api                 # http://localhost:8000 (docs: /docs)

# Terminal 2:
uv run norm exportador          # métricas para Grafana (opcional pero recomendado)

# Terminal 3:
git clone <repo>/normalizacion-front && cd normalizacion-front
npm install && npm run dev      # http://localhost:5173
```

## 5. Usar

En el front (http://localhost:5173) → sección **Ingesta** → **"Indexar carpeta…"**
→ navega y selecciona la carpeta → el pipeline corre completo (catálogo → filtro →
blobs+índice → frío → verificación → puerta) con progreso y métricas por fase.

Equivalente por terminal:

```bash
uv run norm pipeline ~/Documents/carpeta-de-prueba
```

### Carpeta viva (¿puedo seguir metiendo archivos?)

**Sí.** Vuelve a ejecutar (botón "Re-indexar" o el mismo comando): el catálogo es
**incremental e idempotente** — solo lo nuevo/cambiado genera trabajo; nada se
duplica. Recomendación: agrega archivos de forma atómica (escribir fuera y mover
adentro), para no capturar archivos a medio escribir.

## 6. Ver cómo funciona cada fase (logs y estadística)

- **Front → Ingesta → Historial**: cada corrida con duración, archivos/s y
  métricas por fase (hot/cold, dedup, errores, transitorios).
- **Logs estructurados**: cada proceso emite JSON por evento (`fase_completa`,
  `fallo_transitorio`, `zip_bomb…`) — `uv run norm api 2>&1 | tee api.log`.
- **Grafana** (http://localhost:3000): backlog por estado, throughput, % HOT/COLD,
  errores por motivo, puerta de discos. Alertas con runbooks en `deploy/RUNBOOKS.md`.
- **CLI**: `norm estado`, `norm perfil <disco>`, `norm estado-disco <disco>`.

## 7. Operación útil durante la iteración

```bash
uv run norm pausar / reanudar          # detener/continuar sin perder nada
uv run norm reprocesar-errores         # dead-letter de vuelta a su etapa
uv run norm rescore-frio               # re-puntuar el frío tras ajustar el filtro
uv run pytest                          # la suite completa (Postgres+OS arriba)
```

Las **perillas** del filtro (umbrales, guards, pesos) van por entorno:
`NORM_FILTRO__UMBRAL_HOT=70 uv run norm pipeline …` — y cada decisión queda
auditada con su `version_filtro`.
