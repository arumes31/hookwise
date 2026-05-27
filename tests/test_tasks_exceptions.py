from unittest.mock import MagicMock, patch

import pytest

from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.tasks import check_webhook_timeouts


@pytest.fixture
def app():
    from hookwise import create_app

    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_check_webhook_timeouts_outer_exception(app):
    with patch("hookwise.tasks.WebhookConfig.query") as mock_query, \
         patch("hookwise.tasks.logger") as mock_logger, \
         patch("hookwise.tasks.db.session.rollback") as mock_rollback:
        mock_query.filter_by.side_effect = Exception("Outer error")

        check_webhook_timeouts()

        mock_logger.error.assert_called_with("Webhook timeout check task failed: Outer error")
        # rollback is called once in the exception handler
        assert mock_rollback.call_count >= 1


def test_check_webhook_timeouts_inner_exception(app):
    # Setup a mock config that will be returned by the query
    mock_config = MagicMock(spec=WebhookConfig)
    mock_config.name = "Test Endpoint"
    # We want to trigger an exception inside the loop when accessing last_seen_at
    type(mock_config).last_seen_at = property(MagicMock(side_effect=Exception("Inner error")))

    with patch("hookwise.tasks.WebhookConfig.query") as mock_query, \
         patch("hookwise.tasks.logger") as mock_logger, \
         patch("hookwise.tasks.db.session.rollback") as mock_rollback:
        mock_query.filter_by.return_value.all.return_value = [mock_config]

        check_webhook_timeouts()

        # Verify the inner loop exception handler was triggered
        error_calls = [call.args[0] for call in mock_logger.error.call_args_list]
        assert any(
            "Error processing timeout for endpoint 'Test Endpoint': Inner error" in msg for msg in error_calls
        )
        # It's called once per failed iteration
        assert mock_rollback.call_count >= 1
