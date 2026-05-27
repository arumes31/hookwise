from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.commands import clear_cw_cache_command


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app

@pytest.fixture
def runner(app):
    return app.test_cli_runner()

@patch("hookwise.commands.redis_client")
def test_clear_cw_cache_command_success(mock_redis, runner):
    """Test successful execution of clear-cw-cache command."""
    # Setup mock
    mock_redis.scan_iter.return_value = ["hookwise_cw_1", "hookwise_cw_2"]

    # Run command
    result = runner.invoke(clear_cw_cache_command)

    # Assertions
    assert result.exit_code == 0
    assert "Successfully cleared 2 ConnectWise API cache keys." in result.output
    assert mock_redis.delete.call_count == 2
    mock_redis.delete.assert_any_call("hookwise_cw_1")
    mock_redis.delete.assert_any_call("hookwise_cw_2")

@patch("hookwise.commands.redis_client")
def test_clear_cw_cache_command_no_keys(mock_redis, runner):
    """Test execution when no keys match the pattern."""
    mock_redis.scan_iter.return_value = []

    result = runner.invoke(clear_cw_cache_command)

    assert result.exit_code == 0
    assert "Successfully cleared 0 ConnectWise API cache keys." in result.output
    assert mock_redis.delete.call_count == 0

@patch("hookwise.commands.redis_client")
def test_clear_cw_cache_command_error(mock_redis, runner):
    """Test error handling in clear-cw-cache command."""
    mock_redis.scan_iter.side_effect = Exception("Redis connection failed")

    result = runner.invoke(clear_cw_cache_command)

    assert result.exit_code == 0 # Command catches exception and echoes it
    assert "Error clearing cache: Redis connection failed" in result.output
