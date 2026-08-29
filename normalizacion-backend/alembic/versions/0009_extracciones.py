"""Caché de extracción por CONTENIDO — y la base del reproceso dirigido.

El almacén ya deduplica blobs por `hash_contenido`, pero la EXTRACCIÓN corría igual
para cada copia: dos rutas con el mismo archivo pagaban dos veces el OCR. En el corpus
actual eso no es un detalle — 39 312 archivos contra 19 656 hashes únicos, exactamente
el doble: la mitad del trabajo de reconocimiento era por información ya conocida.

La clave es el hash del CONTENIDO, no `archivo_id` (que incluye ruta y mtime): la misma
credencial escaneada, copiada en cuatro carpetas, se lee una vez.

Y como la fila guarda con qué VERSIÓN de extractor se produjo, sirve para lo otro que
faltaba: subir la versión invalida lo viejo y `norm reextraer` sabe exactamente qué
rehacer, leyendo los bytes del almacén sin necesitar el disco original.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extracciones",
        # sha256 del contenido: la MISMA clave del almacén content-addressed.
        sa.Column("hash_contenido", sa.Text, primary_key=True),
        sa.Column("tipo_real", sa.Text, nullable=True),
        # Versión del extractor que produjo esto. Al subirla, las filas viejas dejan
        # de servir como caché y se convierten en la lista de trabajo del reproceso.
        sa.Column("version_extractor", sa.Text, nullable=False),
        # Quién lo produjo: 'nativo' (texto del propio formato) | 'ocr' | 'mixto'.
        # Separa "este PDF no tenía texto" de "el OCR no supo leerlo".
        sa.Column("motor", sa.Text, nullable=False, server_default="nativo"),
        sa.Column("texto", sa.Text, nullable=True),
        sa.Column("campos", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("perfil_calidad", JSONB, nullable=True),
        sa.Column("flags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        # Confianza media del OCR (0-100). NULL cuando no hubo OCR.
        sa.Column("confianza", sa.Float, nullable=True),
        sa.Column("chars", sa.Integer, nullable=False, server_default="0"),
        # Cuánto costó producirlo: sin esto no se puede decir si una mejora de calidad
        # salió cara o barata, y toda la Fase 0 se queda sin la mitad de su tabla.
        sa.Column("ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("creado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # Cuántas veces se reusó. Mide el ahorro real en producción, no en teoría.
        sa.Column("reusos", sa.Integer, nullable=False, server_default="0"),
    )
    # La consulta del reproceso: "lo peor primero, de esta versión, de este tipo".
    op.create_index(
        "ix_extracciones_reproceso",
        "extracciones",
        ["version_extractor", "confianza"],
    )
    op.create_index("ix_extracciones_motor", "extracciones", ["motor", "tipo_real"])


def downgrade() -> None:
    op.drop_index("ix_extracciones_motor", table_name="extracciones")
    op.drop_index("ix_extracciones_reproceso", table_name="extracciones")
    op.drop_table("extracciones")
