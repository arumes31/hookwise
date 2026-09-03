"""Nr. 12: add webhook_config.archived_at

Archivieren statt Loeschen: gesetzt = archiviert. Archivierte Endpoints sind
zusaetzlich pausiert (is_enabled=False); die bestehenden is_enabled-Filter
halten sie damit aus Ingest, Tasks und Timeout-Monitor heraus.

Von Hand geschrieben, weil der Dev-Container das Repo read-only mountet und
flask db migrate die Datei dort nicht ablegen kann. Inhaltlich identisch mit
der Autogenerate-Fassung fuer eine einzelne nullable Spalte.

Revision ID: 9a4b7c1d2e3f
Revises: c8e3f1a2b5d6
"""
from alembic import op
import sqlalchemy as sa

revision = "9a4b7c1d2e3f"
down_revision = "c8e3f1a2b5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "webhook_config",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("webhook_config", "archived_at")
