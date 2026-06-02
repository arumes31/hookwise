import os
from unittest.mock import MagicMock, patch

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "testpass"

import pytest

@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass

@pytest.fixture(autouse=True)
def mock_redis():
    """Globally mock redis for all tests."""
    with patch("hookwise.extensions.redis_client") as mock_ext_redis,          patch("hookwise.tasks.redis_client") as mock_task_redis,          patch("hookwise.api.redis_client") as mock_api_redis:

        mock_ext_redis.ping.return_value = True
        mock_task_redis.ping.return_value = True
        mock_api_redis.ping.return_value = True

        # Default return value for GET to avoid issues in check_maintenance
        mock_ext_redis.get.return_value = None
        mock_task_redis.get.return_value = None
        mock_api_redis.get.return_value = None

        yield {
            "ext": mock_ext_redis,
            "task": mock_task_redis,
            "api": mock_api_redis
        }
