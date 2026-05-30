import os
from unittest.mock import patch

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
def mock_redis():
    with patch("hookwise.extensions.redis_client") as mock:
        mock.get.return_value = None
        mock.ping.return_value = True
        yield mock

@pytest.fixture(autouse=True)
def mock_tasks_redis():
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock

@pytest.fixture(autouse=True)
def mock_api_redis():
    with patch("hookwise.api.redis_client") as mock:
        mock.get.return_value = None
        yield mock
