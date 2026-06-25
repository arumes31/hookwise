import json
from io import BytesIO

import pytest

from hookwise.extensions import db
from hookwise.models import WebhookConfig


@pytest.fixture
def app():
    from hookwise import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    # Ensure we have a secret key for session
    app.config["SECRET_KEY"] = "test-secret"
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

def test_restore_config_functionality(client, app):
    with app.app_context():
        # Create some existing configs with string IDs
        id1 = "config-1-id"
        id2 = "config-2-id"
        id3 = "config-3-id"

        c1 = WebhookConfig(id=id1, name="Config 1", board="Board 1")
        c2 = WebhookConfig(id=id2, name="Config 2", board="Board 2")
        db.session.add_all([c1, c2])
        db.session.commit()

        # Data to restore: 1 update, 1 same, 1 new
        restore_data = [
            {"id": id1, "name": "Config 1 Updated", "board": "Board 1 New"},
            {"id": id2, "name": "Config 2", "board": "Board 2"},
            {"id": id3, "name": "New Config 3", "board": "Board 3"}
        ]

        data = BytesIO(json.dumps(restore_data).encode("utf-8"))

        # Bypass auth
        with client.session_transaction() as sess:
            sess["user_id"] = "admin"

        response = client.post(
            "/admin/restore",
            data={"backup_file": (data, "backup.json")},
            content_type="multipart/form-data"
        )

        assert response.status_code == 200
        assert response.json["status"] == "success"

        # Verify updates in DB
        updated_c1 = WebhookConfig.query.get(id1)
        assert updated_c1.name == "Config 1 Updated"
        assert updated_c1.board == "Board 1 New"

        updated_c2 = WebhookConfig.query.get(id2)
        assert updated_c2.name == "Config 2"

        new_c3 = WebhookConfig.query.get(id3)
        assert new_c3 is not None
        assert new_c3.name == "New Config 3"
        assert new_c3.board == "Board 3"
