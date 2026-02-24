import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, cast

from celery import Celery, Task
from prometheus_client import Counter, Histogram

from .client import ConnectWiseClient
from .extensions import db, redis_client
from .metrics import log_psa_task, log_webhook_processed
from .models import WebhookConfig, WebhookLog
from .utils import log_to_web, resolve_jsonpath

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

    default_url = (
        f"redis://:{redis_password}@{redis_host}:{redis_port}/0"
        if redis_password
        else f"redis://{redis_host}:{redis_port}/0"
    )
    redis_url = os.environ.get("CELERY_BROKER_URL", default_url)

    celery = Celery(app_name, broker=redis_url, backend=redis_url)
    return celery


celery = make_celery("hookwise")
celery.conf.beat_schedule = {
    "cleanup-logs-daily": {
        "task": "hookwise.cleanup_logs",
        "schedule": 86400.0,  # Every 24 hours
    },
    "verify-health-every-15m": {
        "task": "hookwise.verify_endpoint_health",
        "schedule": 900.0,  # Every 15 minutes
    },
}

_app = None


class ContextTask(Task):  # type: ignore[misc]
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        global _app
        if _app is None:
            from . import create_app

            _app = create_app()
        with _app.app_context():
            try:
                return self.run(*args, **kwargs)
            except Exception:
                db.session.rollback()
                logger.exception("Celery task %s failed", self.name)
                raise
            finally:
                db.session.remove()


celery.Task = ContextTask


@celery.task(name="hookwise.run_llm_rca")  # type: ignore[untyped-decorator]
def run_llm_rca(config_id: str, payload: dict, ai_prompt_template: Optional[str]) -> dict:
    """Run LLM root cause analysis in background so the HTTP request returns immediately."""
    from .utils import call_llm

    rca_prompt = (
        "Analyze this technical alert and suggest 3 possible root causes and 3 troubleshooting "
        f"steps. Be concise and technical. Payload: {json.dumps(payload)}"
    )
    system_prompt = ai_prompt_template or (
        "You are a helpful assistant specialized in ConnectWise ticketing and alert analysis. "
        "Be concise and return only the requested value."
    )
    try:
        result = call_llm(rca_prompt, system_prompt=system_prompt)
        if result:
            return {"status": "ok", "rca": result}
        return {"status": "error", "rca": "LLM returned no response — check OLLAMA_HOST and model."}
    except Exception as e:
        logger.error("run_llm_rca task error: %s", e)
        return {"status": "error", "rca": f"LLM error: {type(e).__name__}"}


@celery.task(name="hookwise.cleanup_logs")  # type: ignore[untyped-decorator]
def cleanup_logs() -> None:
    """Remove logs older than retention period."""
    from datetime import datetime

    from .extensions import db
    from .models import WebhookLog

    retention_days_raw = redis_client.get("hookwise_log_retention_days")
    retention_days = (
        int(cast(bytes, retention_days_raw).decode())
        if retention_days_raw
        else int(os.environ.get("LOG_RETENTION_DAYS", 30))
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    deleted = WebhookLog.query.filter(WebhookLog.created_at < cutoff).delete()
    db.session.commit()
    logger.info(f"Cleaned up {deleted} log entries older than {retention_days} days.")


@celery.task(name="hookwise.verify_endpoint_health")  # type: ignore[untyped-decorator]
def verify_endpoint_health() -> None:
    """Validate endpoint configurations against ConnectWise."""
    try:
        # Fetch global metadata
        boards = cw_client.get_boards()
        if not boards:
            logger.warning("Skipping health check: Unable to fetch boards from CW.")
            return

        board_map = {b["name"]: b["id"] for b in boards}

        priorities = cw_client.get_priorities()
        priority_names = {p["name"] for p in priorities}

        status_cache: Dict[int, Any] = {}

        configs = WebhookConfig.query.filter_by(is_enabled=True).all()
        updates = 0

        for config in configs:
            errors = []

            # 1. Check Board
            if config.board:
                if config.board not in board_map:
                    errors.append(f"Board '{config.board}' not found")
                else:
                    # 2. Check Status
                    if config.status:
                        bid = board_map[config.board]
                        if bid not in status_cache:
                            statuses = cw_client.get_board_statuses(bid)
                            status_cache[bid] = {s["name"] for s in statuses}

                        if config.status not in status_cache[bid]:
                            errors.append(f"Status '{config.status}' not found")

            # 3. Check Priority
            if config.priority and config.priority not in priority_names:
                errors.append(f"Priority '{config.priority}' not found")

            # Determine Status
            new_status = "OK"
            new_msg = "Configuration validated"
            if errors:
                new_status = "ERROR"
                new_msg = " | ".join(errors)

            # Update if changed
            if config.config_health_status != new_status or config.config_health_message != new_msg:
                config.config_health_status = new_status
                config.config_health_message = new_msg
                updates += 1

        if updates > 0:
            db.session.commit()
            logger.info(f"Health verification completed. Updated {updates} configs.")

    except Exception as e:
        logger.error(f"Health verification task failed: {e}")
        db.session.rollback()


@celery.task(bind=True, name="hookwise.process_webhook", max_retries=5)  # type: ignore[untyped-decorator]
def process_webhook_task(
    self: Any,
    config_id: str,
    data: Dict[str, Any],
    request_id: str,
    source_ip: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    """Background task to process webhook logic."""
    try:
        handle_webhook_logic(
            config_id, data, request_id, source_ip=source_ip, retry_count=self.request.retries, headers=headers
        )
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
        jitter = random.uniform(0.8, 1.2)
        countdown = (2**self.request.retries) * jitter
        raise self.retry(exc=exc, countdown=countdown) from exc


def is_in_maintenance(config: WebhookConfig) -> bool:
    """Check if current time is within a maintenance window."""
    if not config.maintenance_windows:
        return False
    try:
        from datetime import timezone

        windows = json.loads(config.maintenance_windows)
        now = datetime.now(timezone.utc)
        now_time = now.time()
        now_weekday = now.strftime("%a")  # Mon, Tue, etc.

        for window in windows:
            w_type = window.get("type", "once")
            start_str = window.get("start")
            end_str = window.get("end")

            if not start_str or not end_str:
                continue

            if w_type == "once":
                # Simple format: {"type": "once", "start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z"}
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if start <= now <= end:
                    return True

            elif w_type in ["daily", "weekly"]:
                # Format: {"type": "daily", "start": "HH:mm", "end": "HH:mm"}
                # Check Weekday for Weekly
                if w_type == "weekly":
                    if now_weekday not in window.get("days", []):
                        continue

                # Parse times
                s_h, s_m = map(int, start_str.split(":"))
                e_h, e_m = map(int, end_str.split(":"))
                start_time = now.replace(hour=s_h, minute=s_m, second=0, microsecond=0).time()
                end_time = now.replace(hour=e_h, minute=e_m, second=0, microsecond=0).time()

                if start_time < end_time:
                    # Normal range within a single day
                    if start_time <= now_time <= end_time:
                        return True
                else:
                    # Overnight range (e.g., 22:00 to 02:00)
                    if now_time >= start_time or now_time <= end_time:
                        return True

    except Exception as e:
        logger.error(f"Error checking maintenance window: {e}")
    return False


def handle_webhook_logic(
    config_id: str,
    data: Dict[str, Any],
    request_id: str,
    source_ip: Optional[str] = None,
    retry_count: int = 0,
    headers: Optional[Dict[str, str]] = None,
) -> None:
    """Core logic: process webhook payload and route to ConnectWise."""
    from flask import current_app as app

    extra = {"request_id": request_id, "config_id": config_id}
    start_time = time.time()

    with app.app_context():
        config = WebhookConfig.query.get(config_id)
        if not config:
            logger.error(f"Config {config_id} not found", extra=extra)
            return
        # 1. Create or update Webhook History Log
        from .utils import mask_secrets

        log_entry = WebhookLog.query.filter_by(request_id=request_id).first()
        if not log_entry:
            log_entry = WebhookLog(
                config_id=config_id,
                request_id=request_id,
                payload=json.dumps(mask_secrets(data)),
                headers=json.dumps(mask_secrets(headers)) if headers else None,
                source_ip=source_ip,
                status="processing",
            )
            db.session.add(log_entry)

        log_entry.retry_count = retry_count
        if source_ip:
            config.last_ip = source_ip
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

            config.last_seen_at = datetime.now(timezone.utc)
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
            overridable_fields = [
                "summary",
                "description",
                "customer_id",
                "ticket_type",
                "subtype",
                "item",
                "priority",
                "board",
                "status",
                "severity",
                "impact",
            ]
            mapped_vals = {}
            for field in overridable_fields:
                if field in json_mapping:
                    mapping_val = json_mapping[field]
                    if isinstance(mapping_val, str) and " " in mapping_val:
                        # Tokenize: identify $-variable tokens vs literal text tokens
                        token_re = re.compile(r"(\$\S+|[^\s]+)")
                        tokens = token_re.findall(mapping_val)
                        # Resolve each token
                        resolved: list[tuple[str, bool]] = []  # (value, is_variable)
                        any_jsonpath_resolved = False
                        for tok in tokens:
                            if tok.startswith("$"):
                                r_val = resolve_jsonpath(data, tok)
                                if r_val is not None and str(r_val).strip():
                                    resolved.append((str(r_val).strip(), True))
                                    any_jsonpath_resolved = True
                                else:
                                    resolved.append(("", True))  # failed variable
                            else:
                                resolved.append((tok, False))  # literal
                        if any_jsonpath_resolved:
                            # Drop literals that are only adjacent to failed variables
                            output_parts = []
                            for i, (val, is_var) in enumerate(resolved):
                                if is_var:
                                    if val:
                                        output_parts.append(val)
                                else:
                                    # Include literal only if a neighbour variable resolved
                                    left_ok = any(resolved[j][0] and resolved[j][1] for j in range(i - 1, -1, -1))
                                    right_ok = any(
                                        resolved[j][0] and resolved[j][1]
                                        for j in range(i + 1, len(resolved))
                                    )
                                    if left_ok or right_ok:
                                        output_parts.append(val)
                            if output_parts:
                                mapped_vals[field] = " ".join(output_parts)
                    else:
                        mapped_raw = resolve_jsonpath(data, mapping_val)
                        if mapped_raw is not None:
                            mapped_vals[field] = str(mapped_raw)

            mapped_summary = mapped_vals.get("summary")
            mapped_description = mapped_vals.get("description")
            mapped_customer_id = mapped_vals.get("customer_id")

            if "ticket_type" in mapped_vals:
                ticket_type = mapped_vals["ticket_type"]
            if "subtype" in mapped_vals:
                subtype = mapped_vals["subtype"]
            if "item" in mapped_vals:
                item = mapped_vals["item"]
            if "priority" in mapped_vals:
                priority = mapped_vals["priority"]
            if "board" in mapped_vals:
                board = mapped_vals["board"]
            if "status" in mapped_vals:
                status = mapped_vals["status"]

            # 2. Apply Regex Routing Rules
            for rule in routing_rules:
                rule_path = rule.get("path")
                rule_regex = rule.get("regex")
                rule_overrides = rule.get("overrides", {})

                if rule_path and rule_regex:
                    val = str(resolve_jsonpath(data, rule_path))
                    if re.search(rule_regex, val, re.IGNORECASE):
                        logger.info(f"Routing rule matched: {rule_regex} on {rule_path}", extra=extra)
                        log_entry.matched_rule = f"Match: {rule_regex} on {rule_path}"
                        
                        if rule_overrides.get("drop"):
                            log_entry.status = "skipped"
                            log_entry.error_message = f"Skipped: Dropped by routing rule ({rule_regex})"
                            log_entry.processing_time = time.time() - start_time
                            db.session.commit()
                            log_to_web(
                                f"Webhook skipped (Dropped by routing rule: {rule_regex})",
                                "warning",
                                config_name,
                                data=data,
                            )
                            return

                        if "board" in rule_overrides:
                            board = rule_overrides["board"]
                        if "status" in rule_overrides:
                            status = rule_overrides["status"]
                        if "ticket_type" in rule_overrides:
                            ticket_type = rule_overrides["ticket_type"]
                        if "subtype" in rule_overrides:
                            subtype = rule_overrides["subtype"]
                        if "item" in rule_overrides:
                            item = rule_overrides["item"]
                        if "priority" in rule_overrides:
                            priority = rule_overrides["priority"]

            actual_val = str(resolve_jsonpath(data, trigger_field))
            monitor = data.get("monitor", {})
            monitor_name = monitor.get("name", data.get("title", data.get("name", "Unknown Source")))
            msg = data.get("msg", data.get("message", "No message"))

            open_triggers = [v.strip() for v in open_value.split(",") if v.strip()]
            close_triggers = [v.strip() for v in close_value.split(",") if v.strip()]

            if actual_val in open_triggers:
                alert_type = "DOWN"
            elif actual_val in close_triggers:
                alert_type = "UP"
            else:
                alert_type = "GENERIC"

            prefix = ticket_prefix or os.environ.get("CW_TICKET_PREFIX", "Alert:")
            
            if mapped_summary:
                ticket_summary = f"{prefix} {mapped_summary}" if prefix else mapped_summary
            else:
                ticket_summary = f"{prefix} {monitor_name}" if prefix else monitor_name
            
            if config.summary_remove_strings:
                for s in config.summary_remove_strings.split(","):
                    ticket_summary = ticket_summary.replace(s, "")
            
            if len(ticket_summary) > 99:
                ticket_summary = ticket_summary[:96] + "..."

            cache_key = f"{CACHE_PREFIX}{config_id}:{ticket_summary}"

            ticket_id = None
            if alert_type == "DOWN" or alert_type == "GENERIC":
                cached_val = cast(Optional[bytes], redis_client.get(cache_key))
                if cached_val:
                    ticket_id = int(cached_val.decode())
                    viable_key = f"{cache_key}:viable"
                    is_usable = False
                    
                    is_replay = request_id.startswith(("replay_", "test_"))
                    
                    if not is_replay and redis_client.get(viable_key):
                        is_usable = True
                    else:
                        ticket_data = cw_client.get_ticket(ticket_id)
                        if ticket_data is None:
                            # Transient failure: do not clear the cache, assume still viable
                            is_usable = True
                        else:
                            is_closed = ticket_data.get("closedFlag", False)
                            status_name = ticket_data.get("status", {}).get("name", "")
                            closed_statuses = {"Completed", "Cancelled", "Closed"}
                            if cw_client.status_closed:
                                closed_statuses.add(cw_client.status_closed)
                            if config.close_status:
                                closed_statuses.add(config.close_status)
                                
                            if not is_closed and status_name not in closed_statuses:
                                is_usable = True
                                if not is_replay:
                                    redis_client.set(viable_key, "1", ex=VIABILITY_TTL)

                    if is_usable:    
                        note_text = (
                            f"Duplicate {alert_type} alert detected. Updated details:\n"
                            f"Message: {msg}\nRequest ID: {request_id}"
                        )
                        cw_client.add_ticket_note(ticket_id, note_text)
                        log_to_web(
                            f"{alert_type} alert: Updated existing ticket (ID: {ticket_id})",
                            "warning" if alert_type == "DOWN" else "info",
                            config_name,
                            data=data,
                            ticket_id=ticket_id,
                        )
                        log_psa_task(task_type="create", result="updated")
                        log_webhook_processed(config_id=config_id, status="processed")
                        log_entry.status = "processed"
                        log_entry.action = "update"
                        log_entry.ticket_id = ticket_id
                        db.session.commit()
                        return
                    else:
                        # Ticket is closed/completed so we clear the cache
                        redis_client.delete(cache_key)
                        redis_client.delete(viable_key)
                        ticket_id = None

                existing_ticket = cw_client.find_open_ticket(ticket_summary, close_status=config.close_status)
                if existing_ticket:
                    ticket_id = existing_ticket["id"]
                    note_text = (
                        f"Duplicate {alert_type} alert found in CW. Updated details:\n"
                        f"Message: {msg}\nRequest ID: {request_id}"
                    )
                    cw_client.add_ticket_note(ticket_id, note_text)
                    log_to_web(
                        f"{alert_type} alert: Found and updated open ticket (ID: {ticket_id})",
                        "warning" if alert_type == "DOWN" else "info",
                        config_name,
                        data=data,
                        ticket_id=ticket_id,
                    )
                    redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
                    log_psa_task(task_type="create", result="updated")
                    log_webhook_processed(config_id=config_id, status="processed")
                    log_entry.status = "processed"
                    log_entry.action = "update"
                    log_entry.ticket_id = ticket_id
                    db.session.commit()
                    return

                company_id_match = re.search(r"#CW-?(\w+)", monitor_name)
                company_id = mapped_customer_id or (company_id_match.group(1) if company_id_match else None)

                # 3. Apply Global Mapping (TenantMap) if not yet resolved and enabled
                if not company_id and config.global_routing_enabled:
                    from .models import GlobalMapping

                    # Try common tenant fields
                    tenant_fields = ["Tenant", "tenant", "tenantId", "TenantId"]
                    tenant_val = None
                    for tf in tenant_fields:
                        tenant_raw = resolve_jsonpath(data, f"$.{tf}")
                        if not tenant_raw:
                            # Try nested commonly used paths like .TaskInfo.Tenant
                            tenant_raw = resolve_jsonpath(data, f"$.TaskInfo.{tf}")
                        if tenant_raw:
                            tenant_val = str(tenant_raw)
                            break

                    if tenant_val:
                        # 1. Try exact match
                        mapping = GlobalMapping.query.filter_by(tenant_value=tenant_val).first()
                        
                        # 2. Try wildcard matches if no exact match found
                        if not mapping:
                            import fnmatch

                            from sqlalchemy import or_

                            # Find all mappings that contain wildcards (* or ?)
                            wildcard_mappings = GlobalMapping.query.filter(
                                or_(
                                    GlobalMapping.tenant_value.like('%*%'),
                                    GlobalMapping.tenant_value.like('%?%')
                                )
                            ).all()
                            
                            # Check if the tenant value matches any of these wildcard patterns
                            for w_mapping in wildcard_mappings:
                                if w_mapping.tenant_value and fnmatch.fnmatch(tenant_val, w_mapping.tenant_value):
                                    mapping = w_mapping
                                    break
                        
                        # 3. Try LLM semantic match if still no match
                        if not mapping:
                            from .utils import call_llm
                            
                            # Get all companies from ConnectWise
                            companies = cw_client.get_companies()
                            if companies:
                                # Create a list of identifiers (typically 'identifier' or 'name')
                                available_companies = [
                                    str(c.get("identifier")) for c in companies if c.get("identifier")
                                ]
                                
                                if available_companies:
                                    companies_str = ", ".join(available_companies)
                                    llm_prompt = (
                                        f"Match this incoming tenant string: '{tenant_val}' to the best option "
                                        f"from this list of company identifiers from ConnectWise: {companies_str}. "
                                        "Respond with ONLY the exact string from the list that matches best. "
                                        "If none match reasonably well, reply with exactly 'NONE'."
                                    )
                                    llm_resp = call_llm(llm_prompt)
                                    if (
                                        llm_resp
                                        and llm_resp.strip() != "NONE"
                                        and llm_resp.strip() in available_companies
                                    ):
                                        company_id = llm_resp.strip()
                                        logger.info(
                                            f"LLM fallback matched: {tenant_val} -> {company_id}",
                                            extra=extra,
                                        )
                                        log_entry.matched_rule = (
                                            (log_entry.matched_rule or "")
                                            + f" [LLM Global: {tenant_val} -> {company_id}]"
                                        )

                        if mapping and not company_id:
                            company_id = mapping.company_id
                            logger.info(f"Global mapping matched: {tenant_val} -> {company_id}", extra=extra)
                            log_entry.matched_rule = (log_entry.matched_rule or "") + f" [Global: {tenant_val}]"

                # Fallback to default
                if not company_id:
                    company_id = customer_id_default

                # Sanitize data for substitution/logging
                safe_data = mask_secrets(data)

                if mapped_description:
                    description = mapped_description
                elif description_template:
                    description = (
                        description_template.replace("{{ monitor_name }}", monitor_name)
                        .replace("{{ msg }}", msg)
                        .replace("{{ request_id }}", request_id)
                    )
                    # Handle {$.path} in template
                    paths = re.findall(r"\{(\$.+?)\}", description)
                    for p in paths:
                        val = str(resolve_jsonpath(safe_data, p))
                        description = description.replace("{" + p + "}", val)
                else:
                    description = (
                        f"Source: {monitor_name}\n"
                        f"Message: {msg}\n"
                        f"Request ID: {request_id}\n"
                        f"Payload: {json.dumps(safe_data)}"
                    )

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
                    severity=mapped_vals.get("severity"),
                    impact=mapped_vals.get("impact"),
                )
                if not new_ticket:
                    raise Exception("Failed to create ticket: ConnectWise API returned an error.")
                
                ticket_id = new_ticket["id"]
                redis_client.set(cache_key, str(ticket_id), ex=CACHE_TTL)
                log_to_web(
                    f"{alert_type} alert: Created NEW ticket (ID: {ticket_id})",
                    "warning" if alert_type == "DOWN" else "info",
                    config_name,
                    data=data,
                    ticket_id=ticket_id,
                )
                PSA_TASK_COUNT.labels(type="create", result="success")  # Kept for dynamic registration if needed
                log_psa_task(task_type="create", result="success")
                log_entry.action = "create"

                # 4. Automated RCA Notes (Only triggered for NEW tickets to optimize LLM usage)
                if config.ai_rca_enabled:
                    from .utils import call_llm

                    rca_prompt = (
                        "Analyze this technical alert and suggest 3 possible root causes and 3 troubleshooting "
                        f"steps. Be concise and technical. Payload: {json.dumps(data)}"
                    )
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
                    if existing_ticket:
                        ticket_id = existing_ticket["id"]

                if ticket_id:
                    resolution = f"Resource {monitor_name} is back UP.\nMessage: {msg}\nID: {request_id}"
                    if cw_client.close_ticket(ticket_id, resolution, status_name=config.close_status):
                        redis_client.delete(cache_key)
                        log_to_web(
                            f"UP alert: Closed ticket (ID: {ticket_id})",
                            "success",
                            config_name,
                            data=data,
                            ticket_id=ticket_id,
                        )
                        PSA_TASK_COUNT.labels(type="close", result="success")
                        log_psa_task(task_type="close", result="success")
                        log_entry.action = "close"
                else:
                    log_to_web(
                        f"UP alert: No open ticket to close for {monitor_name}", "success", config_name, data=data
                    )
                    log_psa_task(task_type="close", result="skipped")

            PSA_TASK_DURATION.labels(type=alert_type).observe(time.time() - start_time)

            # Finalize SUCCESS
            log_webhook_processed(config_id=config_id, status="processed")
            log_entry.status = "processed"
            log_entry.ticket_id = ticket_id
            log_entry.processing_time = time.time() - start_time
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            log_webhook_processed(config_id=config_id, status="failed")
            log_entry.status = "failed"

            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    # Capture response body if available (e.g., from requests)
                    error_msg += f" | Details: {e.response.text}"
                except Exception:
                    pass

            log_entry.error_message = error_msg
            log_entry.processing_time = time.time() - start_time
            db.session.commit()
            logger.error(f"Error handling webhook: {error_msg}", extra=extra)
            raise e
