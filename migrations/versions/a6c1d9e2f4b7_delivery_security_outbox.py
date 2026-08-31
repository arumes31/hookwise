"""delivery security, idempotency, and transactional outbox

Revision ID: a6c1d9e2f4b7
Revises: 4bd1a6b2c3d4
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, InvalidToken

revision: str = "a6c1d9e2f4b7"
down_revision: str | None = "4bd1a6b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _encrypt_existing_secrets() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, bearer_token, hmac_secret FROM webhook_config")).mappings()
    records = list(rows)
    if not records:
        return
    raw_key = os.environ.get("ENCRYPTION_KEY")
    if not raw_key:
        raise RuntimeError("ENCRYPTION_KEY is required to migrate stored endpoint secrets")
    fernet = Fernet(raw_key.encode())
    for row in records:
        updates: dict[str, str] = {}
        for field in ("bearer_token", "hmac_secret"):
            value = row[field]
            if not value:
                continue
            try:
                fernet.decrypt(str(value).encode())
            except InvalidToken:
                updates[field] = fernet.encrypt(str(value).encode()).decode()
        if updates:
            bind.execute(
                sa.text(
                    "UPDATE webhook_config SET bearer_token = COALESCE(:bearer_token, bearer_token), "
                    "hmac_secret = COALESCE(:hmac_secret, hmac_secret) WHERE id = :id"
                ),
                {
                    "id": row["id"],
                    "bearer_token": updates.get("bearer_token"),
                    "hmac_secret": updates.get("hmac_secret"),
                },
            )


def _deduplicate_request_ids() -> None:
    bind = op.get_bind()
    duplicate_rows = bind.execute(
        sa.text(
            "SELECT id, request_id, duplicate_number FROM ("
            "SELECT id, request_id, ROW_NUMBER() OVER (PARTITION BY config_id, request_id ORDER BY created_at, id) "
            "AS duplicate_number FROM webhook_log) ranked WHERE duplicate_number > 1"
        )
    ).mappings()
    for row in duplicate_rows:
        replacement = f"{str(row['request_id'])[:75]}_dedup_{str(row['id'])[:8]}_{row['duplicate_number']}"[:100]
        bind.execute(
            sa.text("UPDATE webhook_log SET request_id = :request_id WHERE id = :id"),
            {"id": row["id"], "request_id": replacement},
        )


def upgrade() -> None:
    with op.batch_alter_table("webhook_config") as batch_op:
        batch_op.add_column(sa.Column("allow_unauthenticated", sa.Boolean(), nullable=False, server_default=sa.false()))
    _encrypt_existing_secrets()
    _deduplicate_request_ids()
    with op.batch_alter_table("webhook_log") as batch_op:
        batch_op.create_unique_constraint("uq_webhook_log_config_request", ["config_id", "request_id"])
    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("log_id", sa.String(length=36), nullable=False),
        sa.Column("task_name", sa.String(length=100), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["log_id"], ["webhook_log.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("log_id"),
    )
    op.create_index("ix_delivery_outbox_log_id", "delivery_outbox", ["log_id"])
    op.create_index("ix_delivery_outbox_status", "delivery_outbox", ["status"])
    op.create_index("ix_delivery_outbox_created_at", "delivery_outbox", ["created_at"])
    op.create_table(
        "ticket_operation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("log_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["log_id"], ["webhook_log.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("log_id", "operation", name="uq_ticket_operation_log_operation"),
    )
    op.create_index("ix_ticket_operation_log_id", "ticket_operation", ["log_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_operation_log_id", table_name="ticket_operation")
    op.drop_table("ticket_operation")
    op.drop_index("ix_delivery_outbox_created_at", table_name="delivery_outbox")
    op.drop_index("ix_delivery_outbox_status", table_name="delivery_outbox")
    op.drop_index("ix_delivery_outbox_log_id", table_name="delivery_outbox")
    op.drop_table("delivery_outbox")
    with op.batch_alter_table("webhook_log") as batch_op:
        batch_op.drop_constraint("uq_webhook_log_config_request", type_="unique")
    with op.batch_alter_table("webhook_config") as batch_op:
        batch_op.drop_column("allow_unauthenticated")
