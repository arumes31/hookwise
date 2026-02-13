"""Final large migration for all UX improvements

Revision ID: d3e4f5g6h7i8
Revises: c2d3e4f5g6h7
Create Date: 2026-02-11 23:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d3e4f5g6h7i8"
down_revision = "c2d3e4f5g6h7"
branch_labels = None
depends_on = None


def upgrade():
    # Check if table already exists (in case db.create_all() ran)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # User table (new)
    if "user" not in tables:
        op.create_table(
            "user",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("username", sa.String(length=100), nullable=False),
            sa.Column("password_hash", sa.String(length=256), nullable=False),
            sa.Column("otp_secret", sa.String(length=32), nullable=True),
            sa.Column("is_2fa_enabled", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("role", sa.String(length=20), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username"),
        )

    # WebhookConfig updates
    op.add_column("webhook_config", sa.Column("description_template", sa.Text(), nullable=True))
    op.add_column("webhook_config", sa.Column("hmac_secret", sa.String(length=256), nullable=True))
    op.add_column("webhook_config", sa.Column("is_draft", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("webhook_config", sa.Column("display_order", sa.Integer(), nullable=True, server_default="0"))

    # WebhookLog updates
    op.add_column("webhook_log", sa.Column("headers", sa.Text(), nullable=True))
    op.add_column("webhook_log", sa.Column("matched_rule", sa.Text(), nullable=True))
    op.add_column("webhook_log", sa.Column("processing_time", sa.Float(), nullable=True))
    op.add_column("webhook_log", sa.Column("source_ip", sa.String(length=50), nullable=True))
    op.add_column("webhook_log", sa.Column("retry_count", sa.Integer(), nullable=True, server_default="0"))


def downgrade():
    op.drop_column("webhook_log", "retry_count")
    op.drop_column("webhook_log", "source_ip")
    op.drop_column("webhook_log", "processing_time")
    op.drop_column("webhook_log", "matched_rule")
    op.drop_column("webhook_log", "headers")
    op.drop_column("webhook_config", "display_order")
    op.drop_column("webhook_config", "is_draft")
    op.drop_column("webhook_config", "hmac_secret")
    op.drop_column("webhook_config", "description_template")
    op.drop_table("user")
