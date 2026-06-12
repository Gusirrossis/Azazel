"""Tabla `config_overrides`: perillas editadas desde la UI (lista blanca, umbrales…)
que se mergean sobre la config base al iniciar cada corrida — sin reiniciar procesos.
Además, índice (estado, archivo_id) para el keyset del explorador de la cola.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "config_overrides",
        sa.Column("seccion", sa.Text, primary_key=True),  # hoy solo 'filtro'
        sa.Column("valores", JSONB, nullable=False),
        sa.Column(
            "actualizado_en",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Keyset del explorador: WHERE estado = X AND archivo_id > cursor ORDER BY archivo_id
    op.create_index("ix_archivos_estado_id", "archivos", ["estado", "archivo_id"])


def downgrade() -> None:
    op.drop_index("ix_archivos_estado_id", table_name="archivos")
    op.drop_table("config_overrides")
