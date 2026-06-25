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
    with (
        patch("hookwise.extensions.redis_client") as mock_ext_redis,
        patch("hookwise.tasks.redis_client") as mock_tasks_redis,
        patch("hookwise.api.redis_client") as mock_api_redis,
    ):
        # Make redis_client.get return None by default to avoid maintenance mode
        mock_ext_redis.get.return_value = None
        mock_tasks_redis.get.return_value = None
        mock_api_redis.get.return_value = None

        yield {
            "ext": mock_ext_redis,
            "tasks": mock_tasks_redis,
            "api": mock_api_redis
        }
