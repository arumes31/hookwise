"""Add is_pinned column

Revision ID: c2d3e4f5g6h7
Revises: a1b2c3d4e5f6
Create Date: 2026-02-11 22:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c2d3e4f5g6h7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("webhook_config", sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default="false"))


def downgrade():
    op.drop_column("webhook_config", "is_pinned")
