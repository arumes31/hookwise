"""Add customer alarm subscriptions and durable outgoing deliveries."""

import sqlalchemy as sa
from alembic import op

revision = "c9a7b2e4d6f8"
down_revision = "b4e2c7a9d1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_webhook",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("config_id", sa.String(64), sa.ForeignKey("webhook_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.String(50), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("score_path", sa.String(200), nullable=False),
        sa.Column("minimum_score", sa.Float(), nullable=False),
        sa.Column("bearer_token", sa.Text()),
        sa.Column("signing_secret", sa.Text()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_customer_webhook_company_id", "customer_webhook", ["company_id"])
    op.create_table(
        "customer_webhook_delivery",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "webhook_id", sa.String(36), sa.ForeignKey("customer_webhook.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("log_id", sa.String(36), sa.ForeignKey("webhook_log.id", ondelete="SET NULL")),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("response_status", sa.Integer()),
        sa.Column("error_message", sa.String(255)),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("webhook_id", "event_key", name="uq_customer_webhook_event"),
    )
    op.create_index("ix_customer_delivery_due", "customer_webhook_delivery", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("customer_webhook_delivery")
    op.drop_table("customer_webhook")
