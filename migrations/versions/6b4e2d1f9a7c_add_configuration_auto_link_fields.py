"""Add configuration auto-link settings and result fields.

Revision ID: 6b4e2d1f9a7c
Revises: 5c8d2e9f1a4b
Create Date: 2026-09-04 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "6b4e2d1f9a7c"
down_revision = "5c8d2e9f1a4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_config",
        sa.Column(
            "auto_link_configuration_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "webhook_log",
        sa.Column("configuration_link_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "webhook_log",
        sa.Column("configuration_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook_log", "configuration_id")
    op.drop_column("webhook_log", "configuration_link_status")
    op.drop_column("webhook_config", "auto_link_configuration_enabled")
