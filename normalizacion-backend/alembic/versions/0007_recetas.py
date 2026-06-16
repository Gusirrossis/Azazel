"""Fase 2 — recetas DINÁMICAS (proyección y resolución) como datos editables.

Una receta de PROYECCIÓN transforma la persona canónica (estable) al esquema que
pide cada sistema consumidor (otros nombres, otra anidación, otros valores).
Añadir un sistema = otra receta (fila), sin tocar código. Versionada y reversible.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recetas",
        # clave estable, p. ej. "fz1" o "sistema_plano"
        sa.Column("clave", sa.Text, primary_key=True),
        sa.Column("clase", sa.Text, nullable=False),  # 'proyeccion' | 'resolucion'
        sa.Column("tipo", sa.Text, nullable=False, server_default="persona"),
        sa.Column("nombre", sa.Text, nullable=False),
        sa.Column("descripcion", sa.Text, nullable=False, server_default=""),
        sa.Column("definicion", JSONB, nullable=False),
        sa.Column("version", sa.Text, nullable=False, server_default="v1"),
        sa.Column("activa", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("editable", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("creado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("actualizado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_recetas_clase", "recetas", ["clase", "tipo"])


def downgrade() -> None:
    op.drop_index("ix_recetas_clase", table_name="recetas")
    op.drop_table("recetas")
