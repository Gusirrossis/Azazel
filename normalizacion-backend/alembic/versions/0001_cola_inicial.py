"""Cola durable inicial: tablas `discos` y `archivos` (system-of-record).

Incluye desde el día 0 los campos de la precalificación (Fase 1.5):
puntaje, ruta_decision, tipo_real, senales, motivo, version_filtro, origen_contenedor.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ESTADOS = (
    "PENDIENTE",
    "PRECALIFICADO",
    "COLD",
    "EN_PROCESO",
    "INDEXADO",
    "VERIFICADO",
    "HECHO",
    "ERROR",
)


def upgrade() -> None:
    op.create_table(
        "discos",
        sa.Column("disco_id", sa.Text, primary_key=True),
        sa.Column("etiqueta", sa.Text, nullable=True),
        sa.Column("punto_montaje", sa.Text, nullable=True),
        sa.Column("total_catalogado", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "seguro_para_desechar", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "creado_en",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "actualizado_en",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "archivos",
        # Identidad de TRABAJO: sha256(ruta+tamaño+mtime) — barata, determinista
        sa.Column("archivo_id", sa.Text, primary_key=True),
        sa.Column(
            "disco_id",
            sa.Text,
            sa.ForeignKey("discos.disco_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ruta", sa.Text, nullable=False),
        sa.Column("nombre", sa.Text, nullable=False),
        sa.Column("extension", sa.Text, nullable=True),
        sa.Column("tamano", sa.BigInteger, nullable=False),
        sa.Column("mtime", sa.TIMESTAMP(timezone=True), nullable=False),
        # Máquina de estados (plano de control)
        sa.Column("estado", sa.Text, nullable=False, server_default="PENDIENTE"),
        sa.Column("prioridad", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("intentos", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("error_motivo", sa.Text, nullable=True),
        sa.Column("worker_id", sa.Text, nullable=True),
        sa.Column("lease_hasta", sa.TIMESTAMP(timezone=True), nullable=True),
        # Precalificación (Fase 1.5) — cada decisión queda auditable
        sa.Column("puntaje", sa.SmallInteger, nullable=True),
        sa.Column("ruta_decision", sa.Text, nullable=True),
        sa.Column("tipo_real", sa.Text, nullable=True),
        sa.Column("senales", JSONB, nullable=True),
        sa.Column("motivo", sa.Text, nullable=True),
        sa.Column("version_filtro", sa.Text, nullable=True),
        sa.Column("origen_contenedor", JSONB, nullable=True),
        # Persistencia: identidad de ALMACÉN = sha256(bytes) — clave del dedup
        sa.Column("hash_contenido", sa.Text, nullable=True),
        sa.Column(
            "creado_en",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "actualizado_en",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("estado IN " + repr(ESTADOS), name="estado_valido"),
        sa.CheckConstraint(
            "ruta_decision IS NULL OR ruta_decision IN ('HOT', 'COLD')", name="ruta_valida"
        ),
        sa.CheckConstraint(
            "puntaje IS NULL OR (puntaje >= 0 AND puntaje <= 100)", name="puntaje_rango"
        ),
    )

    # Claim eficiente: los workers buscan por estado ordenando por prioridad
    op.create_index("ix_archivos_claim", "archivos", ["estado", "prioridad", "archivo_id"])
    op.create_index("ix_archivos_disco_estado", "archivos", ["disco_id", "estado"])
    op.create_index("ix_archivos_hash_contenido", "archivos", ["hash_contenido"])
    op.create_index("ix_archivos_lease", "archivos", ["lease_hasta"])


def downgrade() -> None:
    op.drop_table("archivos")
    op.drop_table("discos")
