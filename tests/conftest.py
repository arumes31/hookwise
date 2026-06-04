import os
from unittest.mock import patch

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "test-pass"

import pytest


@pytest.fixture(autouse=True, scope="session")
def mock_redis_global():
    """Globally mock Redis for all tests to avoid connection errors."""
    with (
        patch("hookwise.extensions.redis_client") as mock_ext,
        patch("hookwise.tasks.redis_client") as mock_tasks,
        patch("hookwise.api.redis_client") as mock_api,
    ):
        mock_ext.get.return_value = None
        mock_tasks.get.return_value = None
        mock_api.get.return_value = None
        yield (mock_ext, mock_tasks, mock_api)


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass
