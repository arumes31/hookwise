import os
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.tasks import _get_health_check_metadata, _validate_single_config_health, verify_endpoint_health


@pytest.fixture
def app():
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_")
    os.close(fd)
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    # Mocking Redis to avoid connection errors
    with (
        patch("hookwise.tasks.redis_client"),
        patch("hookwise.api.redis_client"),
        patch("hookwise.metrics.redis_client"),
        patch("hookwise.extensions.redis_client"),
    ):
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            db.create_all()
            yield app
            db.session.remove()
            db.drop_all()
        with app.app_context():
            db.engine.dispose()
        if os.path.exists(path):
            os.remove(path)


@patch("hookwise.tasks.cw_client")
def test_get_health_check_metadata(mock_cw, app):
    mock_cw.get_boards.return_value = [{"name": "Board A", "id": 1}]
    mock_cw.get_priorities.return_value = [{"name": "P1"}]

    with app.app_context():
        metadata = _get_health_check_metadata()
        assert metadata is not None
        board_map, priority_names = metadata
        assert board_map == {"Board A": 1}
        assert priority_names == {"P1"}


@patch("hookwise.tasks.cw_client")
def test_validate_single_config_health_ok(mock_cw, app):
    mock_cw.get_board_statuses.return_value = [{"name": "New"}]

    with app.app_context():
        config = WebhookConfig(
            name="Config OK", board="Board A", status="New", priority="P1", config_health_status="UNKNOWN"
        )
        board_map = {"Board A": 1}
        priority_names = {"P1"}
        status_cache = {}

        updated = _validate_single_config_health(config, board_map, priority_names, status_cache)
        assert updated is True
        assert config.config_health_status == "OK"
        assert config.config_health_message == "Configuration validated"
        assert status_cache == {1: {"New"}}


def test_validate_single_config_health_error(app):
    with app.app_context():
        config = WebhookConfig(
            name="Config Error",
            board="Board Missing",
            status="Status Missing",
            priority="P Missing",
            config_health_status="OK",
        )
        board_map = {"Board A": 1}
        priority_names = {"P1"}
        status_cache = {}

        updated = _validate_single_config_health(config, board_map, priority_names, status_cache)
        assert updated is True
        assert config.config_health_status == "ERROR"
        assert "Board 'Board Missing' not found" in config.config_health_message
        assert "Priority 'P Missing' not found" in config.config_health_message


@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_integration(mock_cw, app):
    mock_cw.get_boards.return_value = [{"name": "Board A", "id": 1}]
    mock_cw.get_priorities.return_value = [{"name": "P1"}]
    mock_cw.get_board_statuses.return_value = [{"name": "New"}]

    with app.app_context():
        config = WebhookConfig(
            name="Integration Test",
            is_enabled=True,
            board="Board A",
            status="New",
            priority="P1",
            config_health_status="UNKNOWN",
            trigger_field="status",
            open_value="down",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        verify_endpoint_health()

        db.session.refresh(config)
        assert config.config_health_status == "OK"
