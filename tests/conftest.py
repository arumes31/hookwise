import os
from unittest.mock import patch

import pytest

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "test-password"


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    pass


@pytest.fixture(autouse=True)
def mock_redis():
    with (
        patch("hookwise.tasks.redis_client") as mock_tasks_redis,
        patch("hookwise.api.redis_client") as mock_api_redis,
        patch("hookwise.extensions.redis_client") as mock_ext_redis,
    ):
        # Configure default behavior for mocks
        mock_tasks_redis.get.return_value = None
        mock_api_redis.get.return_value = None
        mock_api_redis.ping.return_value = True
        mock_ext_redis.get.return_value = None

        # Patch in commands as well, using the same mock as extensions
        with patch("hookwise.commands.redis_client", mock_ext_redis, create=True):
            yield (mock_tasks_redis, mock_api_redis, mock_ext_redis)
