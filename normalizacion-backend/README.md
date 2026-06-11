# normalizacion-backend

Sistema de normalización masiva de datos (~300 TB en discos desechables, miles de millones de archivos): catálogo → **precalificación doble filtro T0–T4** → lectura única (hash + copia al almacén permanente + extracción) → indexado en OpenSearch → verificación → "seguro para desechar".

La documentación de arquitectura y el plan viven en el repo de docs (`Normalizacion masiva de datos/docs/`): `PROPUESTA_ARQUITECTURA.md`, `PLAN_IMPLEMENTACION.md` (+ `.html` visual), `PRECALIFICACION_DOBLE_FILTRO.md`, `DISENO_T0_T4_Y_PIPELINE.md` y `PLAN_DESARROLLO.md`.

## Requisitos

- [uv](https://docs.astral.sh/uv/) (gestiona Python 3.12 y las dependencias)
- Docker (servicios de desarrollo)

## Arranque rápido

```bash
uv sync --group dev                                  # entorno + deps
docker compose -f deploy/docker-compose.dev.yml --profile cola up -d   # solo Postgres
uv run alembic upgrade head                          # crea el esquema de la cola
uv run norm estado                                   # estado de la cola
uv run pytest -m "not integracion"                   # tests unitarios
```

> **Nota (máquinas con poca RAM / WSL capado):** el perfil `cola` levanta solo Postgres. El perfil `full` (OpenSearch + MinIO + Prometheus + Grafana) necesita ≥8 GB en WSL (`.wslconfig`).

## Calidad

Cada PR debe pasar: `ruff check` · `ruff format --check` · `mypy --strict` · `pytest` · allowlist de licencias (sin GPL — riesgo L1). Instala los hooks con `uv run pre-commit install`.

## Estructura

```
src/normalizacion/
├── core/            # CONTRATO COMPARTIDO: config (perillas ⚙K1–K14), modelo, cola, almacén, indexador
├── ingesta/         # catálogo (walker), precalificación (T0–T4), workers (extractores plugin)
├── api/             # FastAPI (Fase 5)
└── cli.py           # norm catalogo|precalifica|worker|estado…
```

## Estado del proyecto

Iteración 0 (Fase 0 — Fundación) en curso. Ver `PLAN_DESARROLLO.md` para las iteraciones hasta M3.
