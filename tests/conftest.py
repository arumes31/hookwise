import os
import pytest

@pytest.fixture(autouse=True, scope="session")
def setup_test_env():
    os.environ["SOCKETIO_ASYNC_MODE"] = "threading"
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    # Ensure gevent is not messing with us during tests if not monkey-patched elsewhere
