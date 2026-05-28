import pytest

from hookwise import create_app


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app


def test_clear_cw_cache_command(app, mock_redis):
    """Test the clear-cw-cache command."""
    # The mock_redis fixture from conftest.py returns (mock_tasks_redis, mock_api_redis, mock_ext_redis)
    _, _, mock_ext_redis = mock_redis

    # Setup mock
    mock_ext_redis.scan_iter.return_value = ["hookwise_cw_1", "hookwise_cw_2"]
    mock_ext_redis.scan_iter.side_effect = None

    runner = app.test_cli_runner()
    result = runner.invoke(args=["clear-cw-cache"])

    assert "Successfully cleared 2 ConnectWise API cache keys." in result.output
    assert result.exit_code == 0
    assert mock_ext_redis.delete.call_count == 2
    mock_ext_redis.delete.assert_any_call("hookwise_cw_1")
    mock_ext_redis.delete.assert_any_call("hookwise_cw_2")


def test_clear_cw_cache_command_error(app, mock_redis):
    """Test the clear-cw-cache command when an error occurs."""
    _, _, mock_ext_redis = mock_redis

    # Setup mock to raise exception
    mock_ext_redis.scan_iter.side_effect = Exception("Redis error")
    mock_ext_redis.scan_iter.return_value = None  # Clear any previous return_value

    runner = app.test_cli_runner()
    result = runner.invoke(args=["clear-cw-cache"])

    assert "Error clearing cache: Redis error" in result.output
    assert result.exit_code == 0  # Command catches exception and prints it
