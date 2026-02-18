"""Increase ID column lengths

Revision ID: f3a1d2c6e5b4
Revises: e5f6a7b8c9d0
Create Date: 2026-02-18 12:35:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f3a1d2c6e5b4"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    # Increase lengths for config_id and id across tables
    # Note: SQLite doesn't strictly enforce varchar lengths, but Postgres does.
    # Alembic handles alter_column differently per dialect.

    with op.batch_alter_table("webhook_config") as batch_op:
        batch_op.alter_column("id", type_=sa.String(length=64))

    with op.batch_alter_table("webhook_log") as batch_op:
        batch_op.alter_column("config_id", type_=sa.String(length=64))

    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column("config_id", type_=sa.String(length=64))


def downgrade():
    with op.batch_alter_table("audit_log") as batch_op:
        batch_op.alter_column("config_id", type_=sa.String(length=36))

    with op.batch_alter_table("webhook_log") as batch_op:
        batch_op.alter_column("config_id", type_=sa.String(length=36))

    with op.batch_alter_table("webhook_config") as batch_op:
        batch_op.alter_column("id", type_=sa.String(length=36))
