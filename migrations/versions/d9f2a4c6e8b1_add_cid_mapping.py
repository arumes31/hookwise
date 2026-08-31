"""Add CID mapping discovery and company assignment.

Revision ID: d9f2a4c6e8b1
Revises: c8e3f1a2b5d6
"""

import sqlalchemy as sa
from alembic import op

revision = "d9f2a4c6e8b1"
down_revision = "c8e3f1a2b5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cid_mapping",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cid", sa.String(length=100), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("company_id", sa.String(length=50), nullable=True),
        sa.Column("seen_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cid_mapping_cid"), "cid_mapping", ["cid"], unique=True)
    op.create_index(op.f("ix_cid_mapping_last_seen_at"), "cid_mapping", ["last_seen_at"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_cid_mapping_last_seen_at"), table_name="cid_mapping")
    op.drop_index(op.f("ix_cid_mapping_cid"), table_name="cid_mapping")
    op.drop_table("cid_mapping")
