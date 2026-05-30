import os

os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["LIMITER_STORAGE_URI"] = "memory://"
os.environ["GUI_PASSWORD"] = "test-password"

import pytest
import unittest.mock as mock

@pytest.fixture(autouse=True)
def mock_redis():
    with mock.patch("hookwise.tasks.redis_client") as m1,          mock.patch("hookwise.api.redis_client") as m2,          mock.patch("hookwise.extensions.redis_client") as m3:
        m1.get.return_value = None
        m2.get.return_value = None
        m3.get.return_value = None
        yield (m1, m2, m3)
