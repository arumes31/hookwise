import json
import logging
from typing import Any, Dict, List, Optional, cast

from prometheus_client import Counter

from .extensions import redis_client

logger = logging.getLogger(__name__)

# Key prefix for metrics in Redis
REDIS_METRICS_KEY_PREFIX = "hookwise:metrics"


class RedisMetricRegistry:
    """
    Handles metrics aggregation across multiple processes/containers using Redis.
    """

    @staticmethod
    def _get_redis_key(metric_name: str, labels: Dict[str, str]) -> str:
        # Create a stable key for the metric + labels combination
        label_str = json.dumps(labels, sort_keys=True)
        return f"{REDIS_METRICS_KEY_PREFIX}:counter:{metric_name}:{label_str}"

    @classmethod
    def incr_counter(cls, name: str, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter in Redis."""
        labels = labels or {}
        key = cls._get_redis_key(name, labels)
        try:
            redis_client.incr(key)
        except Exception as e:
            logger.error(f"Failed to increment metric {name} in Redis: {e}")

    @classmethod
    def sync_to_prometheus(cls, prometheus_counters: Dict[str, Counter]) -> None:
        """
        Fetch all matching counters from Redis and update the local Prometheus objects.
        This should be called just before exporting /metrics.
        """
        try:
            # Pattern: prefix:counter:metric_name:*
            pattern = f"{REDIS_METRICS_KEY_PREFIX}:counter:*"
            keys = cast(List[Any], redis_client.keys(pattern))

            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                # Format: prefix:counter:name:labels_json
                parts = key_str.split(":", 4)
                if len(parts) < 5:
                    continue

                metric_name = parts[3]
                label_json = parts[4]

                if metric_name in prometheus_counters:
                    try:
                        labels = json.loads(label_json)
                        value_raw = cast(Optional[str], redis_client.get(key))
                        if value_raw:
                            value = float(value_raw)
                            # In Prometheus client, we can't easily "set" a counter to a specific value
                            # if it's already higher, but for this fresh export context, clearing and
                            # incrementing is a common pattern for bridged metrics.
                            # However, since these counters in Flask are usually fresh per process start
                            # or we can reset them, we'll use the internal '_value' if available
                            # or just increment by the difference.

                            counter_obj = prometheus_counters[metric_name].labels(**labels)
                            # Direct override of the value to match Redis (the source of truth)
                            counter_obj._value.set(value)
                    except Exception as e:
                        logger.error(f"Error syncing key {key_str}: {e}")
        except Exception as e:
            logger.error(f"Failed to sync metrics from Redis: {e}")


# Helper functions for specific metrics
def log_webhook_received(status: str, config_name: str) -> None:
    RedisMetricRegistry.incr_counter("hookwise_webhooks_received_total", {"status": status, "config_name": config_name})


def log_webhook_processed(config_id: str, status: str) -> None:
    RedisMetricRegistry.incr_counter("hookwise_webhooks_total", {"config_id": config_id, "status": status})


def log_psa_task(task_type: str, result: str) -> None:
    RedisMetricRegistry.incr_counter("hookwise_psa_tasks_total", {"type": task_type, "result": result})
