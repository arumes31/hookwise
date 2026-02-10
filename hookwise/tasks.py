import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, cast

import redis
from celery import Celery, Task
from prometheus_client import Counter, Histogram

from .extensions import db
from .models import WebhookConfig
from .client import ConnectWiseClient
from .utils import get_nested_value, log_to_web

logger = logging.getLogger(__name__)

# Prometheus Metrics
WEBHOOK_TOTAL = Counter('hookwise_webhooks_total', 'Total webhooks received', ['config_id', 'status'])
PSA_TASK_COUNT = Counter('hookwise_psa_tasks_total', 'Total PSA tasks (ticket creation/resolution)', ['type', 'result'])
PSA_TASK_DURATION = Histogram('hookwise_psa_task_seconds', 'Time spent on PSA tasks', ['type'])

# Redis Cache setup
CACHE_PREFIX = "hookwise_ticket:"
CACHE_TTL = 3600 * 24 # 24 hours
redis_client = redis.Redis(
    host=os.environ.get('REDIS_HOST', 'localhost'), 
    port=int(os.environ.get('REDIS_PORT', 6379)), 
    db=0,
    password=os.environ.get('REDIS_PASSWORD')
)
cw_client = ConnectWiseClient()

def make_celery(app_name):
    redis_url = f"redis://{os.environ.get('REDIS_HOST', 'localhost')}:{os.environ.get('REDIS_PORT', 6379)}/0"
    celery = Celery(
        app_name,
        broker=redis_url,
        backend=redis_url
    )
    return celery

celery = make_celery("hookwise")

_app = None

class ContextTask(Task):
    def __call__(self, *args, **kwargs):
        global _app
        if _app is None:
            from . import create_app
            _app = create_app()
        with _app.app_context():
            return self.run(*args, **kwargs)

celery.Task = ContextTask

@celery.task(name="hookwise.process_webhook")
def process_webhook_task(config_id: str, data: Dict[str, Any], request_id: str):
    """Background task to process webhook logic."""
    handle_webhook_logic(config_id, data, request_id)

def handle_webhook_logic(config_id: str, data: Dict[str, Any], request_id: str):
    """Core logic: process webhook payload and route to ConnectWise."""
    from flask import current_app as app
    extra = {'request_id': request_id, 'config_id': config_id}
    start_time = time.time()
    
    with app.app_context():
        config = WebhookConfig.query.get(config_id)
        if not config:
            logger.error(f"Config {config_id} not found", extra=extra)
            return
        
        config_name = config.name
        trigger_field = config.trigger_field or "heartbeat.status"
        open_value = config.open_value or "0"
        close_value = config.close_value or "1"
        ticket_prefix = config.ticket_prefix
        board = config.board
        status = config.status
        ticket_type = config.ticket_type
        subtype = config.subtype
        priority = config.priority
        customer_id_default = config.customer_id_default

        config.last_seen_at = datetime.utcnow()
        db.session.commit()

    actual_val = str(get_nested_value(data, trigger_field))
    monitor = data.get('monitor', {})
    monitor_name = monitor.get('name', data.get('title', data.get('name', 'Unknown Source')))
    msg = data.get('msg', data.get('message', 'No message'))
    
    open_triggers = [v.strip() for v in open_value.split(',') if v.strip()]
    close_triggers = [v.strip() for v in close_value.split(',') if v.strip()]

    if actual_val in open_triggers:
        alert_type = "DOWN"
    elif actual_val in close_triggers:
        alert_type = "UP"
    else:
        alert_type = "GENERIC"

    prefix = ticket_prefix or os.environ.get('CW_TICKET_PREFIX', 'Alert:')
    ticket_summary = f"{prefix} {monitor_name}" if prefix else monitor_name
    cache_key = f"{CACHE_PREFIX}{config_id}:{monitor_name}"

    if alert_type == "DOWN" or alert_type == "GENERIC":
        cached_val = cast(Optional[bytes], redis_client.get(cache_key))
        if cached_val:
            ticket_id = int(cached_val.decode())
            log_to_web(f"{alert_type} alert: Ticket exists (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
            PSA_TASK_COUNT.labels(type='create', result='skipped').inc()
            return

        existing_ticket = cw_client.find_open_ticket(ticket_summary)
        if existing_ticket:
            ticket_id = existing_ticket['id']
            log_to_web(f"{alert_type} alert: Found open ticket (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
            redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
            PSA_TASK_COUNT.labels(type='create', result='skipped').inc()
            return
        
        company_id_match = re.search(r'#CW(\w+)', monitor_name)
        company_id = company_id_match.group(1) if company_id_match else customer_id_default
        description = f"Source: {monitor_name}\nMessage: {msg}\nRequest ID: {request_id}\nPayload: {data}"
        
        new_ticket = cw_client.create_ticket(summary=ticket_summary, description=description, monitor_name=monitor_name, company_id=company_id, board=board, status=status, ticket_type=ticket_type, subtype=subtype, priority=priority)
        if new_ticket:
            ticket_id = new_ticket['id']
            redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
            log_to_web(f"{alert_type} alert: Created NEW ticket (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
            PSA_TASK_COUNT.labels(type='create', result='success').inc()

    elif alert_type == "UP":
        ticket_id = None
        cached_val = cast(Optional[bytes], redis_client.get(cache_key))
        if cached_val:
            ticket_id = int(cached_val.decode())
        else:
            existing_ticket = cw_client.find_open_ticket(ticket_summary)
            if existing_ticket: ticket_id = existing_ticket['id']

        if ticket_id:
            resolution = f"Resource {monitor_name} is back UP.\nMessage: {msg}\nID: {request_id}"
            if cw_client.close_ticket(ticket_id, resolution):
                redis_client.delete(cache_key)
                log_to_web(f"UP alert: Closed ticket (ID: {ticket_id})", "success", config_name, data=data, ticket_id=ticket_id)
                PSA_TASK_COUNT.labels(type='close', result='success').inc()
        else:
            log_to_web(f"UP alert: No open ticket to close for {monitor_name}", "success", config_name, data=data)
            PSA_TASK_COUNT.labels(type='close', result='skipped').inc()

    PSA_TASK_DURATION.labels(type=alert_type).observe(time.time() - start_time)
