import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, cast

import redis
from celery import Celery, Task
from prometheus_client import Counter, Histogram

from .client import ConnectWiseClient
from .extensions import db
from .models import WebhookConfig, WebhookLog
from .utils import log_to_web, resolve_jsonpath

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
celery.conf.beat_schedule = {
    'cleanup-logs-daily': {
        'task': 'hookwise.cleanup_logs',
        'schedule': 86400.0, # Every 24 hours
    },
}

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

@celery.task(name="hookwise.cleanup_logs")
def cleanup_logs():
    """Remove logs older than retention period."""
    from datetime import datetime, timedelta

    from .extensions import db
    from .models import WebhookLog
    
    retention_days = redis_client.get('hookwise_log_retention_days')
    retention_days = int(retention_days.decode()) if retention_days else int(os.environ.get('LOG_RETENTION_DAYS', 30))
    
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    
    deleted = WebhookLog.query.filter(WebhookLog.created_at < cutoff).delete()
    db.session.commit()
    logger.info(f"Cleaned up {deleted} log entries older than {retention_days} days.")

@celery.task(bind=True, name="hookwise.process_webhook", max_retries=5)
def process_webhook_task(self, config_id: str, data: Dict[str, Any], request_id: str, source_ip: str = None, headers: Dict[str, str] = None):
    """Background task to process webhook logic."""
    try:
        handle_webhook_logic(config_id, data, request_id, source_ip=source_ip, retry_count=self.request.retries, headers=headers)
    except Exception as exc:
        logger.error(f"Task failed (Attempt {self.request.retries}/5): {exc}")
        if self.request.retries >= self.max_retries:
            # Final failure, move to DLQ in DB
            from .extensions import db
            from .models import WebhookLog
            # We need to find the log entry and mark it as failed/dlq
            # Since we don't have log_id here, we use request_id
            log_entry = WebhookLog.query.filter_by(request_id=request_id).first()
            if log_entry:
                log_entry.status = "dlq"
                log_entry.error_message = f"Max retries exceeded: {str(exc)}"
                log_entry.retry_count = self.request.retries
                db.session.commit()
            return
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

def is_in_maintenance(config: WebhookConfig) -> bool:
    """Check if current time is within a maintenance window."""
    if not config.maintenance_windows:
        return False
    try:
        from datetime import timezone
        windows = json.loads(config.maintenance_windows)
        now = datetime.now(timezone.utc)
        for window in windows:
            # Simple format: {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z"}
            start = datetime.fromisoformat(window['start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(window['end'].replace('Z', '+00:00'))
            if start <= now <= end:
                return True
    except Exception as e:
        logger.error(f"Error checking maintenance window: {e}")
    return False

def handle_webhook_logic(config_id: str, data: Dict[str, Any], request_id: str, source_ip: str = None, retry_count: int = 0, headers: Dict[str, str] = None):
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
        # 1. Create or update Webhook History Log
        from .utils import mask_secrets
        log_entry = WebhookLog.query.filter_by(request_id=request_id).first()
        if not log_entry:
            log_entry = WebhookLog(
                config_id=config_id,
                request_id=request_id,
                payload=json.dumps(mask_secrets(data)),
                headers=json.dumps(headers) if headers else None,
                source_ip=source_ip,
                status="processing"
            )
            db.session.add(log_entry)
        
        log_entry.retry_count = retry_count
        db.session.commit()

        try:
            # 2. Check Maintenance Window
            if is_in_maintenance(config):
                log_entry.status = "skipped"
                log_entry.error_message = "Skipped: Maintenance Window Active"
                log_entry.processing_time = time.time() - start_time
                db.session.commit()
                log_to_web("Webhook skipped (Maintenance Window Active)", "info", config.name, data=data)
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
            item = config.item
            priority = config.priority
            customer_id_default = config.customer_id_default
            description_template = config.description_template
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
            overridable_fields = ['summary', 'description', 'customer_id', 'ticket_type', 'subtype', 'item', 'priority', 'board', 'status', 'severity', 'impact']
            mapped_vals = {}
            for field in overridable_fields:
                if field in json_mapping:
                    val = resolve_jsonpath(data, json_mapping[field])
                    if val is not None: mapped_vals[field] = str(val)

            mapped_summary = mapped_vals.get('summary')
            mapped_description = mapped_vals.get('description')
            mapped_customer_id = mapped_vals.get('customer_id')
            
            if 'ticket_type' in mapped_vals: ticket_type = mapped_vals['ticket_type']
            if 'subtype' in mapped_vals: subtype = mapped_vals['subtype']
            if 'item' in mapped_vals: item = mapped_vals['item']
            if 'priority' in mapped_vals: priority = mapped_vals['priority']
            if 'board' in mapped_vals: board = mapped_vals['board']
            if 'status' in mapped_vals: status = mapped_vals['status']

            # 2. AI-Driven Routing (if enabled)
            if config.ai_routing_enabled:
                from .utils import call_llm
                # We provide context to the LLM about what we need
                ai_prompt = config.ai_prompt_template or f"Analyze this JSON alert and suggest the best ticket Priority. Return ONLY a JSON object like {{\"priority\": \"name\"}}. Payload: {json.dumps(data)}"
                ai_response = call_llm(ai_prompt)
                if ai_response:
                    try:
                        # Find JSON block in response if LLM added chatter
                        json_match = re.search(r'\{.*\}', ai_response, re.DOTALL)
                        if json_match:
                            ai_data = json.loads(json_match.group())
                            if ai_data.get('priority'): priority = ai_data['priority']
                            log_entry.matched_rule = (log_entry.matched_rule or "") + f" [AI: {ai_data}]"
                    except Exception as e:
                        logger.warning(f"Failed to parse AI response: {ai_response} | Error: {e}")

            # 3. Apply Regex Routing Rules (Regex overrides AI)
            for rule in routing_rules:
                rule_path = rule.get('path')
                rule_regex = rule.get('regex')
                rule_overrides = rule.get('overrides', {})
                
                if rule_path and rule_regex:
                    val = str(resolve_jsonpath(data, rule_path))
                    if re.search(rule_regex, val, re.IGNORECASE):
                        logger.info(f"Routing rule matched: {rule_regex} on {rule_path}", extra=extra)
                        log_entry.matched_rule = f"Match: {rule_regex} on {rule_path}"
                        if 'board' in rule_overrides: board = rule_overrides['board']
                        if 'status' in rule_overrides: status = rule_overrides['status']
                        if 'ticket_type' in rule_overrides: ticket_type = rule_overrides['ticket_type']
                        if 'subtype' in rule_overrides: subtype = rule_overrides['subtype']
                        if 'item' in rule_overrides: item = rule_overrides['item']
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
            
            # AI Summary Generation (if enabled)
            if config.ai_summary_enabled and not mapped_summary:
                from .utils import call_llm
                ai_prompt = f"Summarize this JSON alert into a single concise sentence (max 15 words) suitable for a technical ticket title. Payload: {json.dumps(data)}"
                ai_summary = call_llm(ai_prompt)
                if ai_summary:
                    # Strip quotes if LLM added them
                    ticket_summary = ai_summary.strip('"').strip("'")
                    log_entry.matched_rule = (log_entry.matched_rule or "") + " [AI Summary]"
                else:
                    ticket_summary = f"{prefix} {monitor_name}" if prefix else monitor_name
            else:
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
                    log_entry.action = "update"
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
                    log_entry.action = "update"
                    log_entry.ticket_id = ticket_id
                    db.session.commit()
                    return
                
                company_id_match = re.search(r'#CW(\w+)', monitor_name)
                company_id = mapped_customer_id or (company_id_match.group(1) if company_id_match else customer_id_default)
                
                if mapped_description:
                    description = mapped_description
                elif description_template:
                    description = description_template.replace('{{ monitor_name }}', monitor_name)\
                                                     .replace('{{ msg }}', msg)\
                                                     .replace('{{ request_id }}', request_id)
                    # Handle {$.path} in template
                    paths = re.findall(r'\{($.+?)\}', description)
                    for p in paths:
                        val = str(resolve_jsonpath(data, p))
                        description = description.replace('{' + p + '}', val)
                else:
                    description = f"Source: {monitor_name}\nMessage: {msg}\nRequest ID: {request_id}\nPayload: {data}"
                
                new_ticket = cw_client.create_ticket(
                    summary=ticket_summary, 
                    description=description, 
                    monitor_name=monitor_name, 
                    company_id=company_id, 
                    board=board, 
                    status=status, 
                    ticket_type=ticket_type, 
                    subtype=subtype, 
                    item=item, 
                    priority=priority,
                    severity=mapped_vals.get('severity'),
                    impact=mapped_vals.get('impact')
                )
                if new_ticket:
                    ticket_id = new_ticket['id']
                    redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
                    log_to_web(f"{alert_type} alert: Created NEW ticket (ID: {ticket_id})", "warning" if alert_type == "DOWN" else "info", config_name, data=data, ticket_id=ticket_id)
                    PSA_TASK_COUNT.labels(type='create', result='success').inc()
                    log_entry.action = "create"

                    # 4. Automated RCA Notes (if enabled)
                    if config.ai_rca_enabled:
                        from .utils import call_llm
                        rca_prompt = f"Analyze this technical alert and suggest 3 possible root causes and 3 troubleshooting steps. Be concise and technical. Payload: {json.dumps(data)}"
                        rca_response = call_llm(rca_prompt)
                        if rca_response:
                            note_text = f"--- AI AUTOMATED RCA & TROUBLESHOOTING ---\n\n{rca_response}"
                            cw_client.add_ticket_note(ticket_id, note_text, is_internal=True)
                            log_entry.matched_rule = (log_entry.matched_rule or "") + " [AI RCA]"
            
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
                        log_entry.action = "close"
                else:
                    log_to_web(f"UP alert: No open ticket to close for {monitor_name}", "success", config_name, data=data)
                    PSA_TASK_COUNT.labels(type='close', result='skipped').inc()

            PSA_TASK_DURATION.labels(type=alert_type).observe(time.time() - start_time)
            
            # Finalize SUCCESS
            log_entry.status = "processed"
            log_entry.ticket_id = ticket_id
            log_entry.processing_time = time.time() - start_time
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            log_entry.status = "failed"
            log_entry.error_message = str(e)
            log_entry.processing_time = time.time() - start_time
            db.session.commit()
            logger.error(f"Error handling webhook: {e}", extra=extra)
            raise e
