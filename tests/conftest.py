import os
from unittest.mock import MagicMock, patch

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "test-password"

import pytest

@pytest.fixture(autouse=True)
def mock_redis():
    """Global fixture to mock redis_client across all tests."""
    with patch("hookwise.extensions.redis_client") as mock_ext,          patch("hookwise.api.redis_client") as mock_api,          patch("hookwise.tasks.redis_client") as mock_tasks:

        mock_ext.get.return_value = None
        mock_ext.ping.return_value = True
        mock_api.get.return_value = None
        mock_api.ping.return_value = True
        mock_tasks.get.return_value = None
        mock_tasks.ping.return_value = True

        yield (mock_ext, mock_api, mock_tasks)

@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass
