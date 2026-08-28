"""Reconcile user secret length and webhook log cascade behavior.

Revision ID: 4bd1a6b2c3d4
Revises: 9a2d4f6e8b1c
Create Date: 2026-08-28 09:00:00.000000
"""

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "4bd1a6b2c3d4"
down_revision = "9a2d4f6e8b1c"
branch_labels = None
depends_on = None


def _config_foreign_key() -> Mapping[str, Any]:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("webhook_log"):
        if foreign_key["constrained_columns"] == ["config_id"]:
            return foreign_key
    raise RuntimeError("webhook_log.config_id foreign key was not found")


def _replace_config_foreign_key(*, ondelete: str | None) -> None:
    foreign_key = _config_foreign_key()
    constraint_name = foreign_key.get("name") or "fk_webhook_log_config_id_webhook_config"
    naming_convention = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}

    with op.batch_alter_table("webhook_log", naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_webhook_log_config_id_webhook_config",
            "webhook_config",
            ["config_id"],
            ["id"],
            ondelete=ondelete,
        )


def _unique_constraint_name(table_name: str, column_name: str) -> str:
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name):
        if constraint["column_names"] == [column_name]:
            return constraint.get("name") or f"uq_{table_name}_{column_name}"
    raise RuntimeError(f"{table_name}.{column_name} unique constraint was not found")


def _drop_unique_constraint(table_name: str, column_name: str) -> None:
    naming_convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    with op.batch_alter_table(table_name, naming_convention=naming_convention) as batch_op:
        batch_op.drop_constraint(_unique_constraint_name(table_name, column_name), type_="unique")


def _create_unique_constraint(table_name: str, column_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.create_unique_constraint(f"uq_{table_name}_{column_name}", [column_name])


def _ensure_timeout_check_constraint() -> None:
    constraints = sa.inspect(op.get_bind()).get_check_constraints("webhook_config")
    if any(constraint.get("name") == "webhook_config_timeout_hours_check" for constraint in constraints):
        return
    with op.batch_alter_table("webhook_config") as batch_op:
        batch_op.create_check_constraint("webhook_config_timeout_hours_check", "timeout_hours >= 0")


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.alter_column(
            "otp_secret",
            existing_type=sa.String(length=32),
            type_=sa.String(length=256),
            existing_nullable=True,
        )
    _ensure_timeout_check_constraint()
    _replace_config_foreign_key(ondelete="CASCADE")
    _drop_unique_constraint("endpoint_tag", "name")
    op.drop_index("ix_endpoint_tag_name", table_name="endpoint_tag")
    op.create_index("ix_endpoint_tag_name", "endpoint_tag", ["name"], unique=True)
    _drop_unique_constraint("user_preference", "user_id")
    op.drop_index("ix_user_preference_user_id", table_name="user_preference")
    op.create_index("ix_user_preference_user_id", "user_preference", ["user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_preference_user_id", table_name="user_preference")
    op.create_index("ix_user_preference_user_id", "user_preference", ["user_id"], unique=False)
    _create_unique_constraint("user_preference", "user_id")
    op.drop_index("ix_endpoint_tag_name", table_name="endpoint_tag")
    op.create_index("ix_endpoint_tag_name", "endpoint_tag", ["name"], unique=False)
    _create_unique_constraint("endpoint_tag", "name")
    _replace_config_foreign_key(ondelete=None)
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("webhook_config") as batch_op:
            batch_op.drop_constraint("webhook_config_timeout_hours_check", type_="check")
    with op.batch_alter_table("user") as batch_op:
        batch_op.alter_column(
            "otp_secret",
            existing_type=sa.String(length=256),
            type_=sa.String(length=32),
            existing_nullable=True,
        )
