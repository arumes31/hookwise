import os

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"

import pytest


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass

@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    from unittest.mock import MagicMock
    mock = MagicMock()
    # Mock some common redis methods used in the app
    mock.get.return_value = None
    mock.set.return_value = True

    import hookwise.api
    import hookwise.extensions
    import hookwise.tasks

    monkeypatch.setattr(hookwise.extensions, "redis_client", mock)
    monkeypatch.setattr(hookwise.tasks, "redis_client", mock)
    monkeypatch.setattr(hookwise.api, "redis_client", mock)
    return mock
