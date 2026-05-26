from unittest.mock import patch
import pytest
from hookwise import create_app
from hookwise.extensions import db

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
    """Mock Redis to avoid connection errors."""
    with patch("hookwise.tasks.redis_client") as mock_tasks_redis, \
         patch("hookwise.api.redis_client") as mock_api_redis:
        mock_tasks_redis.get.return_value = None
        mock_api_redis.ping.return_value = True
        yield mock_tasks_redis, mock_api_redis

def test_clone_endpoint_not_found(client):
    """Test that cloning a non-existent endpoint returns 404."""
    # Simulate an authenticated session
    with client.session_transaction() as sess:
        sess["user_id"] = "test_user"
        sess["username"] = "testuser"
        sess["role"] = "admin"

    # POST to a non-existent ID
    response = client.post("/endpoint/clone/non-existent-id")

    # Assert that it returns 404
    assert response.status_code == 404
