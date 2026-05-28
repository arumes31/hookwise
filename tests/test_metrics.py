import json
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import Counter

from hookwise.metrics import (
    RedisMetricRegistry,
    log_psa_task,
    log_webhook_processed,
    log_webhook_received,
)


@pytest.fixture
def mock_redis():
    with patch("hookwise.metrics.redis_client") as mocked:
        yield mocked


def test_get_redis_key():
    labels = {"status": "success", "config_name": "test"}
    # Labels should be sorted alphabetically by json.dumps(sort_keys=True)
    expected_labels_json = json.dumps(labels, sort_keys=True)
    key = RedisMetricRegistry._get_redis_key("test_metric", labels)
    assert key == f"hookwise:metrics:counter:test_metric:{expected_labels_json}"


def test_incr_counter_success(mock_redis):
    labels = {"status": "success"}
    RedisMetricRegistry.incr_counter("test_metric", labels)

    key = RedisMetricRegistry._get_redis_key("test_metric", labels)
    mock_redis.incr.assert_called_once_with(key)


def test_incr_counter_no_labels(mock_redis):
    RedisMetricRegistry.incr_counter("test_metric")

    key = RedisMetricRegistry._get_redis_key("test_metric", {})
    mock_redis.incr.assert_called_once_with(key)


def test_incr_counter_error(mock_redis):
    mock_redis.incr.side_effect = Exception("Redis error")
    with patch("hookwise.metrics.logger") as mock_logger:
        RedisMetricRegistry.incr_counter("test_metric")
        mock_logger.error.assert_called_once()
        assert "Failed to increment metric test_metric in Redis" in mock_logger.error.call_args[0][0]


def test_sync_to_prometheus_success(mock_redis):
    # Mock redis_client.keys
    metric_name = "test_metric"
    labels = {"service": "api"}
    labels_json = json.dumps(labels, sort_keys=True)
    key = f"hookwise:metrics:counter:{metric_name}:{labels_json}"
    mock_redis.keys.return_value = [key.encode()]
    mock_redis.get.return_value = "42"

    # Mock Prometheus counter
    mock_counter = MagicMock(spec=Counter)
    mock_labels_obj = MagicMock()
    mock_counter.labels.return_value = mock_labels_obj

    prometheus_counters = {metric_name: mock_counter}

    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    mock_redis.keys.assert_called_once()
    mock_redis.get.assert_called_once_with(key.encode())
    mock_counter.labels.assert_called_once_with(**labels)
    mock_labels_obj._value.set.assert_called_once_with(42.0)


def test_sync_to_prometheus_invalid_key_format(mock_redis):
    mock_redis.keys.return_value = [b"too:short"]
    prometheus_counters = {}

    # Should not raise exception
    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)
    mock_redis.get.assert_not_called()


def test_sync_to_prometheus_metric_not_in_dict(mock_redis):
    key = "hookwise:metrics:counter:unknown_metric:{}"
    mock_redis.keys.return_value = [key.encode()]
    prometheus_counters = {"known_metric": MagicMock()}

    RedisMetricRegistry.sync_to_prometheus(prometheus_counters)
    mock_redis.get.assert_not_called()


def test_sync_to_prometheus_redis_error(mock_redis):
    mock_redis.keys.side_effect = Exception("Redis connection error")
    with patch("hookwise.metrics.logger") as mock_logger:
        RedisMetricRegistry.sync_to_prometheus({})
        mock_logger.error.assert_called_once()
        assert "Failed to sync metrics from Redis" in mock_logger.error.call_args[0][0]


def test_log_webhook_received(mock_redis):
    log_webhook_received("success", "my_config")
    key = RedisMetricRegistry._get_redis_key(
        "hookwise_webhooks_received_total", {"status": "success", "config_name": "my_config"}
    )
    mock_redis.incr.assert_called_once_with(key)


def test_log_webhook_processed(mock_redis):
    log_webhook_processed("cfg_123", "processed")
    key = RedisMetricRegistry._get_redis_key("hookwise_webhooks_total", {"config_id": "cfg_123", "status": "processed"})
    mock_redis.incr.assert_called_once_with(key)


def test_log_psa_task(mock_redis):
    log_psa_task("ticket_creation", "success")
    key = RedisMetricRegistry._get_redis_key(
        "hookwise_psa_tasks_total", {"type": "ticket_creation", "result": "success"}
    )
    mock_redis.incr.assert_called_once_with(key)
