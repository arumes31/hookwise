import io
import json
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.services.backups import parse_backup


@pytest.fixture
def app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def mock_redis():
    """Mock Redis to avoid connection errors in before_request check_maintenance."""
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock


def test_restore_config_functionality(client, app):
    """Test that restore_config correctly updates and creates configurations."""
    with app.app_context():
        # 1. Setup: Create an existing config
        existing_cfg = WebhookConfig(id="existing-1", name="Old Name")
        db.session.add(existing_cfg)
        db.session.commit()

        # 2. Prepare restore data (one update, one new)
        restore_data = [
            {"id": "existing-1", "name": "Updated Name", "board": "New Board"},
            {"id": "new-2", "name": "Brand New", "board": "Brand New Board"},
        ]

        data = io.BytesIO(json.dumps(restore_data).encode("utf-8"))

        # 3. Simulate authenticated session
        with client.session_transaction() as sess:
            sess["user_id"] = "admin-id"
            sess["username"] = "admin"
            sess["role"] = "admin"

        # 4. Call restore endpoint
        response = client.post(
            "/admin/restore", data={"backup_file": (data, "backup.json")}, content_type="multipart/form-data"
        )

        assert response.status_code == 200
        assert response.json["status"] == "success"

        # 5. Verify database state
        updated_cfg = WebhookConfig.query.get("existing-1")
        assert updated_cfg.name == "Updated Name"
        assert updated_cfg.board == "New Board"

        new_cfg = WebhookConfig.query.get("new-2")
        assert new_cfg is not None
        assert new_cfg.name == "Brand New"
        assert new_cfg.board == "Brand New Board"


def test_restore_config_no_n_plus_one(client, app):
    """
    Test that restore_config does not perform N+1 queries.
    """
    with app.app_context():
        # Prepare a set of data
        num_configs = 5
        restore_data = [{"id": f"cfg-{i}", "name": f"Config {i}"} for i in range(num_configs)]
        data = io.BytesIO(json.dumps(restore_data).encode("utf-8"))

        with client.session_transaction() as sess:
            sess["user_id"] = "admin-id"
            sess["role"] = "admin"

        # We patch WebhookConfig.query.get to see if it is called
        with patch.object(WebhookConfig.query, "get") as mock_get:
            response = client.post(
                "/admin/restore", data={"backup_file": (data, "backup.json")}, content_type="multipart/form-data"
            )

            assert response.status_code == 200
            # If the fix works, WebhookConfig.query.get should NOT be called at all
            assert mock_get.call_count == 0

        # Verify that all configs were created
        assert WebhookConfig.query.count() == num_configs


def test_restore_config_rejects_out_of_range_delivery_controls(client, app):
    with app.app_context():
        config = WebhookConfig(id="delivery-controls", name="Endpoint")
        db.session.add(config)
        db.session.commit()

        restored = [
            {
                "id": config.id,
                "name": config.name,
                "retry_enabled": False,
                "retry_max_attempts": 999,
                "retry_base_delay_seconds": 120,
                "retry_max_delay_seconds": 2,
                "rate_limit_per_minute": 0,
            }
        ]
        with client.session_transaction() as sess:
            sess.update(user_id="admin-id", username="admin", role="admin")

        response = client.post(
            "/admin/restore",
            data={"backup_file": (io.BytesIO(json.dumps(restored).encode()), "backup.json")},
            content_type="multipart/form-data",
        )

        assert response.status_code == 400
        db.session.refresh(config)
        assert config.retry_enabled is True
        assert config.retry_max_attempts == 5
        assert config.retry_base_delay_seconds == 1
        assert config.retry_max_delay_seconds == 300
        assert config.rate_limit_per_minute == 60


def test_backup_is_encrypted_authenticated_and_versioned(client, app):
    with app.app_context():
        db.session.add(WebhookConfig(id="secure-backup", name="Secure", bearer_token="super-secret"))
        db.session.commit()
    with client.session_transaction() as sess:
        sess.update(user_id="admin-id", username="admin", role="admin")

    response = client.get("/admin/backup")

    assert response.status_code == 200
    assert response.mimetype == "application/vnd.hookwise.backup"
    assert b"super-secret" not in response.data
    document = parse_backup(response.data)
    assert document["format"] == "hookwise-config"
    assert document["version"] == 2
    assert document["configs"][0]["id"] == "secure-backup"

    tampered = bytearray(response.data)
    tampered[-1] = tampered[-1] ^ 1
    rejected = client.post(
        "/admin/restore",
        data={"backup_file": (io.BytesIO(tampered), "backup.hwbackup")},
        content_type="multipart/form-data",
    )
    assert rejected.status_code == 400
    assert "authentication" in rejected.get_json()["message"]
