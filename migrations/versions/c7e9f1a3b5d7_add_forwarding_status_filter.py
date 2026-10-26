"""Add optional open-incident status filtering for outgoing customer alarms."""

import sqlalchemy as sa
from alembic import op

revision = "c7e9f1a3b5d7"
down_revision = "b6d8e0f2a4c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_webhook", sa.Column("status_path", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    # Older workers cannot enforce open-incident filtering. Cancel queued data
    # and pause these connectors instead of silently forwarding closed incidents.
    op.execute(
        "UPDATE customer_webhook_delivery SET status = 'cancelled', payload = '{}' "
        "WHERE status IN ('pending', 'sending') AND webhook_id IN "
        "(SELECT id FROM customer_webhook WHERE status_path <> '')"
    )
    op.execute("UPDATE customer_webhook SET is_enabled = false WHERE status_path <> ''")
    with op.batch_alter_table("customer_webhook") as batch:
        batch.drop_column("status_path")
