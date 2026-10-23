"""Bind queued forwarding deliveries to their destination generation."""

import sqlalchemy as sa
from alembic import op

revision = "d7f3a9b2c5e1"
down_revision = "c9a7b2e4d6f8"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("customer_webhook", "customer_webhook_delivery"):
        op.add_column(table, sa.Column("destination_generation", sa.Integer(), nullable=False, server_default="1"))
    op.execute(
        "UPDATE customer_webhook_delivery SET payload = '{}' WHERE status IN ('delivered', 'failed', 'cancelled')"
    )


def downgrade():
    for table in ("customer_webhook_delivery", "customer_webhook"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("destination_generation")
