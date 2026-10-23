"""Support multiple encrypted forwarding destinations and independent deliveries."""

import sqlalchemy as sa
from alembic import op

revision = "f2a8c4d6e9b1"
down_revision = "e8b4c2d6f9a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_webhook", sa.Column("additional_urls", sa.Text(), nullable=True))
    op.add_column(
        "customer_webhook_delivery", sa.Column("target_index", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    # The previous version only understands the first destination. Never send
    # another receiver's queued copy to that first destination after rollback.
    op.execute("DELETE FROM customer_webhook_delivery WHERE target_index <> 0")
    with op.batch_alter_table("customer_webhook_delivery") as batch:
        batch.drop_column("target_index")
    with op.batch_alter_table("customer_webhook") as batch:
        batch.drop_column("additional_urls")
