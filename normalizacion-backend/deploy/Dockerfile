# Imagen del backend: la MISMA que correrá en Mac (Docker Desktop = contenedores Linux).
# Incluye libmagic → T1 con su capa extra de detección activa, igual que en producción.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# libmagic → capa extra de T1; unar → backend de rarfile (exploración de RAR)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 unar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Capa de dependencias (cacheable): solo lock + manifest
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra workers --extra api

# El código y lo que el runtime necesita (migraciones, mappings, ISM)
COPY README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY deploy/mappings ./deploy/mappings
COPY deploy/ism ./deploy/ism
RUN uv sync --frozen --no-dev --extra workers --extra api

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["norm", "api", "--host", "0.0.0.0", "--puerto", "8000"]
