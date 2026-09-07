# Azazel

Sistema de normalización e indexación masiva de archivos: cataloga, filtra
(caliente/frío), deduplica por hash, explora contenedores (zip/7z/rar/tar) e
indexa el contenido para búsqueda.

- **`normalizacion-backend/`** — API y pipeline (Python · FastAPI · Postgres + OpenSearch).
- **`normalizacion-front/`** — interfaz web (React + Vite).

## Por dónde empezar a leer

| Documento | Para qué | Caduca |
|---|---|---|
| [`docs/ESTADO.md`](docs/ESTADO.md) | **Cuántas filas hay, qué corre, qué falta.** Cifras medidas con su fecha | sí — vuelve a medir |
| [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) | Cómo está construido: pipeline, filtro, almacén, índice, auth | no |
| [`docs/OPERACION_VPS.md`](docs/OPERACION_VPS.md) | Operar el servidor: desplegar, respaldar, entrar al panel | parcialmente |
| [`docs/PROCESAMIENTO_Y_OCR.md`](docs/PROCESAMIENTO_Y_OCR.md) | Cómo se extrae texto de PDFs e imágenes | no |
| `docs/PLAN_*.md` | Planes fechados: el **porqué** de cada decisión | son históricos |

Los `PLAN_*` se conservan aunque estén ejecutados porque explican decisiones que el
código no puede contar. Los que ya se hicieron lo dicen en su cabecera.

## Puesta en marcha (macOS, nativo)

Ver **`normalizacion-backend/COMANDOS_MAC.md`** para los pasos completos. En resumen:

```bash
brew install uv node git libmagic unar sevenzip postgresql@16 opensearch
brew services start postgresql@16 opensearch

cd normalizacion-backend
uv sync --extra workers --extra api
cp .env.ejemplo .env          # ajusta conexiones y destino del almacén
uv run alembic upgrade head
uv run norm aplicar-indice
uv run norm api               # API en http://localhost:8000

cd ../normalizacion-front
npm install
npm run dev                   # interfaz en http://localhost:5173
```
