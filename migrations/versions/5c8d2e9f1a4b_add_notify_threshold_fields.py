"""Nr. 18: per-endpoint failure notification threshold

Revision ID: 5c8d2e9f1a4b
Revises: 9a4b7c1d2e3f
"""
import sqlalchemy as sa
from alembic import op

revision = "5c8d2e9f1a4b"
down_revision = "9a4b7c1d2e3f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webhook_config", sa.Column("notify_failure_threshold", sa.Integer(), nullable=True))
    op.add_column(
        "webhook_config",
        sa.Column("notify_window_minutes", sa.Integer(), nullable=False, server_default="60"),
    )
    op.add_column("webhook_config", sa.Column("last_threshold_alert_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("webhook_config", "last_threshold_alert_at")
    op.drop_column("webhook_config", "notify_window_minutes")
    op.drop_column("webhook_config", "notify_failure_threshold")
