import pytest
from datetime import datetime, timezone, timedelta
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog, User
from hookwise import create_app
from werkzeug.security import generate_password_hash

@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["GUI_PASSWORD"] = "password"
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        user = User(username="admin", password_hash=generate_password_hash("password"), role="admin")
        db.session.add(user)
        db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = user.id
                sess["username"] = user.username
                sess["role"] = user.role
            yield client
        db.session.remove()
        db.drop_all()

def test_index_route(client, app):
    with app.app_context():
        config = WebhookConfig(name="Test Config", is_enabled=True)
        db.session.add(config)
        db.session.commit()

        log = WebhookLog(
            config_id=config.id,
            request_id="test-req",
            payload="{}",
            status="processed",
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(log)
        db.session.commit()

    response = client.get("/")
    assert response.status_code == 200
    assert b"Test Config" in response.data
    assert b"processed" in response.data
