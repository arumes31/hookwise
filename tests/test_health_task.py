import pytest
import os
from unittest.mock import patch, MagicMock
from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.tasks import verify_endpoint_health

@pytest.fixture
def app():
    os.environ["GUI_PASSWORD"] = "testpass"
    _app = create_app()
    _app.config["TESTING"] = True
    _app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///test_health.db"
    with _app.app_context():
        db.create_all()
        # Mock hookwise.tasks._app to use our test app
        with patch("hookwise.tasks._app", _app):
            yield _app
        db.session.remove()
        db.drop_all()
    if os.path.exists("test_health.db"):
        try:
            os.remove("test_health.db")
        except:
            pass

@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_success(mock_cw, app):
    """Test that valid configurations are marked as OK."""
    mock_cw.get_boards.return_value = [{"name": "Test Board", "id": 1}]
    mock_cw.get_priorities.return_value = [{"name": "P1"}]
    mock_cw.get_board_statuses.return_value = [{"name": "New"}]

    with app.app_context():
        config = WebhookConfig(
            name="Valid Config",
            board="Test Board",
            status="New",
            priority="P1",
            is_enabled=True
        )
        db.session.add(config)
        db.session.commit()

        verify_endpoint_health()

        db.session.refresh(config)
        assert config.config_health_status == "OK"
        assert config.config_health_message == "Configuration validated"

@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_errors(mock_cw, app):
    """Test that invalid configurations are marked with ERROR and messages."""
    mock_cw.get_boards.return_value = [{"name": "Valid Board", "id": 1}]
    mock_cw.get_priorities.return_value = [{"name": "Valid Priority"}]
    mock_cw.get_board_statuses.return_value = [{"name": "Valid Status"}]

    with app.app_context():
        # 1. Invalid Board
        config_bad_board = WebhookConfig(
            name="Bad Board",
            board="Invalid Board",
            is_enabled=True
        )

        # 2. Invalid Status
        config_bad_status = WebhookConfig(
            name="Bad Status",
            board="Valid Board",
            status="Invalid Status",
            is_enabled=True
        )

        # 3. Invalid Priority
        config_bad_priority = WebhookConfig(
            name="Bad Priority",
            priority="Invalid Priority",
            is_enabled=True
        )

        db.session.add_all([config_bad_board, config_bad_status, config_bad_priority])
        db.session.commit()

        verify_endpoint_health()

        db.session.refresh(config_bad_board)
        db.session.refresh(config_bad_status)
        db.session.refresh(config_bad_priority)

        assert config_bad_board.config_health_status == "ERROR"
        assert "Board 'Invalid Board' not found" in config_bad_board.config_health_message

        assert config_bad_status.config_health_status == "ERROR"
        assert "Status 'Invalid Status' not found" in config_bad_status.config_health_message

        assert config_bad_priority.config_health_status == "ERROR"
        assert "Priority 'Invalid Priority' not found" in config_bad_priority.config_health_message

@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_cw_failure(mock_cw, app):
    """Test that if CW is unreachable, it logs a warning and returns."""
    mock_cw.get_boards.return_value = []

    with app.app_context():
        config = WebhookConfig(name="Test", is_enabled=True)
        db.session.add(config)
        db.session.commit()

        # Should return early without updating anything
        verify_endpoint_health()

        db.session.refresh(config)
        assert config.config_health_status == "OK" # Default value
