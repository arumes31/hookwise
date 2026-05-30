import os
import unittest.mock

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "admin"

import pytest

@pytest.fixture(autouse=True, scope="session")
def mock_redis_client():
    mock_redis = unittest.mock.MagicMock()
    mock_redis.get.return_value = None
    with unittest.mock.patch("hookwise.tasks.redis_client", mock_redis),          unittest.mock.patch("hookwise.api.redis_client", mock_redis),          unittest.mock.patch("hookwise.extensions.redis_client", mock_redis):
        yield mock_redis

@pytest.fixture
def app():
    from hookwise import create_app
    from hookwise.extensions import db

    app = create_app()
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def runner(app):
    return app.test_cli_runner()
