import os
from unittest.mock import patch

import pytest

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "testpass"


@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.extensions.redis_client.get") as mock_get:
        mock_get.return_value = None
        yield mock_get
