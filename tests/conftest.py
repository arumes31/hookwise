import os
from unittest.mock import patch

import pytest

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass


@pytest.fixture(autouse=True)
def mock_redis():
    """Globally mock Redis to avoid connection errors during tests."""
    with patch("hookwise.tasks.redis_client") as mock_redis_client:
        mock_redis_client.get.return_value = None
        yield mock_redis_client
