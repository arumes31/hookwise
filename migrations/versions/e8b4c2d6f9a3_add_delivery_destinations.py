"""Add independent endpoint delivery controls, enabled for existing endpoints."""

import sqlalchemy as sa
from alembic import op

revision = "e8b4c2d6f9a3"
down_revision = "d7f3a9b2c5e1"
branch_labels = None
depends_on = None


def upgrade():
    for field in ("connectwise_delivery_enabled", "forwarding_enabled"):
        op.add_column("webhook_config", sa.Column(field, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table("webhook_config") as batch:
        batch.drop_column("forwarding_enabled")
        batch.drop_column("connectwise_delivery_enabled")
