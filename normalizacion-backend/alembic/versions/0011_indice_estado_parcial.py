"""Cambiar ix_archivos_estado_id por un índice PARCIAL que excluya PENDIENTE.

`ix_archivos_estado_id (estado, archivo_id)` existe para la paginación keyset del
listado de la cola (`ingesta.pipeline.listar_archivos_cola`). Pesa 3.795 MB sobre
28,8 millones de filas y, medido en producción, **el planificador ni lo mira para el
estado que domina la tabla**:

    PENDIENTE, primera página → Index Scan using archivos_pkey
                                Filter: estado = 'PENDIENTE'
                                Rows Removed by Filter: 17   (para devolver 50)

Y es lo correcto: el 97,19% de las filas son PENDIENTE, así que el filtro no
discrimina nada y recorrer la clave primaria encuentra 50 coincidencias casi
inmediatamente. El índice solo aporta de verdad en los estados selectivos:

    PENDIENTE  28.017.883   97,19%   ← la pkey basta
    ERROR         411.293    1,43%
    INDEXADO      362.702    1,26%
    HECHO          27.309    0,09%
    COLD           10.060    0,03%

Un índice parcial sobre ese 2,81% ocupa ~107 MB en vez de 3.795 MB. Los estados
selectivos siguen resolviéndose por índice, y PENDIENTE cae a la clave primaria, que
es lo que el planificador ya elegía por su cuenta.

CONCURRENTLY y en este orden —crear el nuevo, comprobar, borrar el viejo— porque un
`CREATE INDEX` normal toma un lock que bloquea escrituras, y sobre 28,8 millones de
filas eso son minutos con la ingesta parada. Por lo mismo va en `autocommit_block`:
CONCURRENTLY no puede correr dentro de una transacción.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_NUEVO = "ix_archivos_estado_id_sel"
_VIEJO = "ix_archivos_estado_id"
#: El estado que domina la tabla y que la clave primaria ya resuelve.
_EXCLUIDO = "PENDIENTE"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NUEVO} ON archivos"
            f" (estado, archivo_id) WHERE estado <> '{_EXCLUIDO}'"
        )
        # Solo después de que el sustituto EXISTE. Al revés dejaría una ventana en la
        # que el listado de la cola no tiene índice ninguno sobre 28,8 M de filas.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_VIEJO}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_VIEJO} ON archivos"
            " (estado, archivo_id)"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NUEVO}")
