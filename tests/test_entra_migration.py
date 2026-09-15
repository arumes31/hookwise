import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

migration = importlib.import_module("migrations.versions.d9f1a7c4e2b6_add_entra_app_roles_and_overrides")


def _legacy_user_table(metadata: sa.MetaData, *, with_identity: bool = False) -> sa.Table:
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(20), nullable=True),
    ]
    if with_identity:
        columns.extend(
            [
                sa.Column("auth_source", sa.String(16), nullable=True),
                sa.Column("entra_tid", sa.String(64), nullable=True),
                sa.Column("entra_oid", sa.String(64), nullable=True),
                sa.Column("upn", sa.String(255), nullable=True),
                sa.Column("is_active", sa.Boolean(), nullable=True),
                sa.Column("last_login_at", sa.DateTime(), nullable=True),
            ]
        )
    return sa.Table("user", metadata, *columns)


def _upgrade(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()


def test_entra_migration_adds_bridge_identity_schema_to_clean_database():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    user = _legacy_user_table(metadata)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            user.insert(),
            {"id": "local-1", "username": "admin", "password_hash": "hash", "role": "admin"},
        )

    _upgrade(engine)

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("user")}
    indexes = {index["name"] for index in inspector.get_indexes("user")}
    assert {
        "auth_source",
        "entra_tid",
        "entra_oid",
        "upn",
        "is_active",
        "last_login_at",
        "entra_role",
        "entra_role_synced_at",
        "is_override_active",
        "override_role",
    } <= columns
    assert {"ix_user_entra_oid", "ix_user_upn", "ix_user_entra_role", "ix_user_override_role"} <= indexes


def test_entra_migration_preserves_bridge_identity_and_invalidates_permissions():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    user = _legacy_user_table(metadata, with_identity=True)
    rbac_meta = sa.Table(
        "rbac_meta",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("permissions_epoch", sa.Integer(), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            user.insert(),
            {
                "id": "entra-1",
                "username": "user@example.test",
                "password_hash": "hash",
                "role": "admin",
                "auth_source": "entra",
                "entra_tid": "tenant-1",
                "entra_oid": "object-1",
                "upn": "user@example.test",
                "is_active": True,
            },
        )
        connection.execute(rbac_meta.insert(), {"id": 1, "permissions_epoch": 7})

    _upgrade(engine)

    with engine.connect() as connection:
        migrated = (
            connection.execute(
                sa.text(
                    'SELECT auth_source, entra_tid, entra_oid, upn, is_active, entra_role FROM "user" WHERE id = :id'
                ),
                {"id": "entra-1"},
            )
            .mappings()
            .one()
        )
        epoch = connection.execute(sa.text("SELECT permissions_epoch FROM rbac_meta WHERE id = 1")).scalar_one()

    assert dict(migrated) == {
        "auth_source": "entra",
        "entra_tid": "tenant-1",
        "entra_oid": "object-1",
        "upn": "user@example.test",
        "is_active": 1,
        "entra_role": migration.ENTRA_NO_PERMISSIONS_ROLE,
    }
    assert epoch == 8
