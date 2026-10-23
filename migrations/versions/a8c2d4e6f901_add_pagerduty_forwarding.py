"""Add an explicit PagerDuty Events v2 forwarding format and encrypted key."""

import sqlalchemy as sa
from alembic import op

revision = "a8c2d4e6f901"
down_revision = "f2a8c4d6e9b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_webhook", sa.Column("payload_format", sa.String(20), nullable=False, server_default="hookwise")
    )
    op.add_column("customer_webhook", sa.Column("pagerduty_routing_key", sa.Text(), nullable=True))


def downgrade() -> None:
    # The old worker cannot encode PagerDuty requests. Cancel these before rollback.
    op.execute(
        "UPDATE customer_webhook_delivery SET status = 'cancelled', payload = '{}' "
        "WHERE status IN ('pending', 'sending') AND webhook_id IN "
        "(SELECT id FROM customer_webhook WHERE payload_format = 'pagerduty')"
    )
    op.execute("UPDATE customer_webhook SET is_enabled = false WHERE payload_format = 'pagerduty'")
    with op.batch_alter_table("customer_webhook") as batch:
        batch.drop_column("pagerduty_routing_key")
        batch.drop_column("payload_format")
