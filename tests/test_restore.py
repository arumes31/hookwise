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
            {"id": "new-2", "name": "Brand New", "board": "Brand New Board"}
        ]

        data = io.BytesIO(json.dumps(restore_data).encode("utf-8"))

        # 3. Simulate authenticated session
        with client.session_transaction() as sess:
            sess["user_id"] = "admin-id"
            sess["username"] = "admin"
            sess["role"] = "admin"

        # 4. Call restore endpoint
        response = client.post(
            "/admin/restore",
            data={"backup_file": (data, "backup.json")},
            content_type="multipart/form-data"
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
                "/admin/restore",
                data={"backup_file": (data, "backup.json")},
                content_type="multipart/form-data"
            )

            assert response.status_code == 200
            # If the fix works, WebhookConfig.query.get should NOT be called at all
            assert mock_get.call_count == 0

        # Verify that all configs were created
        assert WebhookConfig.query.count() == num_configs
