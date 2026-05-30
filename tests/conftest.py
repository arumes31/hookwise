import os
from unittest.mock import patch

import pytest

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "password"


@pytest.fixture(autouse=True, scope="session")
def mock_redis_global():
    """Mock Redis globally to avoid connection errors when Redis is not available."""
    # Some might import from hookwise directly
    with (
        patch("hookwise.tasks.redis_client") as m1,
        patch("hookwise.api.redis_client") as m2,
        patch("hookwise.extensions.redis_client") as m3,
        patch("hookwise.redis_client", create=True) as m4,
    ):
        # Default behavior: maintenance mode is OFF
        m1.get.return_value = None
        m2.get.return_value = None
        m3.get.return_value = None
        m4.get.return_value = None

        yield (m1, m2, m3, m4)


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Already set at top level, but kept for clarity
    pass
