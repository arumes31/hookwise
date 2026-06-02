import json
from unittest.mock import MagicMock, patch

from hookwise.metrics import (
    REDIS_METRICS_KEY_PREFIX,
    RedisMetricRegistry,
    log_psa_task,
    log_webhook_processed,
    log_webhook_received,
)


def test_get_redis_key():
    metric_name = "test_metric"
    labels = {"a": "1", "b": "2"}
    expected_label_str = json.dumps(labels, sort_keys=True)
    expected_key = f"{REDIS_METRICS_KEY_PREFIX}:counter:{metric_name}:{expected_label_str}"

    assert RedisMetricRegistry._get_redis_key(metric_name, labels) == expected_key


@patch("hookwise.metrics.redis_client")
def test_incr_counter_success(mock_redis):
    metric_name = "test_metric"
    labels = {"a": "1"}
    RedisMetricRegistry.incr_counter(metric_name, labels)

    key = RedisMetricRegistry._get_redis_key(metric_name, labels)
    mock_redis.incr.assert_called_once_with(key)


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_incr_counter_failure(mock_logger, mock_redis):
    mock_redis.incr.side_effect = Exception("Redis error")
    RedisMetricRegistry.incr_counter("test_metric")

    mock_logger.error.assert_called_once()
    assert "Failed to increment metric" in mock_logger.error.call_args[0][0]


@patch("hookwise.metrics.redis_client")
def test_sync_to_prometheus_success(mock_redis):
    metric_name = "test_metric"
    labels = {"a": "1"}
    key = RedisMetricRegistry._get_redis_key(metric_name, labels)

    mock_redis.keys.return_value = [key.encode()]
    mock_redis.get.return_value = "42"

    mock_counter = MagicMock()
    prometheus_counters = {metric_name: mock_counter}

    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    mock_counter.labels.assert_called_once_with(**labels)
    mock_counter.labels()._value.set.assert_called_once_with(42.0)


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_sync_to_prometheus_failure(mock_logger, mock_redis):
    mock_redis.keys.side_effect = Exception("Redis error")
    RedisMetricRegistry.sync_to_prometheus({})

    mock_logger.error.assert_called_once()
    assert "Failed to sync metrics from Redis" in mock_logger.error.call_args[0][0]


@patch("hookwise.metrics.redis_client")
@patch("hookwise.metrics.logger")
def test_sync_to_prometheus_key_error(mock_logger, mock_redis):
    metric_name = "test_metric"
    # Invalid JSON in labels part
    key = f"{REDIS_METRICS_KEY_PREFIX}:counter:{metric_name}:invalid_json"

    mock_redis.keys.return_value = [key.encode()]
    prometheus_counters = {metric_name: MagicMock()}

    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    mock_logger.error.assert_called_once()
    assert "Error syncing key" in mock_logger.error.call_args[0][0]


@patch.object(RedisMetricRegistry, "incr_counter")
def test_log_webhook_received(mock_incr):
    log_webhook_received("success", "my_config")
    mock_incr.assert_called_once_with(
        "hookwise_webhooks_received_total", {"status": "success", "config_name": "my_config"}
    )


@patch.object(RedisMetricRegistry, "incr_counter")
def test_log_webhook_processed(mock_incr):
    log_webhook_processed("cfg_123", "processed")
    mock_incr.assert_called_once_with("hookwise_webhooks_total", {"config_id": "cfg_123", "status": "processed"})


@patch.object(RedisMetricRegistry, "incr_counter")
def test_log_psa_task(mock_incr):
    log_psa_task("ticket_create", "ok")
    mock_incr.assert_called_once_with("hookwise_psa_tasks_total", {"type": "ticket_create", "result": "ok"})
