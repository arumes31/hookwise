"""Add AI routing fields

Revision ID: aa0069cc86c2
Revises: d3e4f5g6h7i8
Create Date: 2026-02-12 22:50:44.968450

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'aa0069cc86c2'
down_revision = 'd3e4f5g6h7i8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('webhook_config', sa.Column('ai_routing_enabled', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('webhook_config', sa.Column('ai_prompt_template', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('webhook_config', 'ai_prompt_template')
    op.drop_column('webhook_config', 'ai_routing_enabled')
