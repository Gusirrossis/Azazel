# Imagen del backend: la MISMA que correrá en Mac (Docker Desktop = contenedores Linux).
# Incluye libmagic → T1 con su capa extra de detección activa, igual que en producción.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# libmagic → capa extra de T1.
#
# Los RAR necesitan DOS binarios y sólo estaba uno: se LISTAN con 7-Zip (`7zz`) y
# se EXTRAEN con `unar` (ver `contenedores.py`). Sin `7zz`, `_7zz_bin()` lanza
# FileNotFoundError y todo RAR acaba preservado íntegro sin explorar
# (`formato_no_soportado`): no se pierde nada, pero su contenido nunca se indexa y
# el motivo real —falta una herramienta— queda enterrado en una bandera.
# En Debian bookworm el paquete `7zip` provee `7zz`.
#
# tesseract-ocr + sus idiomas: SIN el binario, `pytesseract` levanta
# TesseractNotFoundError, `_ocr.py` lo traduce a la bandera `ocr_no_disponible` y
# TODO el pipeline de OCR queda en un no-op silencioso — los PDFs escaneados se
# indexan sin una línea de texto y nadie se entera. El extra `ocr` de Python va con
# ellos: hacen falta los dos o no sirve ninguno.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libmagic1 unar 7zip tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Capa de dependencias (cacheable): solo lock + manifest
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra workers --extra api --extra ocr

# El código y lo que el runtime necesita (migraciones, mappings, ISM)
COPY README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY deploy/mappings ./deploy/mappings
COPY deploy/ism ./deploy/ism
RUN uv sync --frozen --no-dev --extra workers --extra api --extra ocr

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["norm", "api", "--host", "0.0.0.0", "--puerto", "8000"]
