"""Add history-filter indexes (source_ip + composite config/status/created_at)

Revision ID: c4e5f6a7b8c9
Revises: 3f8d9c123abc
Create Date: 2026-07-06 00:00:00.000000

Speeds up the Webhook History page's status / source-IP filters and the
common "endpoint + status, newest first" query.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e5f6a7b8c9"
down_revision = "3f8d9c123abc"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_webhook_log_source_ip", "webhook_log", ["source_ip"])
    op.create_index(
        "ix_webhook_log_config_status_created",
        "webhook_log",
        ["config_id", "status", "created_at"],
    )


def downgrade():
    op.drop_index("ix_webhook_log_config_status_created", table_name="webhook_log")
    op.drop_index("ix_webhook_log_source_ip", table_name="webhook_log")
