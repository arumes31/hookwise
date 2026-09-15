"""Add durable CIPP Defender incident correlation state.

Revision ID: e4b7c1d9a2f6
Revises: d9f1a7c4e2b6
Create Date: 2026-09-15 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "e4b7c1d9a2f6"
down_revision = "d9f1a7c4e2b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cipp_defender_incident_state",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_key", sa.String(length=255), nullable=False),
        sa.Column("incident_key", sa.String(length=255), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("seen_alert_ids", sa.Text(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column("bundle_key", sa.String(length=32), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["webhook_config.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "config_id",
            "tenant_key",
            "incident_key",
            name="uq_cipp_defender_incident_config_tenant_key",
        ),
    )
    op.create_index(
        "ix_cipp_defender_incident_state_config_id",
        "cipp_defender_incident_state",
        ["config_id"],
    )
    op.create_index(
        "ix_cipp_defender_incident_state_ticket_id",
        "cipp_defender_incident_state",
        ["ticket_id"],
    )
    op.create_index(
        "ix_cipp_defender_incident_config_tenant_bundle",
        "cipp_defender_incident_state",
        ["config_id", "tenant_key", "bundle_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_cipp_defender_incident_config_tenant_bundle", table_name="cipp_defender_incident_state")
    op.drop_index("ix_cipp_defender_incident_state_ticket_id", table_name="cipp_defender_incident_state")
    op.drop_index("ix_cipp_defender_incident_state_config_id", table_name="cipp_defender_incident_state")
    op.drop_table("cipp_defender_incident_state")
