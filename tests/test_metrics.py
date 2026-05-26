import json
from unittest.mock import MagicMock, patch

from hookwise.metrics import (
    REDIS_METRICS_KEY_PREFIX,
    RedisMetricRegistry,
    log_psa_task,
    log_webhook_processed,
    log_webhook_received,
)


@patch("hookwise.metrics.redis_client")
def test_incr_counter_no_labels(mock_redis):
    """Test incrementing a counter without labels."""
    RedisMetricRegistry.incr_counter("test_metric")

    expected_key = f"{REDIS_METRICS_KEY_PREFIX}:counter:test_metric:{json.dumps({}, sort_keys=True)}"
    mock_redis.incr.assert_called_once_with(expected_key)


@patch("hookwise.metrics.redis_client")
def test_incr_counter_with_labels(mock_redis):
    """Test incrementing a counter with labels."""
    labels = {"service": "web", "region": "us-east-1"}
    RedisMetricRegistry.incr_counter("test_metric", labels)

    expected_labels_json = json.dumps(labels, sort_keys=True)
    expected_key = f"{REDIS_METRICS_KEY_PREFIX}:counter:test_metric:{expected_labels_json}"
    mock_redis.incr.assert_called_once_with(expected_key)


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_incr_counter_redis_error(mock_logger, mock_redis):
    """Test that Redis errors are caught and logged."""
    mock_redis.incr.side_effect = Exception("Redis connection failed")

    # Should not raise exception
    RedisMetricRegistry.incr_counter("test_metric")

    mock_logger.error.assert_called_once()
    assert "Failed to increment metric test_metric in Redis" in mock_logger.error.call_args[0][0]


@patch("hookwise.metrics.redis_client")
def test_sync_to_prometheus(mock_redis):
    """Test syncing Redis metrics to Prometheus counters."""
    # Setup mock redis keys and values
    metric_name = "test_total"
    labels = {"status": "ok"}
    label_json = json.dumps(labels, sort_keys=True)
    redis_key = f"{REDIS_METRICS_KEY_PREFIX}:counter:{metric_name}:{label_json}"

    mock_redis.keys.return_value = [redis_key.encode()]
    mock_redis.get.return_value = "42.0".encode()

    # Setup mock Prometheus counter
    mock_counter = MagicMock()
    mock_labels_obj = MagicMock()
    mock_counter.labels.return_value = mock_labels_obj
    prometheus_counters = {metric_name: mock_counter}

    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    # Verify prometheus counter update
    mock_counter.labels.assert_called_with(**labels)
    mock_labels_obj._value.set.assert_called_with(42.0)


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_sync_to_prometheus_invalid_key(mock_logger, mock_redis):
    """Test that malformed keys are skipped."""
    mock_redis.keys.return_value = [b"invalid:key:format"]

    prometheus_counters = {}
    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    # No interaction with prometheus_counters expected
    assert mock_logger.error.call_count == 0


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_sync_to_prometheus_error_handling(mock_logger, mock_redis):
    """Test that errors during sync are logged."""
    mock_redis.keys.side_effect = Exception("Redis error")

    RedisMetricRegistry.sync_to_prometheus({})

    mock_logger.error.assert_called_once()
    assert "Failed to sync metrics from Redis" in mock_logger.error.call_args[0][0]


@patch.object(RedisMetricRegistry, "incr_counter")
def test_log_webhook_received(mock_incr):
    """Test log_webhook_received helper."""
    log_webhook_received("success", "my_config")
    mock_incr.assert_called_once_with(
        "hookwise_webhooks_received_total", {"status": "success", "config_name": "my_config"}
    )


@patch.object(RedisMetricRegistry, "incr_counter")
def test_log_webhook_processed(mock_incr):
    """Test log_webhook_processed helper."""
    log_webhook_processed("cfg_123", "processed")
    mock_incr.assert_called_once_with("hookwise_webhooks_total", {"config_id": "cfg_123", "status": "processed"})


@patch.object(RedisMetricRegistry, "incr_counter")
def test_log_psa_task(mock_incr):
    """Test log_psa_task helper."""
    log_psa_task("ticket_create", "ok")
    mock_incr.assert_called_once_with("hookwise_psa_tasks_total", {"type": "ticket_create", "result": "ok"})
