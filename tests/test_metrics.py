import json
import unittest
from unittest.mock import MagicMock, patch

from hookwise.metrics import (
    REDIS_METRICS_KEY_PREFIX,
    RedisMetricRegistry,
    log_psa_task,
    log_webhook_processed,
    log_webhook_received,
)


class TestRedisMetricRegistry(unittest.TestCase):
    def test_get_redis_key(self):
        name = "test_metric"
        labels = {"b": "2", "a": "1"}
        key = RedisMetricRegistry._get_redis_key(name, labels)

        # Labels should be sorted alphabetically in JSON
        expected_labels = json.dumps({"a": "1", "b": "2"}, sort_keys=True)
        expected_key = f"{REDIS_METRICS_KEY_PREFIX}:counter:{name}:{expected_labels}"
        self.assertEqual(key, expected_key)

    @patch("hookwise.metrics.redis_client")
    def test_incr_counter_success(self, mock_redis):
        name = "test_metric"
        labels = {"status": "success"}
        RedisMetricRegistry.incr_counter(name, labels)

        expected_key = RedisMetricRegistry._get_redis_key(name, labels)
        mock_redis.incr.assert_called_once_with(expected_key)

    @patch("hookwise.metrics.redis_client")
    @patch("hookwise.metrics.logger")
    def test_incr_counter_failure(self, mock_logger, mock_redis):
        mock_redis.incr.side_effect = Exception("Redis connection error")

        RedisMetricRegistry.incr_counter("test_metric")

        mock_logger.error.assert_called()
        self.assertIn("Failed to increment metric test_metric in Redis", mock_logger.error.call_args[0][0])

    @patch("hookwise.metrics.redis_client")
    def test_sync_to_prometheus(self, mock_redis):
        # Setup mock data in Redis
        metric_name = "test_metric"
        labels = {"job": "test"}
        label_json = json.dumps(labels, sort_keys=True)
        redis_key = f"{REDIS_METRICS_KEY_PREFIX}:counter:{metric_name}:{label_json}"

        mock_redis.keys.return_value = [redis_key.encode()]
        mock_redis.get.return_value = "10.5"

        # Mock Prometheus counter
        mock_counter = MagicMock()
        mock_labeled_counter = MagicMock()
        # Mocking the internal _value.set as used in the implementation
        mock_labeled_counter._value = MagicMock()
        mock_counter.labels.return_value = mock_labeled_counter
        prometheus_counters = {metric_name: mock_counter}

        RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

        mock_counter.labels.assert_called_once_with(**labels)
        mock_labeled_counter._value.set.assert_called_once_with(10.5)

    @patch("hookwise.metrics.redis_client")
    def test_sync_to_prometheus_malformed_key(self, mock_redis):
        # Short key that should be skipped (less than 5 parts)
        mock_redis.keys.return_value = ["hookwise:metrics:too:short".encode()]

        prometheus_counters = {}
        # Should not raise exception
        RedisMetricRegistry.sync_to_prometheus(prometheus_counters)

    @patch("hookwise.metrics.RedisMetricRegistry.incr_counter")
    def test_log_webhook_received(self, mock_incr):
        log_webhook_received("200", "my_config")
        mock_incr.assert_called_once_with(
            "hookwise_webhooks_received_total", {"status": "200", "config_name": "my_config"}
        )

    @patch("hookwise.metrics.RedisMetricRegistry.incr_counter")
    def test_log_webhook_processed(self, mock_incr):
        log_webhook_processed("cfg_123", "processed")
        mock_incr.assert_called_once_with(
            "hookwise_webhooks_total", {"config_id": "cfg_123", "status": "processed"}
        )

    @patch("hookwise.metrics.RedisMetricRegistry.incr_counter")
    def test_log_psa_task(self, mock_incr):
        log_psa_task("ticket_creation", "success")
        mock_incr.assert_called_once_with(
            "hookwise_psa_tasks_total", {"type": "ticket_creation", "result": "success"}
        )
