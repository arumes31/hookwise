"""Add Entra app-role state and manual authorization overrides.

Revision ID: d9f1a7c4e2b6
Revises: 6b4e2d1f9a7c
Create Date: 2026-09-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "d9f1a7c4e2b6"
down_revision = "6b4e2d1f9a7c"
branch_labels = None
depends_on = None

ENTRA_NO_PERMISSIONS_ROLE = "none"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("user")}
    indexes = {index["name"] for index in inspector.get_indexes("user")}
    with op.batch_alter_table("user") as batch_op:
        if "entra_role" not in columns:
            batch_op.add_column(sa.Column("entra_role", sa.String(length=50), nullable=True))
        if "entra_role_synced_at" not in columns:
            batch_op.add_column(sa.Column("entra_role_synced_at", sa.DateTime(), nullable=True))
        if "is_override_active" not in columns:
            batch_op.add_column(
                sa.Column(
                    "is_override_active",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "override_role" not in columns:
            batch_op.add_column(sa.Column("override_role", sa.String(length=50), nullable=True))
        if "ix_user_entra_role" not in indexes:
            batch_op.create_index("ix_user_entra_role", ["entra_role"], unique=False)
        if "ix_user_override_role" not in indexes:
            batch_op.create_index("ix_user_override_role", ["override_role"], unique=False)

    if "auth_source" in columns:
        bind.execute(
            sa.text("UPDATE \"user\" SET entra_role = :role WHERE LOWER(auth_source) = 'entra' AND entra_role IS NULL"),
            {"role": ENTRA_NO_PERMISSIONS_ROLE},
        )

    if "rbac_meta" in inspector.get_table_names():
        ergebnis = bind.execute(sa.text("UPDATE rbac_meta SET permissions_epoch = permissions_epoch + 1 WHERE id = 1"))
        if ergebnis.rowcount == 0:
            bind.execute(sa.text("INSERT INTO rbac_meta (id, permissions_epoch) VALUES (1, 2)"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("user")}
    indexes = {index["name"] for index in inspector.get_indexes("user")}
    with op.batch_alter_table("user") as batch_op:
        if "ix_user_override_role" in indexes:
            batch_op.drop_index("ix_user_override_role")
        if "ix_user_entra_role" in indexes:
            batch_op.drop_index("ix_user_entra_role")
        if "override_role" in columns:
            batch_op.drop_column("override_role")
        if "is_override_active" in columns:
            batch_op.drop_column("is_override_active")
        if "entra_role_synced_at" in columns:
            batch_op.drop_column("entra_role_synced_at")
        if "entra_role" in columns:
            batch_op.drop_column("entra_role")
