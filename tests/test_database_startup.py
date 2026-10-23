"""Application startup must upgrade an existing database before serving requests."""

from unittest.mock import patch

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from hookwise import create_app
from hookwise.extensions import db, migrate


def test_startup_upgrades_old_database_and_is_repeatable(tmp_path):
    uri = "sqlite:///" + (tmp_path / "startup.db").as_posix()
    options = {"SQLALCHEMY_DATABASE_URI": uri, "AUTO_DB_UPGRADE": False, "RBAC_SCHEMA_BOOTSTRAP": False}
    old_app = create_app(options)
    with old_app.app_context():
        command.upgrade(migrate.get_config(), "b4e2c7a9d1f3")
        with db.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO webhook_config (id, name, bearer_token) VALUES ('existing', 'Existing', 'token')")
            )
    for _ in range(2):
        app = create_app({**options, "AUTO_DB_UPGRADE": True, "RBAC_SCHEMA_BOOTSTRAP": True})
        with app.app_context(), db.engine.connect() as connection:
            assert "customer_webhook_delivery" in inspect(connection).get_table_names()
            assert connection.execute(
                text("SELECT connectwise_delivery_enabled, forwarding_enabled FROM webhook_config WHERE id='existing'")
            ).one() == (1, 1)
            assert compare_metadata(MigrationContext.configure(connection), db.metadata) == []


def test_migration_failure_prevents_startup():
    with patch("alembic.command.upgrade", side_effect=RuntimeError("migration failed")):
        with pytest.raises(RuntimeError, match="migration failed"):
            create_app({"AUTO_DB_UPGRADE": True})


def test_explicit_migration_cli_does_not_auto_upgrade(monkeypatch):
    monkeypatch.setenv("FLASK_RUN_FROM_CLI", "true")
    monkeypatch.setattr("sys.argv", ["flask", "--app", "hookwise:create_app", "db", "downgrade", "-1"])
    with patch("alembic.command.upgrade") as upgrade:
        create_app({"AUTO_DB_UPGRADE": True})
        upgrade.assert_not_called()
