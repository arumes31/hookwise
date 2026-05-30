import os
from unittest.mock import MagicMock, patch

import pytest

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "test-pass"


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    # Mock redis globally for tests
    with (
        patch("hookwise.tasks.redis_client", MagicMock(), create=True),
        patch("hookwise.api.redis_client", MagicMock(), create=True),
        patch("hookwise.extensions.redis_client", MagicMock(), create=True),
        patch("hookwise.redis_client", MagicMock(), create=True),
    ):
        yield
