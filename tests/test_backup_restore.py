import json
import io
import pytest
from hookwise.models import WebhookConfig, User
from hookwise.extensions import db
from hookwise import create_app
import os

@pytest.fixture
def app():
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["ENCRYPTION_KEY"] = "v36S8X8-X8X8X8X8X8X8X8X8X8X8X8X8X8X8X8X8X88="
    os.environ["GUI_PASSWORD"] = "test-password"
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        user = User(username="admin", password_hash="hash")
        db.session.add(user)
        db.session.commit()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = user.id
                sess["username"] = user.username
            yield client
        db.session.remove()
        db.drop_all()

def test_restore_config_success(client, app):
    backup_data = [
        {
            "id": "config-1",
            "name": "Config 1",
            "bearer_token": "token-1",
            "is_enabled": True
        },
        {
            "id": "config-2",
            "name": "Config 2",
            "bearer_token": "token-2",
            "is_enabled": False
        }
    ]

    data = {
        "backup_file": (io.BytesIO(json.dumps(backup_data).encode("utf-8")), "backup.json")
    }

    response = client.post("/admin/restore", data=data, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.json["status"] == "success"

    with app.app_context():
        configs = WebhookConfig.query.all()
        assert len(configs) == 2
        config_map = {c.id: c for c in configs}
        assert config_map["config-1"].name == "Config 1"
        assert config_map["config-2"].name == "Config 2"
        assert config_map["config-1"].is_enabled is True
        assert config_map["config-2"].is_enabled is False

def test_restore_config_update_existing(client, app):
    with app.app_context():
        existing = WebhookConfig(id="config-1", name="Old Name", bearer_token="old-token")
        db.session.add(existing)
        db.session.commit()

    backup_data = [
        {
            "id": "config-1",
            "name": "Updated Name",
            "bearer_token": "new-token"
        }
    ]

    data = {
        "backup_file": (io.BytesIO(json.dumps(backup_data).encode("utf-8")), "backup.json")
    }

    response = client.post("/admin/restore", data=data, content_type="multipart/form-data")
    assert response.status_code == 200

    with app.app_context():
        config = WebhookConfig.query.get("config-1")
        assert config.name == "Updated Name"
