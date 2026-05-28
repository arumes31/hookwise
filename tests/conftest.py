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
def mock_redis():
    """Global mock for redis_client to prevent connection errors during tests."""
    # Patch across all modules that use redis_client
    with patch("hookwise.tasks.redis_client") as mock_tasks_redis, \
         patch("hookwise.api.redis_client") as mock_api_redis, \
         patch("hookwise.metrics.redis_client") as mock_metrics_redis, \
         patch("hookwise.extensions.redis_client") as mock_ext_redis, \
         patch("hookwise.commands.redis_client") as mock_cmd_redis:

        # Configure default return values
        mock_tasks_redis.get.return_value = None
        mock_api_redis.get.return_value = None
        mock_metrics_redis.get.return_value = None
        mock_ext_redis.get.return_value = None
        mock_cmd_redis.get.return_value = None

        yield {
            'tasks': mock_tasks_redis,
            'api': mock_api_redis,
            'metrics': mock_metrics_redis,
            'ext': mock_ext_redis,
            'cmd': mock_cmd_redis
        }

@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass
