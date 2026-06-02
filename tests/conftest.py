import os
from unittest.mock import patch

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "admin" # Set GUI_PASSWORD to avoid startup error

import pytest

@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass

@pytest.fixture(autouse=True)
def mock_redis():
    """Mock Redis to avoid connection errors in before_request check_maintenance."""
    # Need to patch in all modules that might use it
    with patch("hookwise.tasks.redis_client") as m1,          patch("hookwise.api.redis_client") as m2,          patch("hookwise.extensions.redis_client") as m3:
        m1.get.return_value = None
        m2.get.return_value = None
        m3.get.return_value = None
        yield (m1, m2, m3)
