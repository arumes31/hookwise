import os

content = """import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, cast

from celery import Celery, Task
from prometheus_client import Counter, Histogram

from .client import ConnectWiseClient, ConnectWiseError, TicketNotFoundError
from .extensions import build_redis_uri, db, redis_client
from .metrics import log_psa_task, log_webhook_processed
from .models import WebhookConfig, WebhookLog
from .utils import log_audit, log_to_web, resolve_jsonpath

logger = logging.getLogger(__name__)

# Prometheus Metrics
WEBHOOK_TOTAL = Counter("hookwise_webhooks_total", "Total webhooks received", ["config_id", "status"])
PSA_TASK_COUNT = Counter("hookwise_psa_tasks_total", "Total PSA tasks (ticket creation/resolution)", ["type", "result"])
PSA_TASK_DURATION = Histogram("hookwise_psa_task_seconds", "Time spent on PSA tasks", ["type"])

# Redis Cache setup
CACHE_PREFIX = "hookwise_ticket:"
CACHE_TTL = 3600 * 24  # 24 hours
_raw_viability_ttl = os.environ.get("VIABILITY_TTL", "300")
VIABILITY_TTL = max(1, int(_raw_viability_ttl)) if _raw_viability_ttl.isdigit() else 300

cw_client = ConnectWiseClient()


def make_celery(app_name: str) -> Celery:
    redis_password = os.environ.get("REDIS_PASSWORD")
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = os.environ.get("REDIS_PORT", 6379)

    default_url = build_redis_uri(redis_password, redis_host, redis_port, db=0)
    redis_url = os.environ.get("CELERY_BROKER_URL", default_url)

    celery = Celery(app_name, broker=redis_url, backend=redis_url)
    return celery


celery = make_celery("hookwise")
"""

with open("hookwise/tasks.py", "r") as f:
    lines = f.readlines()

# Find the line where the old celery initialization ended
start_index = -1
for i, line in enumerate(lines):
    if 'celery.conf.beat_schedule = {' in line:
        start_index = i
        break

if start_index != -1:
    rest = "".join(lines[start_index:])
    with open("hookwise/tasks.py", "w") as f:
        f.write(content + rest)
else:
    print("Could not find start of beat_schedule")
