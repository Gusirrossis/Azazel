"""Login real — usuarios con rol y sesiones del lado del servidor.

Hasta aquí la API key ERA la identidad: un secreto compartido, sin nombre, sin
caducidad y sin forma de saber quién hizo qué. Esto separa las dos cosas:

  * `usuarios`  — personas que entran al panel (usuario + contraseña + rol).
  * `sesiones`  — cada inicio de sesión, revocable de una en una.

De la contraseña se guarda su hash **argon2id**; del token de sesión, su **sha256**.
Ni la una ni el otro se pueden recuperar desde la BD, solo verificar o revocar.
Las API keys con nombre siguen existiendo aparte: son para consumidores MÁQUINA
(reddoor, el AEB), que no tienen ni navegador ni cookies.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usuarios",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # El identificador con el que se entra. `citext` sería más limpio, pero exige
        # una extensión: se normaliza a minúsculas en la capa de aplicación.
        sa.Column("usuario", sa.Text, nullable=False, unique=True),
        sa.Column("nombre", sa.Text, nullable=False, server_default=""),
        sa.Column("hash_contrasena", sa.Text, nullable=False),
        # 'lector' | 'operador' | 'admin' — ver normalizacion.api.roles
        sa.Column("rol", sa.Text, nullable=False, server_default="lector"),
        # Desactivar en vez de borrar: preserva la traza de quién hizo qué.
        sa.Column("activo", sa.Boolean, nullable=False, server_default=sa.text("true")),
        # Alta por un admin o contraseña reseteada: obliga a cambiarla al entrar.
        sa.Column("debe_cambiar", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("creado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("actualizado_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("ultimo_acceso", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_usuarios_rol", "usuarios", "rol IN ('lector', 'operador', 'admin')"
    )

    op.create_table(
        "sesiones",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Al borrar de verdad un usuario se van sus sesiones: nada sobrevive huérfano.
        sa.Column("usuario_id", sa.BigInteger,
                  sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        # sha256 del token que viaja en la cookie. El token NO se guarda.
        sa.Column("hash_token", sa.Text, nullable=False, unique=True),
        sa.Column("creada_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expira_en", sa.TIMESTAMP(timezone=True), nullable=False),
        # Renovación deslizante: se refresca al usarla, para caducar por inactividad.
        sa.Column("vista_en", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # Para que el usuario reconozca sus sesiones al listarlas ("¿esta cuál es?").
        sa.Column("ip", sa.Text, nullable=False, server_default=""),
        sa.Column("agente", sa.Text, nullable=False, server_default=""),
        sa.Column("revocada_en", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # El camino caliente: una lectura por request autenticado.
    op.create_index("ix_sesiones_hash_token", "sesiones", ["hash_token"])
    # Listar "mis sesiones activas" y el barrido de expiradas.
    op.create_index("ix_sesiones_usuario", "sesiones", ["usuario_id", "expira_en"])


def downgrade() -> None:
    op.drop_index("ix_sesiones_usuario", table_name="sesiones")
    op.drop_index("ix_sesiones_hash_token", table_name="sesiones")
    op.drop_table("sesiones")
    op.drop_constraint("ck_usuarios_rol", "usuarios", type_="check")
    op.drop_table("usuarios")
