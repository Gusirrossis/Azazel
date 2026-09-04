"""Que las entidades se puedan BUSCAR, no solo listar.

`listar_entidades` filtra por nombre con `campos->>'nombre_completo' ILIKE '%texto%'`
y por CURP con `campos->>'curp' = ...`. Ninguna de las dos tenía índice: cada
consulta era un escaneo completo de la tabla. Da igual con la tabla vacía; deja de
darlo en cuanto el backfill la puebla, y sobre todo cuando quien pregunta es un
consumidor externo que llama una vez por búsqueda de usuario.

`pg_trgm` es lo que hace que un `ILIKE '%…%'` pueda usar índice: descompone el texto
en trigramas y busca por ellos. Es el mismo problema que el comodín inicial de
OpenSearch, con la misma solución conceptual.

Los índices son PARCIALES (`WHERE activo`): una entidad desactivada no se busca, y
excluirlas del índice lo hace más pequeño y más rápido.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # La extensión la crea el superusuario; en un Postgres gestionado donde no se
    # pueda, la migración no debe tumbar el arranque de la API: sin trigramas la
    # búsqueda por nombre sigue funcionando, solo que lenta.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entidades_nombre_trgm ON entidades"
        " USING gin ((campos->>'nombre_completo') gin_trgm_ops) WHERE activo"
    )
    # Exactos y baratos: buscar por una CURP concreta es el camino más frecuente
    # desde una federación, y es el que tiene que responder en microsegundos.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entidades_curp ON entidades"
        " ((campos->>'curp')) WHERE activo"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entidades_rfc ON entidades"
        " ((campos->>'rfc')) WHERE activo"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entidades_rfc")
    op.execute("DROP INDEX IF EXISTS ix_entidades_curp")
    op.execute("DROP INDEX IF EXISTS ix_entidades_nombre_trgm")
    # La extensión NO se borra: puede haberla creado o estarla usando otra cosa.
