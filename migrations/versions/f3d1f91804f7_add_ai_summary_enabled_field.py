"""Add ai_summary_enabled field

Revision ID: f3d1f91804f7
Revises: aa0069cc86c2
Create Date: 2026-02-12 22:57:06.656756

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f3d1f91804f7"
down_revision = "aa0069cc86c2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "webhook_config", sa.Column("ai_summary_enabled", sa.Boolean(), nullable=False, server_default="false")
    )


def downgrade():
    op.drop_column("webhook_config", "ai_summary_enabled")
