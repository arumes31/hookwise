"""Regression coverage for additive TenantMap alias grouping."""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

migration = importlib.import_module("migrations.versions.a7d3e5f9b1c4_group_tenant_mapping_aliases")


def test_tenantmap_group_migration_backfills_existing_rows():
    """Give every legacy alias its own rollback-compatible logical group."""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "global_mapping",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_value", sa.String(255), nullable=False, unique=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {"id": "mapping-one", "tenant_value": "one.example"},
                {"id": "mapping-two", "tenant_value": "two.example"},
            ],
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("global_mapping")}
    indexes = {index["name"] for index in inspector.get_indexes("global_mapping")}
    assert columns["mapping_group_id"]["nullable"] is True
    assert "ix_global_mapping_mapping_group_id" in indexes
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT id, mapping_group_id FROM global_mapping ORDER BY id")).all()
    assert rows == [("mapping-one", "mapping-one"), ("mapping-two", "mapping-two")]
