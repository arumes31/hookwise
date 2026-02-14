"""Remove ai_summary_enabled field

Revision ID: 2a87581868f5
Revises: fb8dcb0fbd26
Create Date: 2026-02-12 23:43:02.523976

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "2a87581868f5"
down_revision = "fb8dcb0fbd26"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("webhook_config", "ai_summary_enabled")


def downgrade():
    op.add_column(
        "webhook_config",
        sa.Column(
            "ai_summary_enabled", sa.BOOLEAN(), server_default=sa.text("false"), autoincrement=False, nullable=False
        ),
    )
