import json
from unittest.mock import MagicMock, patch
import pytest
from hookwise.metrics import RedisMetricRegistry, log_webhook_received, log_webhook_processed, log_psa_task

@patch("hookwise.metrics.redis_client")
class TestRedisMetricRegistry:

    def test_get_redis_key(self, mock_redis):
        labels = {"config_name": "test_config", "status": "success"}
        key = RedisMetricRegistry._get_redis_key("test_metric", labels)
        expected_label_str = json.dumps(labels, sort_keys=True)
        assert key == f"hookwise:metrics:counter:test_metric:{expected_label_str}"

    def test_incr_counter(self, mock_redis):
        labels = {"status": "success"}
        RedisMetricRegistry.incr_counter("test_metric", labels)

        expected_key = RedisMetricRegistry._get_redis_key("test_metric", labels)
        mock_redis.incr.assert_called_once_with(expected_key)

    def test_incr_counter_no_labels(self, mock_redis):
        RedisMetricRegistry.incr_counter("test_metric")

        expected_key = RedisMetricRegistry._get_redis_key("test_metric", {})
        mock_redis.incr.assert_called_once_with(expected_key)

    def test_incr_counter_exception(self, mock_redis, caplog):
        mock_redis.incr.side_effect = Exception("Redis error")

        with caplog.at_level("ERROR"):
            RedisMetricRegistry.incr_counter("test_metric")

        assert "Failed to increment metric test_metric in Redis: Redis error" in caplog.text

    def test_sync_to_prometheus(self, mock_redis):
        # Setup mock redis keys and get
        metric_name = "test_metric"
        labels = {"status": "success"}
        label_json = json.dumps(labels, sort_keys=True)
        key = f"hookwise:metrics:counter:{metric_name}:{label_json}"

        mock_redis.keys.return_value = [key.encode()]
        mock_redis.get.return_value = "10"

        # Setup mock prometheus counter
        mock_counter = MagicMock()
        mock_labels_obj = MagicMock()
        mock_counter.labels.return_value = mock_labels_obj

        prometheus_counters = {metric_name: mock_counter}

        RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

        mock_redis.get.assert_called_once_with(key.encode())
        mock_counter.labels.assert_called_once_with(**labels)
        mock_labels_obj._value.set.assert_called_once_with(10.0)

    def test_sync_to_prometheus_invalid_key(self, mock_redis):
        # Key with not enough parts
        mock_redis.keys.return_value = [b"hookwise:metrics:counter:short"]
        prometheus_counters = {}
        RedisMetricRegistry.sync_to_prometheus(prometheus_counters)
        # Should not call get or labels
        mock_redis.get.assert_not_called()

    def test_sync_to_prometheus_json_error(self, mock_redis, caplog):
        metric_name = "test_metric"
        key = f"hookwise:metrics:counter:{metric_name}:invalid_json"
        mock_redis.keys.return_value = [key.encode()]

        prometheus_counters = {metric_name: MagicMock()}

        with caplog.at_level("ERROR"):
            RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

        assert f"Error syncing key {key}:" in caplog.text

    def test_sync_to_prometheus_exception(self, mock_redis, caplog):
        mock_redis.keys.side_effect = Exception("Redis error")

        with caplog.at_level("ERROR"):
            RedisMetricRegistry.sync_to_prometheus({})

        assert "Failed to sync metrics from Redis: Redis error" in caplog.text

def test_helpers():
    with patch.object(RedisMetricRegistry, "incr_counter") as mock_incr:
        log_webhook_received("success", "my_config")
        mock_incr.assert_called_with("hookwise_webhooks_received_total", {"status": "success", "config_name": "my_config"})

        log_webhook_processed("123", "failed")
        mock_incr.assert_called_with("hookwise_webhooks_total", {"config_id": "123", "status": "failed"})

        log_psa_task("create_ticket", "ok")
        mock_incr.assert_called_with("hookwise_psa_tasks_total", {"type": "create_ticket", "result": "ok"})
