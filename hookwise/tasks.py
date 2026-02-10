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
from .models import WebhookConfig, WebhookLog
from .client import ConnectWiseClient
from .utils import resolve_jsonpath, log_to_web
import json

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

@celery.task(bind=True, name="hookwise.process_webhook", max_retries=5)
def process_webhook_task(self, config_id: str, data: Dict[str, Any], request_id: str):
    """Background task to process webhook logic."""
    try:
        handle_webhook_logic(config_id, data, request_id)
    except Exception as exc:
        # Retry for external API failures (usually caught inside handle_webhook_logic, 
        # but if we bubble up a specific retryable error, handle it here)
        logger.error(f"Task failed, retrying: {exc}")
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

def is_in_maintenance(config: WebhookConfig) -> bool:
    """Check if current time is within a maintenance window."""
    if not config.maintenance_windows:
        return False
    try:
        windows = json.loads(config.maintenance_windows)
        now = datetime.utcnow()
        for window in windows:
            # Simple format: {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z"}
            start = datetime.fromisoformat(window['start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(window['end'].replace('Z', '+00:00'))
            if start <= now.replace(tzinfo=None) <= end:
                return True
    except Exception as e:
        logger.error(f"Error checking maintenance window: {e}")
    return False

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
        
    with app.app_context():
        # 1. Create Webhook History Log
        log_entry = WebhookLog(
            config_id=config_id,
            request_id=request_id,
            payload=json.dumps(data),
            status="processing"
        )
        db.session.add(log_entry)
        db.session.commit()
        log_id = log_entry.id

        try:
            # 2. Check Maintenance Window
            if is_in_maintenance(config):
                log_entry.status = "skipped"
                log_entry.error_message = "Skipped: Maintenance Window Active"
                db.session.commit()
                log_to_web(f"Webhook skipped (Maintenance Window Active)", "info", config.name, data=data)
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
            json_mapping_str = config.json_mapping
            routing_rules_str = config.routing_rules

            config.last_seen_at = datetime.utcnow()
            db.session.commit()

            # Parse JSON mappings and routing rules
            json_mapping = {}
            if json_mapping_str:
                try:
                    json_mapping = json.loads(json_mapping_str)
                except Exception as e:
                    logger.error(f"Failed to parse json_mapping: {e}", extra=extra)

            routing_rules = []
            if routing_rules_str:
                try:
                    routing_rules = json.loads(routing_rules_str)
                except Exception as e:
                    logger.error(f"Failed to parse routing_rules: {e}", extra=extra)

            # 1. Apply JSONPath Mappings
            mapped_summary = None
            if 'summary' in json_mapping:
                mapped_summary = resolve_jsonpath(data, json_mapping['summary'])
            
            mapped_description = None
            if 'description' in json_mapping:
                mapped_description = resolve_jsonpath(data, json_mapping['description'])

            mapped_customer_id = None
            if 'customer_id' in json_mapping:
                mapped_customer_id = resolve_jsonpath(data, json_mapping['customer_id'])

            # 2. Apply Regex Routing Rules
            for rule in routing_rules:
                rule_path = rule.get('path')
                rule_regex = rule.get('regex')
                rule_overrides = rule.get('overrides', {})
                
                if rule_path and rule_regex:
                    val = str(resolve_jsonpath(data, rule_path))
                    if re.search(rule_regex, val, re.IGNORECASE):
                        logger.info(f"Routing rule matched: {rule_regex} on {rule_path}", extra=extra)
                        if 'board' in rule_overrides: board = rule_overrides['board']
                        if 'status' in rule_overrides: status = rule_overrides['status']
                        if 'ticket_type' in rule_overrides: ticket_type = rule_overrides['ticket_type']
                        if 'subtype' in rule_overrides: subtype = rule_overrides['subtype']
                        if 'priority' in rule_overrides: priority = rule_overrides['priority']

            actual_val = str(resolve_jsonpath(data, trigger_field))
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
            ticket_summary = mapped_summary or (f"{prefix} {monitor_name}" if prefix else monitor_name)
            cache_key = f"{CACHE_PREFIX}{config_id}:{monitor_name}"

            ticket_id = None
            if alert_type == "DOWN" or alert_type == "GENERIC":
                cached_val = cast(Optional[bytes], redis_client.get(cache_key))
                if cached_val:
                    ticket_id = int(cached_val.decode())
                    note_text = f"Duplicate {alert_type} alert detected. Updated details:\nMessage: {msg}\nRequest ID: {request_id}"
                    cw_client.add_ticket_note(ticket_id, note_text)
                    log_to_web(f"{alert_type} alert: Updated existing ticket (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
                    PSA_TASK_COUNT.labels(type='create', result='updated').inc()
                    log_entry.status = "processed"
                    log_entry.ticket_id = ticket_id
                    db.session.commit()
                    return

                existing_ticket = cw_client.find_open_ticket(ticket_summary)
                if existing_ticket:
                    ticket_id = existing_ticket['id']
                    note_text = f"Duplicate {alert_type} alert found in CW. Updated details:\nMessage: {msg}\nRequest ID: {request_id}"
                    cw_client.add_ticket_note(ticket_id, note_text)
                    log_to_web(f"{alert_type} alert: Found and updated open ticket (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
                    redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
                    PSA_TASK_COUNT.labels(type='create', result='updated').inc()
                    log_entry.status = "processed"
                    log_entry.ticket_id = ticket_id
                    db.session.commit()
                    return
                
                company_id_match = re.search(r'#CW(\w+)', monitor_name)
                company_id = mapped_customer_id or (company_id_match.group(1) if company_id_match else customer_id_default)
                
                if mapped_description:
                    description = mapped_description
                else:
                    description = f"Source: {monitor_name}\nMessage: {msg}\nRequest ID: {request_id}\nPayload: {data}"
                
                new_ticket = cw_client.create_ticket(summary=ticket_summary, description=description, monitor_name=monitor_name, company_id=company_id, board=board, status=status, ticket_type=ticket_type, subtype=subtype, priority=priority)
                if new_ticket:
                    ticket_id = new_ticket['id']
                    redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
                    log_to_web(f"{alert_type} alert: Created NEW ticket (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
                    PSA_TASK_COUNT.labels(type='create', result='success').inc()
            
            elif alert_type == "UP":
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
            
            # Finalize SUCCESS
            log_entry.status = "processed"
            log_entry.ticket_id = ticket_id
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            log_entry.status = "failed"
            log_entry.error_message = str(e)
            db.session.commit()
            logger.error(f"Error handling webhook: {e}", extra=extra)
            raise e
