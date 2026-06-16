"""Fase 2 — tablas de ENTIDADES canónicas y mapeos aprobados.

`entidades`: la persona resuelta (entidad_id idempotente por ancla). `mapeos_aprobados`:
la asignación columna→campo confirmada por "forma" de dataset (huella de columnas),
reutilizable automáticamente en datasets con la misma forma.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entidades",
        sa.Column("entidad_id", sa.Text, primary_key=True),
        sa.Column("tipo", sa.Text, nullable=False, server_default="persona"),
        sa.Column("ancla_tipo", sa.Text, nullable=False),  # curp/rfc/email/telefono
        sa.Column("ancla_valor", sa.Text, nullable=False),
        sa.Column("campos", JSONB, nullable=False, server_default="{}"),
        sa.Column("confianza", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("version_receta", sa.Text, nullable=False),
        sa.Column("version_resolucion", sa.Text, nullable=False),
        # soft-delete (LFPDPPP): nunca se borra, se marca inactiva
        sa.Column("activo", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("procedencias", JSONB, nullable=False, server_default="[]"),
        sa.Column("creado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("actualizado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    # Búsqueda por tipo + ancla (resolución exacta) y exploración paginada.
    op.create_index("ix_entidades_tipo", "entidades", ["tipo"])
    op.create_index("ix_entidades_ancla", "entidades", ["ancla_tipo", "ancla_valor"])
    # Búsqueda exacta por CURP desde la API/UI sin escanear toda la tabla
    # (a escala de millones de personas, un full scan sobre JSONB es inviable).
    op.create_index("ix_entidades_curp", "entidades", [sa.text("(campos->>'curp')")])

    op.create_table(
        "mapeos_aprobados",
        # huella = sha256 de los nombres de columna ordenados (la "forma" del dataset)
        sa.Column("huella", sa.Text, primary_key=True),
        sa.Column("tipo_entidad", sa.Text, nullable=False),
        sa.Column("asignacion", JSONB, nullable=False),  # {columna: campo_receta}
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("creado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("mapeos_aprobados")
    op.drop_index("ix_entidades_curp", table_name="entidades")
    op.drop_index("ix_entidades_ancla", table_name="entidades")
    op.drop_index("ix_entidades_tipo", table_name="entidades")
    op.drop_table("entidades")
