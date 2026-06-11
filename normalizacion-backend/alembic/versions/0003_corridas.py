"""Tabla `corridas`: historial de ejecuciones del pipeline con métricas POR FASE.

Cada corrida guarda qué carpeta se procesó, cuánto duró cada fase y sus números
(procesados, hot/cold, dedup, errores, transitorios) — la base para ver cómo
funciona cada etapa y dónde hay margen de mejora.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corridas",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("disco_id", sa.Text, nullable=False),
        sa.Column("ruta", sa.Text, nullable=False),
        sa.Column("estado", sa.Text, nullable=False, server_default="EN_CURSO"),
        sa.Column("fase_actual", sa.Text, nullable=True),
        sa.Column("fases", JSONB, nullable=False, server_default="[]"),
        sa.Column("seguro_para_desechar", sa.Boolean, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "iniciada_en",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("terminada_en", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "estado IN ('EN_CURSO', 'COMPLETADA', 'FALLIDA')", name="estado_corrida_valido"
        ),
    )
    op.create_index("ix_corridas_estado", "corridas", ["estado"])


def downgrade() -> None:
    op.drop_table("corridas")
