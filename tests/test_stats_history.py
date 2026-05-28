import pytest
from datetime import datetime, timedelta, timezone
from hookwise.extensions import db
from hookwise.models import WebhookLog, WebhookConfig
from hookwise import create_app
import os
from unittest.mock import patch, MagicMock

@pytest.fixture
def app():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_stats_")
    os.close(fd)
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["SECRET_KEY"] = "test_secret"
    os.environ["ENCRYPTION_KEY"] = "D9kn14YPZb66Dpt4gY9MznD3TfFjKJ_i6TF_jHj7B3w="
    os.environ["GUI_PASSWORD"] = "password"

    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    with patch("hookwise.tasks.redis_client", mock_redis),          patch("hookwise.api.redis_client", mock_redis),          patch("hookwise.metrics.redis_client", mock_redis),          patch("hookwise.extensions.redis_client", mock_redis),          patch("hookwise.commands.redis_client", mock_redis):

        with patch("hookwise.tasks.check_webhook_timeouts", MagicMock()):
            app = create_app()
            with app.app_context():
                import hookwise.utils
                hookwise.utils._fernet_instance = None

                db.create_all()
                config = WebhookConfig(name="Test Config")
                db.session.add(config)
                db.session.commit()
                yield app
                db.session.remove()
                db.drop_all()
    if os.path.exists(path):
        os.remove(path)

@pytest.fixture
def client(app):
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = "admin"
            sess["username"] = "admin"
            sess["role"] = "admin"
        yield client

def test_get_stats_history_daily(app, client):
    with app.app_context():
        config = WebhookConfig.query.first()
        now = datetime.now(timezone.utc)
        for i in range(3):
            log = WebhookLog(
                config_id=config.id,
                request_id=f"req_{i}",
                payload="{}",
                status="processed",
                action="create",
                created_at=now - timedelta(days=i)
            )
            db.session.add(log)
        db.session.commit()

    response = client.get("/api/stats/history?period=daily")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 7
    assert data[-1]["created"] == 1
    assert data[-2]["created"] == 1

def test_get_stats_history_weekly(app, client):
    with app.app_context():
        config = WebhookConfig.query.first()
        now = datetime.now(timezone.utc)
        log1 = WebhookLog(
            config_id=config.id,
            request_id="req_w1",
            payload="{}",
            status="processed",
            action="update",
            created_at=now
        )
        log2 = WebhookLog(
            config_id=config.id,
            request_id="req_w2",
            payload="{}",
            status="processed",
            action="update",
            created_at=now - timedelta(days=8)
        )
        db.session.add(log1)
        db.session.add(log2)
        db.session.commit()

    response = client.get("/api/stats/history?period=weekly")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 4
    assert data[-1]["updated"] == 1
    found = False
    for entry in data[:-1]:
        if entry["updated"] == 1:
            found = True
    assert found

def test_get_stats_history_monthly(app, client):
    with app.app_context():
        config = WebhookConfig.query.first()
        now = datetime.now(timezone.utc)
        log1 = WebhookLog(
            config_id=config.id,
            request_id="req_m1",
            payload="{}",
            status="processed",
            action="close",
            created_at=now
        )
        db.session.add(log1)
        db.session.commit()

    response = client.get("/api/stats/history?period=monthly")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 6
    assert data[-1]["closed"] == 1
