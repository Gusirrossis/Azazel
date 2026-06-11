# normalizacion-front

Buscador web del sistema de normalización masiva: consume la API del backend
(`normalizacion-backend`, contrato OpenAPI vía `norm openapi`).

## Qué hace

- **Búsqueda** por nombre (wildcard) con autocompletar y filtros (tipo real,
  extensión, disco, puntaje del filtro), **facetas** clicables y **paginación
  profunda** (search_after — botón "Cargar más").
- **Detalle** de cada archivo: tipo real, señales del filtro, campos extraídos,
  perfil de calidad, texto extraído, avisos (extensión mentirosa, límites).
- **Descarga del original** desde el almacén permanente por hash.
- API key opcional (header `X-API-Key`, guardada en localStorage).

## Correr en dev

```bash
# 1) el backend arriba (en normalizacion-backend):
#    docker compose -f deploy/docker-compose.dev.yml --profile full up -d
#    uv run norm api          # puerto 8000

# 2) este front:
npm install
npm run dev                   # http://localhost:5173 (proxy /api → :8000)
```

## Build de producción

```bash
npm run build                 # tsc + vite → dist/
```

En producción, el reverse proxy debe enrutar `/api/*` a la API (o configurar
`api_cors_origenes` en el backend para el dominio del front).

## Stack

React 18 + TypeScript estricto + Vite. Sin librerías de UI: CSS propio con la
paleta de los documentos de arquitectura del proyecto.
