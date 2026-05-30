from unittest.mock import patch

import pytest

from hookwise import create_app


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@patch("hookwise.commands.redis_client")
def test_clear_cw_cache_command_success(mock_redis, app):
    """Test successful execution of clear-cw-cache command."""
    # Mock scan_iter to return some keys
    mock_redis.scan_iter.return_value = ["hookwise_cw_key1", "hookwise_cw_key2"]

    runner = app.test_cli_runner()
    result = runner.invoke(args=["clear-cw-cache"])

    assert result.exit_code == 0
    assert "Successfully cleared 2 ConnectWise API cache keys." in result.output
    assert mock_redis.delete.call_count == 2
    mock_redis.delete.assert_any_call("hookwise_cw_key1")
    mock_redis.delete.assert_any_call("hookwise_cw_key2")


@patch("hookwise.commands.redis_client")
def test_clear_cw_cache_command_error(mock_redis, app):
    """Test error handling in clear-cw-cache command."""
    # Mock scan_iter to raise an exception
    mock_redis.scan_iter.side_effect = Exception("Redis error")

    runner = app.test_cli_runner()
    result = runner.invoke(args=["clear-cw-cache"])

    assert "Error clearing cache: Redis error" in result.output
