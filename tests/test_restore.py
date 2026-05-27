import io
import json
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@patch("hookwise.api.redis_client")
def test_restore_config(mock_redis, client, app):
    # Setup: Create some existing configs
    with app.app_context():
        config1 = WebhookConfig(id="id1", name="Config 1")
        db.session.add(config1)
        db.session.commit()

    # Data to restore (one update, one new)
    restore_data = [
        {"id": "id1", "name": "Updated Config 1", "board": "Board 1"},
        {"id": "id2", "name": "New Config 2", "board": "Board 2"},
    ]

    data = io.BytesIO(json.dumps(restore_data).encode("utf-8"))

    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"

    response = client.post(
        "/admin/restore", data={"backup_file": (data, "backup.json")}, content_type="multipart/form-data"
    )

    assert response.status_code == 200
    assert response.json["status"] == "success"

    # Verify updates
    with app.app_context():
        c1 = WebhookConfig.query.get("id1")
        assert c1.name == "Updated Config 1"
        assert c1.board == "Board 1"

        c2 = WebhookConfig.query.get("id2")
        assert c2 is not None
        assert c2.name == "New Config 2"
        assert c2.board == "Board 2"
