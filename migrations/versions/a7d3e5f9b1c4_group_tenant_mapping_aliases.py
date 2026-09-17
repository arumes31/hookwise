"""Group TenantMap aliases without changing flat webhook matching.

Revision ID: a7d3e5f9b1c4
Revises: e4b7c1d9a2f6
Create Date: 2026-09-17 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d3e5f9b1c4"
down_revision = "e4b7c1d9a2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill the nullable group identifier."""
    op.add_column("global_mapping", sa.Column("mapping_group_id", sa.String(length=36), nullable=True))
    op.execute(sa.text("UPDATE global_mapping SET mapping_group_id = id WHERE mapping_group_id IS NULL"))
    op.create_index("ix_global_mapping_mapping_group_id", "global_mapping", ["mapping_group_id"], unique=False)


def downgrade() -> None:
    """Remove only grouping metadata; flat alias rows remain routable."""
    op.drop_index("ix_global_mapping_mapping_group_id", table_name="global_mapping")
    op.drop_column("global_mapping", "mapping_group_id")
