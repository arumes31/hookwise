"""Add optional per-incident severity filtering for outgoing customer alarms."""

import sqlalchemy as sa
from alembic import op

revision = "b6d8e0f2a4c6"
down_revision = "a8c2d4e6f901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_webhook", sa.Column("severity_path", sa.String(200), nullable=False, server_default=""))
    op.add_column(
        "customer_webhook", sa.Column("minimum_severity", sa.String(20), nullable=False, server_default="high")
    )


def downgrade() -> None:
    # Old workers cannot enforce severity filtering. Disable these subscriptions
    # and cancel queued work instead of silently forwarding every severity.
    op.execute(
        "UPDATE customer_webhook_delivery SET status = 'cancelled', payload = '{}' "
        "WHERE status IN ('pending', 'sending') AND webhook_id IN "
        "(SELECT id FROM customer_webhook WHERE severity_path <> '')"
    )
    op.execute("UPDATE customer_webhook SET is_enabled = false WHERE severity_path <> ''")
    with op.batch_alter_table("customer_webhook") as batch:
        batch.drop_column("minimum_severity")
        batch.drop_column("severity_path")
