import os

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

from unittest.mock import MagicMock
import sys

@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    mock = MagicMock()
    # Mocking in hookwise.extensions
    monkeypatch.setattr("hookwise.extensions.redis_client", mock)
    # Mocking in hookwise.tasks
    monkeypatch.setattr("hookwise.tasks.redis_client", mock)
    # Mocking in hookwise.api
    monkeypatch.setattr("hookwise.api.redis_client", mock)
    return mock
