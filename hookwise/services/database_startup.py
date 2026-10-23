"""Apply versioned database upgrades before web and worker startup."""

from pathlib import Path

from alembic import command
from flask import Flask

from ..extensions import migrate


def upgrade_database(app: Flask) -> None:
    """Fail startup if the schema cannot be upgraded; env.py serializes runners."""
    from .. import models  # noqa: F401 -- register every table before migration

    directory = Path(__file__).resolve().parents[2] / "migrations"
    with app.app_context():
        config = migrate.get_config(directory=str(directory))
        command.upgrade(config, "head")
