"""Add delivery lifecycle, reliability, and UI-preference foundations.

Revision ID: 9a2d4f6e8b1c
Revises: c4e5f6a7b8c9
Create Date: 2026-08-27 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "9a2d4f6e8b1c"
down_revision = "c4e5f6a7b8c9"
branch_labels = None
depends_on = None


def _columns(table_name):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name):
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _foreign_key_columns(table_name):
    return {tuple(key["constrained_columns"]) for key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)}


def upgrade():
    config_columns = _columns("webhook_config")
    with op.batch_alter_table("webhook_config") as batch_op:
        if "bearer_token_last4" not in config_columns:
            batch_op.add_column(sa.Column("bearer_token_last4", sa.String(length=4), nullable=True))
        if "rate_limit_per_minute" not in config_columns:
            batch_op.add_column(sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"))
        if "retry_enabled" not in config_columns:
            batch_op.add_column(
                sa.Column("retry_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"))
            )
        if "retry_max_attempts" not in config_columns:
            batch_op.add_column(sa.Column("retry_max_attempts", sa.Integer(), nullable=False, server_default="5"))
        if "retry_base_delay_seconds" not in config_columns:
            batch_op.add_column(sa.Column("retry_base_delay_seconds", sa.Integer(), nullable=False, server_default="1"))
        if "retry_max_delay_seconds" not in config_columns:
            batch_op.add_column(
                sa.Column("retry_max_delay_seconds", sa.Integer(), nullable=False, server_default="300")
            )
    if "ix_webhook_config_bearer_token_last4" not in _indexes("webhook_config"):
        op.create_index("ix_webhook_config_bearer_token_last4", "webhook_config", ["bearer_token_last4"])

    log_columns = _columns("webhook_log")
    with op.batch_alter_table("webhook_log") as batch_op:
        if "correlation_id" not in log_columns:
            batch_op.add_column(sa.Column("correlation_id", sa.String(length=100), nullable=True))
        if "received_at" not in log_columns:
            batch_op.add_column(sa.Column("received_at", sa.DateTime(), nullable=True))
        if "queued_at" not in log_columns:
            batch_op.add_column(sa.Column("queued_at", sa.DateTime(), nullable=True))
        if "processing_started_at" not in log_columns:
            batch_op.add_column(sa.Column("processing_started_at", sa.DateTime(), nullable=True))
        if "connectwise_started_at" not in log_columns:
            batch_op.add_column(sa.Column("connectwise_started_at", sa.DateTime(), nullable=True))
        if "connectwise_responded_at" not in log_columns:
            batch_op.add_column(sa.Column("connectwise_responded_at", sa.DateTime(), nullable=True))
        if "completed_at" not in log_columns:
            batch_op.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))
        if "connectwise_duration_ms" not in log_columns:
            batch_op.add_column(sa.Column("connectwise_duration_ms", sa.Integer(), nullable=True))
        if "error_type" not in log_columns:
            batch_op.add_column(sa.Column("error_type", sa.String(length=100), nullable=True))
        if "error_chain" not in log_columns:
            batch_op.add_column(sa.Column("error_chain", sa.Text(), nullable=True))
        if "replay_of_log_id" not in log_columns:
            batch_op.add_column(sa.Column("replay_of_log_id", sa.String(length=36), nullable=True))
        if "retry_exhausted_at" not in log_columns:
            batch_op.add_column(sa.Column("retry_exhausted_at", sa.DateTime(), nullable=True))
    if ("replay_of_log_id",) not in _foreign_key_columns("webhook_log"):
        with op.batch_alter_table("webhook_log") as batch_op:
            batch_op.create_foreign_key(
                "fk_webhook_log_replay_of_log_id",
                "webhook_log",
                ["replay_of_log_id"],
                ["id"],
                ondelete="SET NULL",
            )
    indexes = _indexes("webhook_log")
    for name, columns in (
        ("ix_webhook_log_correlation_id", ["correlation_id"]),
        ("ix_webhook_log_error_type", ["error_type"]),
        ("ix_webhook_log_replay_of_log_id", ["replay_of_log_id"]),
        ("ix_webhook_log_config_request", ["config_id", "request_id"]),
        ("ix_webhook_log_status_created", ["status", "created_at"]),
        ("ix_webhook_log_ticket_id", ["ticket_id"]),
        ("ix_webhook_log_processing_time", ["processing_time"]),
        ("ix_webhook_log_received_at", ["received_at"]),
    ):
        if name not in indexes:
            op.create_index(name, "webhook_log", columns)

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "webhook_retry_attempt" not in tables:
        op.create_table(
            "webhook_retry_attempt",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("log_id", sa.String(length=36), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("retry_interval_seconds", sa.Float(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["log_id"], ["webhook_log.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("log_id", "attempt_number", name="uq_retry_attempt_log_number"),
        )
        op.create_index("ix_webhook_retry_attempt_log_id", "webhook_retry_attempt", ["log_id"])
        op.create_index("ix_webhook_retry_attempt_status", "webhook_retry_attempt", ["status"])
    if "endpoint_tag" not in tables:
        op.create_table(
            "endpoint_tag",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )
        op.create_index("ix_endpoint_tag_name", "endpoint_tag", ["name"])
    if "endpoint_tag_association" not in tables:
        op.create_table(
            "endpoint_tag_association",
            sa.Column("config_id", sa.String(length=64), nullable=False),
            sa.Column("tag_id", sa.String(length=36), nullable=False),
            sa.ForeignKeyConstraint(["config_id"], ["webhook_config.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tag_id"], ["endpoint_tag.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("config_id", "tag_id"),
        )
    if "user_preference" not in tables:
        op.create_table(
            "user_preference",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("dashboard_layout", sa.Text(), nullable=True),
            sa.Column("hidden_kpis", sa.Text(), nullable=True),
            sa.Column("dashboard_compact_mode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("dashboard_refresh_interval", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("timezone", sa.String(length=64), nullable=True),
            sa.Column("activity_buffer_size", sa.Integer(), nullable=False, server_default="200"),
            sa.Column("browser_notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("sound_notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id"),
        )
        op.create_index("ix_user_preference_user_id", "user_preference", ["user_id"])
    if "saved_history_search" not in tables:
        op.create_table(
            "saved_history_search",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("filters", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_saved_history_search_user_id", "saved_history_search", ["user_id"])
    if "event_annotation" not in tables:
        op.create_table(
            "event_annotation",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("log_id", sa.String(length=36), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["log_id"], ["webhook_log.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_event_annotation_user_id", "event_annotation", ["user_id"])
        op.create_index("ix_event_annotation_log_id", "event_annotation", ["log_id"])


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in (
        "event_annotation",
        "saved_history_search",
        "user_preference",
        "endpoint_tag_association",
        "endpoint_tag",
        "webhook_retry_attempt",
    ):
        if table_name in tables:
            op.drop_table(table_name)
    log_columns = _columns("webhook_log")
    log_indexes = _indexes("webhook_log")
    for index_name in (
        "ix_webhook_log_correlation_id",
        "ix_webhook_log_error_type",
        "ix_webhook_log_replay_of_log_id",
        "ix_webhook_log_config_request",
        "ix_webhook_log_status_created",
        "ix_webhook_log_ticket_id",
        "ix_webhook_log_processing_time",
        "ix_webhook_log_received_at",
    ):
        if index_name in log_indexes:
            op.drop_index(index_name, table_name="webhook_log")
    with op.batch_alter_table("webhook_log") as batch_op:
        for column in (
            "retry_exhausted_at",
            "replay_of_log_id",
            "error_chain",
            "error_type",
            "connectwise_duration_ms",
            "completed_at",
            "connectwise_responded_at",
            "connectwise_started_at",
            "processing_started_at",
            "queued_at",
            "received_at",
            "correlation_id",
        ):
            if column in log_columns:
                batch_op.drop_column(column)
    config_columns = _columns("webhook_config")
    if "ix_webhook_config_bearer_token_last4" in _indexes("webhook_config"):
        op.drop_index("ix_webhook_config_bearer_token_last4", table_name="webhook_config")
    with op.batch_alter_table("webhook_config") as batch_op:
        for column in (
            "retry_max_delay_seconds",
            "retry_base_delay_seconds",
            "retry_max_attempts",
            "retry_enabled",
            "rate_limit_per_minute",
            "bearer_token_last4",
        ):
            if column in config_columns:
                batch_op.drop_column(column)
