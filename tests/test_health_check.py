import pytest
import os
from unittest.mock import patch, MagicMock
from hookwise.models import WebhookConfig
from hookwise.extensions import db
import hookwise.tasks

@pytest.fixture
def app_with_db():
    from hookwise import create_app
    app = create_app()
    # conftest.py already sets DATABASE_URL to sqlite:///:memory:
    with app.app_context():
        db.create_all()
        # Mock the global _app in hookwise.tasks
        old_app = hookwise.tasks._app
        hookwise.tasks._app = app
        yield app
        hookwise.tasks._app = old_app
        db.session.remove()
        db.drop_all()

@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_success(mock_cw, app_with_db):
    # Setup mocks
    mock_cw.get_boards.return_value = [{"name": "Board1", "id": 101}, {"name": "Board2", "id": 102}]
    mock_cw.get_priorities.return_value = [{"name": "P1"}, {"name": "P2"}]
    mock_cw.get_board_statuses.side_effect = lambda bid: [{"name": "New"}] if bid == 101 else [{"name": "Closed"}]

    with app_with_db.app_context():
        config = WebhookConfig(
            name="Healthy Config",
            board="Board1",
            status="New",
            priority="P1",
            is_enabled=True,
            config_health_status="UNKNOWN"
        )
        db.session.add(config)
        db.session.commit()

        # Call the run method directly to avoid ContextTask creating a new app
        hookwise.tasks.verify_endpoint_health.run()

        db.session.refresh(config)
        assert config.config_health_status == "OK"
        assert config.config_health_message == "Configuration validated"

@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_errors(mock_cw, app_with_db):
    # Setup mocks
    mock_cw.get_boards.return_value = [{"name": "Board1", "id": 101}]
    mock_cw.get_priorities.return_value = [{"name": "P1"}]
    mock_cw.get_board_statuses.return_value = [{"name": "New"}]

    with app_with_db.app_context():
        config1 = WebhookConfig(
            name="Invalid Board",
            board="WrongBoard",
            is_enabled=True
        )
        config2 = WebhookConfig(
            name="Invalid Status",
            board="Board1",
            status="WrongStatus",
            is_enabled=True
        )
        config3 = WebhookConfig(
            name="Invalid Priority",
            board="Board1",
            status="New",
            priority="WrongPriority",
            is_enabled=True
        )
        db.session.add_all([config1, config2, config3])
        db.session.commit()

        hookwise.tasks.verify_endpoint_health.run()

        db.session.refresh(config1)
        assert config1.config_health_status == "ERROR"
        assert "Board 'WrongBoard' not found" in config1.config_health_message

        db.session.refresh(config2)
        assert config2.config_health_status == "ERROR"
        assert "Status 'WrongStatus' not found" in config2.config_health_message

        db.session.refresh(config3)
        assert config3.config_health_status == "ERROR"
        assert "Priority 'WrongPriority' not found" in config3.config_health_message

@patch("hookwise.tasks.cw_client")
def test_verify_endpoint_health_no_boards(mock_cw, app_with_db):
    mock_cw.get_boards.return_value = []

    with app_with_db.app_context():
        config = WebhookConfig(name="Test", is_enabled=True, config_health_status="OK")
        db.session.add(config)
        db.session.commit()

        hookwise.tasks.verify_endpoint_health.run()

        db.session.refresh(config)
        # Should NOT change anything because it returns early
        assert config.config_health_status == "OK"
