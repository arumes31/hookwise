import json
import io
from unittest.mock import patch
import pytest
from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, User

@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["GUI_PASSWORD"] = "testpass" # Required by new check
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@patch("hookwise.tasks.redis_client")
@patch("hookwise.api.redis_client")
@patch("hookwise.extensions.redis_client")
def test_backup_restore_flow(mock_ext_redis, mock_api_redis, mock_tasks_redis, client, app):
    """Test backing up and restoring configurations."""
    mock_ext_redis.get.return_value = None
    mock_api_redis.get.return_value = None
    mock_tasks_redis.get.return_value = None

    with app.app_context():
        # 1. Setup initial config
        c1 = WebhookConfig(id="config1", name="Config 1", board="Board 1")
        db.session.add(c1)
        db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["username"] = "admin"
        sess["role"] = "admin"

    # 2. Backup
    resp = client.get("/admin/backup")
    assert resp.status_code == 200
    backup_data = json.loads(resp.data)
    assert len(backup_data) == 1
    assert backup_data[0]["id"] == "config1"

    # 3. Modify data for restore
    backup_data[0]["name"] = "Config 1 Updated"
    backup_data.append({
        "id": "config2",
        "name": "Config 2",
        "board": "Board 2"
    })

    # 4. Restore
    data = io.BytesIO(json.dumps(backup_data).encode("utf-8"))
    resp = client.post(
        "/admin/restore",
        data={"backup_file": (data, "backup.json")},
        content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    assert resp.json["status"] == "success"

    # 5. Verify restoration
    with app.app_context():
        configs = WebhookConfig.query.order_by(WebhookConfig.id).all()
        assert len(configs) == 2
        assert configs[0].id == "config1"
        assert configs[0].name == "Config 1 Updated"
        assert configs[1].id == "config2"
        assert configs[1].name == "Config 2"
