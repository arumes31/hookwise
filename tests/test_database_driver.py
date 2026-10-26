from unittest.mock import patch

import pytest
from flask import Flask

from hookwise import _configure_database_engine, create_app


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("postgresql://user:pass@db/app", "postgresql+psycopg2://user:pass@db/app"),
        (
            "postgresql://user:p%40ss@[2001:db8::1]:5432/app?sslmode=require",
            "postgresql+psycopg2://user:p%40ss@[2001:db8::1]:5432/app?sslmode=require",
        ),
        ("postgresql+psycopg2://user:pass@db/app", "postgresql+psycopg2://user:pass@db/app"),
        ("postgresql+psycopg://user:pass@db/app", "postgresql+psycopg://user:pass@db/app"),
        ("postgresql+asyncpg://user:pass@db/app", "postgresql+asyncpg://user:pass@db/app"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
    ],
)
def test_database_driver_keeps_existing_psycopg2_default(uri, expected):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri

    _configure_database_engine(app)

    assert app.config["SQLALCHEMY_DATABASE_URI"] == expected


def test_create_app_normalizes_config_override_before_extension_setup():
    captured = []

    class StopBeforeExtensionSetup(Exception):
        pass

    def capture_uri(app):
        captured.append(app.config["SQLALCHEMY_DATABASE_URI"])
        raise StopBeforeExtensionSetup

    with patch("hookwise._register_extensions", side_effect=capture_uri):
        with pytest.raises(StopBeforeExtensionSetup):
            create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "postgresql://user:pass@db/override"})

    assert captured == ["postgresql+psycopg2://user:pass@db/override"]
