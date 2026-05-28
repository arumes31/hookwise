import os
from unittest.mock import MagicMock, patch

# Set environment variables before any other imports
os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "admin"

import pytest

# Patch redis.Redis before importing anything from hookwise
mock_redis_client = MagicMock()
mock_redis_client.get.return_value = None
mock_redis_client.set.return_value = True

# We use a global patcher to ensure it's active during module imports
patcher = patch("redis.Redis", return_value=mock_redis_client)
patcher.start()


@pytest.fixture(autouse=True)
def redis_mock():
    return mock_redis_client


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass
