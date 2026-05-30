import pytest
import os
import tempfile
import time
from unittest.mock import MagicMock, patch
from hookwise import create_app
from hookwise.extensions import db
from hookwise.tasks import _get_global_mappings_cached, _GLOBAL_MAPPINGS_CACHE


@pytest.fixture
def app():
    # Set environment variables for test app
    old_db = os.environ.get("DATABASE_URL")
    old_testing = os.environ.get("TESTING")

    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_")
    os.close(fd)

    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["TESTING"] = "true"
    os.environ["GUI_PASSWORD"] = "testpassword"
    os.environ["ENCRYPTION_KEY"] = "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E="
    os.environ["SECRET_KEY"] = "test-secret"

    _app = create_app()

    with _app.app_context():
        db.create_all()
        yield _app
        db.session.remove()
        db.drop_all()

    if os.path.exists(path):
        os.unlink(path)

    if old_db:
        os.environ["DATABASE_URL"] = old_db
    else:
        os.environ.pop("DATABASE_URL", None)

    if old_testing:
        os.environ["TESTING"] = old_testing
    else:
        os.environ.pop("TESTING", None)


def test_global_mappings_cache_efficiency(app):
    """Test that global mappings are cached and refreshed after TTL."""
    with app.app_context():
        # Clear cache first
        _GLOBAL_MAPPINGS_CACHE.clear()

        # Mock GlobalMapping.query.all
        mock_mapping1 = MagicMock()
        mock_mapping1.tenant_value = "tenant1"
        mock_mapping1.company_id = "comp1"

        with patch("hookwise.tasks.GlobalMapping.query") as mock_query:
            mock_query.all.return_value = [mock_mapping1]

            # Use a manual time tracking approach
            current_time = 1000.0

            with patch("hookwise.tasks.time.time", side_effect=lambda: current_time):
                # 1. Initial load
                exact, wild = _get_global_mappings_cached()
                assert exact == {"tenant1": "comp1"}
                assert mock_query.all.call_count == 1

                # 2. Within TTL (300s)
                current_time = 1100.0
                exact2, wild2 = _get_global_mappings_cached()
                assert exact2 == exact
                assert mock_query.all.call_count == 1

                # 3. Past TTL
                current_time = 1400.0
                _get_global_mappings_cached()
                assert mock_query.all.call_count == 2
