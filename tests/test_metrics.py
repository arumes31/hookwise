import json
from unittest.mock import MagicMock, patch

from prometheus_client import Counter

from hookwise.metrics import (
    RedisMetricRegistry,
    log_psa_task,
    log_webhook_processed,
    log_webhook_received,
)


@patch("hookwise.metrics.redis_client")
def test_log_webhook_received(mock_redis):
    log_webhook_received("success", "test_config")

    expected_labels = {"status": "success", "config_name": "test_config"}
    label_str = json.dumps(expected_labels, sort_keys=True)
    expected_key = f"hookwise:metrics:counter:hookwise_webhooks_received_total:{label_str}"

    mock_redis.incr.assert_called_once_with(expected_key)


@patch("hookwise.metrics.redis_client")
def test_log_webhook_processed(mock_redis):
    log_webhook_processed("config_123", "processed")

    expected_labels = {"config_id": "config_123", "status": "processed"}
    label_str = json.dumps(expected_labels, sort_keys=True)
    expected_key = f"hookwise:metrics:counter:hookwise_webhooks_total:{label_str}"

    mock_redis.incr.assert_called_once_with(expected_key)


@patch("hookwise.metrics.redis_client")
def test_log_psa_task(mock_redis):
    log_psa_task("ticket_creation", "success")

    expected_labels = {"type": "ticket_creation", "result": "success"}
    label_str = json.dumps(expected_labels, sort_keys=True)
    expected_key = f"hookwise:metrics:counter:hookwise_psa_tasks_total:{label_str}"

    mock_redis.incr.assert_called_once_with(expected_key)


@patch("hookwise.metrics.redis_client")
def test_sync_to_prometheus(mock_redis):
    # Setup mock data in Redis
    metric_name = "hookwise_webhooks_total"
    labels = {"config_id": "config_123", "status": "processed"}
    label_json = json.dumps(labels, sort_keys=True)
    redis_key = f"hookwise:metrics:counter:{metric_name}:{label_json}"

    mock_redis.keys.return_value = [redis_key.encode()]
    mock_redis.get.return_value = b"42"

    # Mock Prometheus counter
    mock_counter = MagicMock(spec=Counter)
    mock_label_instance = MagicMock()
    mock_counter.labels.return_value = mock_label_instance

    prometheus_counters = {metric_name: mock_counter}

    # Call sync
    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    # Verify
    mock_counter.labels.assert_called_once_with(**labels)
    mock_label_instance._value.set.assert_called_once_with(42.0)


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_incr_counter_redis_error(mock_logger, mock_redis):
    mock_redis.incr.side_effect = Exception("Redis connection failed")

    # Should not raise exception
    RedisMetricRegistry.incr_counter("test_metric", {"a": "1"})

    mock_logger.error.assert_called_once()
    assert "Failed to increment metric test_metric in Redis" in mock_logger.error.call_args[0][0]


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_sync_to_prometheus_redis_error(mock_logger, mock_redis):
    mock_redis.keys.side_effect = Exception("Redis error")

    # Should not raise exception
    RedisMetricRegistry.sync_to_prometheus({})

    mock_logger.error.assert_called_once()
    assert "Failed to sync metrics from Redis" in mock_logger.error.call_args[0][0]


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_sync_to_prometheus_individual_key_error(mock_logger, mock_redis):
    mock_redis.keys.return_value = [b"hookwise:metrics:counter:metric:invalid_json"]

    # Should not raise exception, but log error for individual key
    RedisMetricRegistry.sync_to_prometheus({"metric": MagicMock()})

    mock_logger.error.assert_called_once()
    assert "Error syncing key hookwise:metrics:counter:metric:invalid_json" in mock_logger.error.call_args[0][0]


@patch("hookwise.metrics.redis_client")
def test_sync_to_prometheus_invalid_key_format(mock_redis):
    # Key with less than 5 parts
    mock_redis.keys.return_value = [b"hookwise:metrics:counter:too_short"]

    # Should skip without error
    RedisMetricRegistry.sync_to_prometheus({})

    mock_redis.get.assert_not_called()
