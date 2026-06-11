# Azazel

Sistema de normalización e indexación masiva de archivos: cataloga, filtra
(caliente/frío), deduplica por hash, explora contenedores (zip/7z/rar/tar) e
indexa el contenido para búsqueda.

- **`normalizacion-backend/`** — API y pipeline (Python · FastAPI · Postgres + OpenSearch).
- **`normalizacion-front/`** — interfaz web (React + Vite).

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
