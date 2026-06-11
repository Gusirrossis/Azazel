"""Columna `destino` en corridas: carpeta de destino elegida desde el front.

NULL = destino por defecto de la configuración (MinIO o carpeta local del .env).
Con valor = el almacén HOT y el frío de ESA corrida vivieron bajo esa carpeta.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corridas", sa.Column("destino", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("corridas", "destino")
