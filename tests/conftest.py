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


@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    mock_redis = MagicMock()
    # Mock some common redis methods used in the app
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    with (
        patch("hookwise.extensions.redis_client", mock_redis),
        patch("hookwise.tasks.redis_client", mock_redis),
        patch("hookwise.api.redis_client", mock_redis),
    ):
        yield
