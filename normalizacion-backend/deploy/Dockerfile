# Imagen del backend: la MISMA que correrá en Mac (Docker Desktop = contenedores Linux).
# Incluye libmagic → T1 con su capa extra de detección activa, igual que en producción.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# libmagic → capa extra de T1.
#
# RAR: se LISTA y se EXTRAE con `unrar`, el oficial de RARLab (ver `contenedores.py`).
# Con `unar` 49 de las 1133 entradas de 'Matrix.rar' salieron a 0 B o truncadas, y
# `lsar` leyó mal el tamaño de la de 21,4 GB. El archivo estaba sano: `unrar` extrae
# las 49 con el CRC correcto. `unrar` vive en el componente non-free de Debian: es
# freeware, y su licencia permite usarlo y redistribuirlo para EXTRAER. Lo que prohíbe
# es usar su código para crear un compresor RAR. Bookworm declara sus fuentes en
# `debian.sources` (formato deb822); el `grep` hace fallar el build si la imagen base
# cambia de formato, en vez de seguir sin non-free. El `sed` lo añade también a
# bookworm-security, para que lleguen sus parches (unrar ha tenido CVE de rutas).
# Probado sobre `normalizacion-api` en la matriz: instala `unrar` 1:6.2.6-1+deb12u1
# (UNRAR 6.21).
#
# `unar`/`lsar` se quedan: son el respaldo de RAR si falta `unrar` y el de 7z con
# filtros que py7zr no trae. `7zz` (paquete `7zip`) extrae los 7z en streaming, sin
# cargar el bloque sólido en RAM. Ese paquete NO trae el códec RAR: `7zz l` sobre un
# RAR sale con rc=2.
#
# tesseract-ocr + sus idiomas: SIN el binario, `pytesseract` levanta
# TesseractNotFoundError, `_ocr.py` lo traduce a la bandera `ocr_no_disponible` y
# TODO el pipeline de OCR queda en un no-op silencioso — los PDFs escaneados se
# indexan sin una línea de texto y nadie se entera. El extra `ocr` de Python va con
# ellos: hacen falta los dos o no sirve ninguno.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && grep -q '^Components: main non-free$' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libmagic1 unrar unar 7zip tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
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
