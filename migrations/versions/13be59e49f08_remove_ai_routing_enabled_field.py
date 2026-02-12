"""Remove ai_routing_enabled field

Revision ID: 13be59e49f08
Revises: 2a87581868f5
Create Date: 2026-02-12 23:54:02.858159

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '13be59e49f08'
down_revision = '2a87581868f5'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('webhook_config', 'ai_routing_enabled')


def downgrade():
    op.add_column('webhook_config', sa.Column('ai_routing_enabled', sa.BOOLEAN(), server_default=sa.text('false'), autoincrement=False, nullable=False))
