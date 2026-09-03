import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, cast

from celery import Celery, Task
from celery.exceptions import Retry as CeleryRetry
from prometheus_client import Counter, Histogram

from .client import ConnectWiseClient, ConnectWiseError, TicketCreationRejected, TicketNotFoundError
from .extensions import build_redis_uri, db, redis_client
from .metrics import log_psa_task, log_webhook_processed
from .models import GlobalMapping, WebhookConfig, WebhookLog, WebhookRetryAttempt
from .services.routing import evaluate_routing
from .services.ticket_operations import (
    TicketOperationInProgress,
    complete,
    may_take_over,
    release,
    reserve,
    seconds_until_takeover,
)
from .utils import (
    CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY,
    filter_cipp_app_certificate_expiry_results,
    format_cipp_results,
    log_to_web,
    parse_cipp_app_certificate_exclude_patterns,
    resolve_jsonpath,
    resolve_monitor_name,
)

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

_ERROR_SECRET_RE = re.compile(r"(?i)(authorization|token|password|secret|api[-_ ]?key)\s*([:=])\s*[^\s,;]+")


def check_failure_threshold(config_id: str) -> None:
    """Nr. 18: je-Endpoint-Schwelle pruefen und ggf. an den Health-Webhook melden.

    Wird nach jedem als failed/dlq markierten Log gerufen. Nebenbefund beim
    Bau: der in den Einstellungen hinterlegte Health-Webhook wurde bisher von
    keiner Stelle beschickt -- dies ist sein erster Konsument.
    Die Funktion darf die Verarbeitung nie mitreissen: alles gekapselt,
    Fehler landen nur im Log.
    """
    try:
        config = WebhookConfig.query.get(config_id)
        if config is None or not config.notify_failure_threshold:
            return
        fenster = max(1, int(config.notify_window_minutes or 60))
        jetzt = datetime.now(timezone.utc)
        seit = jetzt - timedelta(minutes=fenster)
        letzte = config.last_threshold_alert_at
        if letzte is not None:
            letzte = letzte.replace(tzinfo=timezone.utc) if letzte.tzinfo is None else letzte
            if letzte > seit:
                return  # im aktuellen Fenster schon gemeldet
        anzahl = (
            WebhookLog.query.filter(
                WebhookLog.config_id == config_id,
                WebhookLog.status.in_(["failed", "dlq"]),
                WebhookLog.created_at >= seit,
            ).count()
        )
        if anzahl < int(config.notify_failure_threshold):
            return
        ziel = redis_client.get("hookwise_health_webhook")
        ziel = ziel.decode() if isinstance(ziel, bytes) else (ziel or "")
        if not ziel:
            # Kein Health-Webhook konfiguriert: NICHT drosseln -- sonst wird
            # der erste Alarm nach dem Eintragen der URL ein Fenster lang
            # verschluckt.
            return
        # Erst Drossel-Stempel setzen und committen, DANN senden: der Versand
        # haelt so weder die DB-Transaktion offen, noch kann eine kaputte URL
        # die Drossel umgehen.
        config.last_threshold_alert_at = jetzt
        db.session.commit()
        try:
            import requests

            requests.post(
                ziel,
                json={"content": (
                    f"HookWise: endpoint '{config.name}' reached {anzahl} failures "
                    f"in {fenster} minutes (threshold {config.notify_failure_threshold})."
                )},
                timeout=5,
            )
        except Exception:
            logger.warning("Threshold alert webhook unreachable (throttled anyway)")
        logger.info(
            f"Failure threshold alert for {config.name}: {anzahl} failures in {fenster}m"
        )
    except Exception:
        db.session.rollback()
        logger.exception("Failure-threshold check failed (non-fatal)")

def _sanitize_error(error: BaseException | str) -> str:
    """Bound error text before retaining it in history or a retry attempt."""
    message = str(error)
    message = _ERROR_SECRET_RE.sub(r"\1\2***", message)
    return message[:2000]


def _append_error_chain(log_entry: WebhookLog, error: BaseException | str, retry_count: int) -> None:
    """Append structured, sanitized failure metadata without overwriting evidence."""
    try:
        chain = json.loads(log_entry.error_chain or "[]")
        if not isinstance(chain, list):
            chain = []
    except TypeError, ValueError:
        chain = []
    chain.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "type": type(error).__name__ if isinstance(error, BaseException) else "Error",
            "message": _sanitize_error(error),
            "retry_count": retry_count,
        }
    )
    log_entry.error_chain = json.dumps(chain[-20:])
    log_entry.error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"


def _bounded_retry_policy(config: Optional[WebhookConfig], default_max_retries: int) -> tuple[bool, int, int, int]:
    """Return a safe retry policy even for legacy or malformed endpoint settings."""
    if config is None:
        return True, default_max_retries, 1, 300
    try:
        max_attempts = max(0, min(int(config.retry_max_attempts), 20))
        base_delay = max(1, min(int(config.retry_base_delay_seconds), 3600))
        max_delay = max(base_delay, min(int(config.retry_max_delay_seconds), 86400))
    except TypeError, ValueError:
        return bool(config.retry_enabled), default_max_retries, 1, 300
    return bool(config.retry_enabled), max_attempts, base_delay, max_delay


def _load_current_endpoint(config_id: str) -> Optional[WebhookConfig]:
    """Load endpoint settings from the database, bypassing the worker identity cache."""
    statement = db.select(WebhookConfig).where(WebhookConfig.id == config_id).execution_options(populate_existing=True)
    return db.session.execute(statement).scalar_one_or_none()


def _add_ticket_note_once(
    log_entry: WebhookLog,
    ticket_id: int,
    text: str,
    *,
    operation_name: str,
    is_internal: bool = False,
) -> bool:
    """Protect ConnectWise note POSTs against worker redelivery."""
    operation, acquired = reserve(log_entry.id, operation_name)
    if not acquired and operation.status == "completed":
        return True
    if not acquired and not may_take_over(operation):
        raise TicketOperationInProgress(
            f"{operation_name} is already owned by another worker",
            retry_after_seconds=seconds_until_takeover(operation),
        )
    result = cw_client.add_ticket_note(ticket_id, text, is_internal=is_internal)
    if result:
        complete(operation, ticket_id)
    else:
        release(operation)
    return result


cw_client = ConnectWiseClient()
_cached_mappings = None
_last_cache_update = 0.0
CACHE_REFRESH_INTERVAL = 300  # 5 minutes


def get_all_global_mappings() -> list[dict[str, Any]]:
    """Retrieve all GlobalMapping records as dicts, cached with TTL to avoid N+1 queries."""
    global _cached_mappings, _last_cache_update
    now = time.time()
    if _cached_mappings is None or (now - _last_cache_update) > CACHE_REFRESH_INTERVAL:
        mappings = GlobalMapping.query.all()
        _cached_mappings = [m.to_dict() for m in mappings]
        _last_cache_update = now
    return _cached_mappings


def make_celery(app_name: str) -> Celery:
    redis_password = os.environ.get("REDIS_PASSWORD")
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = os.environ.get("REDIS_PORT", 6379)

    default_url = build_redis_uri(redis_password, redis_host, redis_port, db=0)
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
    "check-timeouts-every-30m": {
        "task": "hookwise.check_webhook_timeouts",
        "schedule": 1800.0,  # Every 30 minutes
    },
    "dispatch-delivery-outbox": {
        "task": "hookwise.dispatch_delivery_outbox",
        "schedule": 10.0,
    },
}

# Execution guards: a hung ConnectWise/LLM call must not pin a worker forever.
# The soft limit raises SoftTimeLimitExceeded (catchable for cleanup); the hard
# limit force-kills the task. Defaults are generous enough for slow LLM RCA runs
# and are overridable via env. Parsing is defensive (mirrors VIABILITY_TTL above):
# a malformed env value falls back to the default instead of crashing worker
# startup at import time, and the soft limit is kept strictly below the hard limit
# (Celery requires soft < hard for the soft limit to ever fire).
_raw_soft_limit = os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "120")
_soft_time_limit = max(1, int(_raw_soft_limit)) if _raw_soft_limit.isdigit() else 120
_raw_hard_limit = os.environ.get("CELERY_TASK_TIME_LIMIT", "300")
_hard_time_limit = max(1, int(_raw_hard_limit)) if _raw_hard_limit.isdigit() else 300
if _soft_time_limit >= _hard_time_limit:
    logger.warning(
        "CELERY_TASK_SOFT_TIME_LIMIT (%s) must be less than CELERY_TASK_TIME_LIMIT (%s); "
        "clamping the soft limit below the hard limit.",
        _soft_time_limit,
        _hard_time_limit,
    )
    _soft_time_limit = _hard_time_limit - 1
celery.conf.task_soft_time_limit = _soft_time_limit
celery.conf.task_time_limit = _hard_time_limit

# LLM inference on CPU can legitimately run longer than the general task
# guard, especially while Ollama loads a model for the first request. Keep the
# request timeout and Celery limits aligned so Celery does not kill a healthy
# inference before requests has a chance to report its result.
_raw_llm_timeout = os.environ.get("LLM_TIMEOUT", "900")
LLM_REQUEST_TIMEOUT = max(1, int(_raw_llm_timeout)) if _raw_llm_timeout.isdigit() else 900
LLM_TASK_SOFT_TIME_LIMIT = LLM_REQUEST_TIMEOUT + 15
LLM_TASK_TIME_LIMIT = LLM_TASK_SOFT_TIME_LIMIT + 15

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
            except CeleryRetry:
                # Celery uses this control-flow exception for an expected retry;
                # the worker will emit the concise retry event itself.
                db.session.rollback()
                raise
            except Exception:
                db.session.rollback()
                logger.exception("Celery task %s failed", self.name)
                raise
            finally:
                db.session.remove()


celery.Task = ContextTask


@celery.task(
    name="hookwise.run_llm_rca",
    soft_time_limit=LLM_TASK_SOFT_TIME_LIMIT,
    time_limit=LLM_TASK_TIME_LIMIT,
)  # type: ignore[untyped-decorator]
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


@celery.task(
    name="hookwise.run_llm_test",
    soft_time_limit=LLM_TASK_SOFT_TIME_LIMIT,
    time_limit=LLM_TASK_TIME_LIMIT,
)  # type: ignore[untyped-decorator]
def run_llm_test(prompt: str) -> dict[str, str]:
    """Run an administrator diagnostic without holding an HTTP connection open."""
    from .utils import call_llm

    try:
        result = call_llm(prompt)
        if result:
            return {"status": "success", "result": result}
        return {"status": "error", "message": "LLM call failed or returned empty result"}
    except Exception as e:
        logger.error("run_llm_test task error: %s", e)
        return {"status": "error", "message": f"LLM error: {type(e).__name__}"}


@celery.task(name="hookwise.dispatch_delivery_outbox")  # type: ignore[untyped-decorator]
def dispatch_delivery_outbox() -> dict[str, int]:
    """Retry durable task intents that could not reach the broker after commit."""
    from .services.delivery_queue import dispatch_pending

    dispatched, failed = dispatch_pending()
    return {"dispatched": dispatched, "failed": failed}


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

        configs = WebhookConfig.query.filter_by(is_enabled=True).all()

        # Pre-populate status cache to avoid N+1 API calls
        status_cache: Dict[int, Any] = {}
        unique_bids = {board_map[c.board] for c in configs if c.board and c.board in board_map and c.status}

        if unique_bids:
            bid_list = list(unique_bids)
            keys = [f"hookwise_cw_statuses_{bid}" for bid in bid_list]
            try:
                cached_data: List[Optional[bytes]] = cast(List[Optional[bytes]], redis_client.mget(keys))
            except Exception as e:
                logger.warning(f"Redis MGET failed in health check: {e}")
                cached_data = [None] * len(bid_list)

            for i, bid in enumerate(bid_list):
                raw = cached_data[i]
                if raw:
                    try:
                        statuses = json.loads(raw)
                        status_cache[bid] = {s["name"] for s in statuses}
                    except json.JSONDecodeError, TypeError:
                        pass

                if bid not in status_cache:
                    # Fallback to API if not in Redis
                    statuses = cw_client.get_board_statuses(bid)
                    if statuses:
                        status_cache[bid] = {s["name"] for s in statuses}
                        # Update global cache (aligns with API route cache)
                        try:
                            redis_client.set(f"hookwise_cw_statuses_{bid}", json.dumps(statuses), ex=3600)
                        except Exception:
                            pass
                    else:
                        status_cache[bid] = set()

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
                        # status_cache is already populated
                        if config.status not in status_cache.get(bid, set()):
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


@celery.task(name="hookwise.check_webhook_timeouts")  # type: ignore[untyped-decorator]
def check_webhook_timeouts() -> None:
    """Check for endpoints that have not received data within the configured timeout period."""
    from .extensions import db
    from .models import WebhookConfig

    try:
        # Only check enabled, non-draft endpoints with timeout alerts enabled
        configs = WebhookConfig.query.filter_by(timeout_alerts_enabled=True, is_enabled=True, is_draft=False).all()
        logger.info(f"Starting timeout check for {len(configs)} endpoints with alerts enabled.")

        updates = 0
        now = datetime.now(timezone.utc)

        for config in configs:
            try:
                # Fallback to created_at if last_seen_at is None
                last_activity = config.last_seen_at or config.created_at

                if not last_activity:
                    logger.debug(f"Skipping '{config.name}': No activity date recorded yet.")
                    continue

                # SQLite might return naive datetimes, ensure timezone-aware comparison
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=timezone.utc)

                diff = now - last_activity
                hours_since_activity = diff.total_seconds() / 3600

                # Defensive check: Ensure timeout_hours is an int
                timeout_limit = getattr(config, "timeout_hours", 24) or 24

                logger.debug(
                    f"Checking '{config.name}': {hours_since_activity:.2f}h since activity "
                    f"(Alert Threshold: {timeout_limit}h)"
                )

                if hours_since_activity > timeout_limit:
                    # Determine if it's time to send/repeat an alert
                    is_first_alert = config.last_stale_alert_at is None
                    is_repeat_alert = False
                    if not is_first_alert:
                        # Defensive check: ensure last_stale_alert_at is timezone-aware
                        alert_at = config.last_stale_alert_at
                        if alert_at.tzinfo is None:
                            alert_at = alert_at.replace(tzinfo=timezone.utc)
                        time_since_alert = now - alert_at
                        if time_since_alert.total_seconds() / 3600 >= timeout_limit:
                            is_repeat_alert = True
                        else:
                            logger.debug(
                                f"Stale endpoint '{config.name}' already has an open alert and "
                                f"has not reached the repeat interval yet ({timeout_limit}h)."
                            )

                    if is_first_alert or is_repeat_alert:
                        # Verify if an existing timeout ticket was closed so we can raise another
                        if config.timeout_ticket_id:
                            try:
                                ticket_data = cw_client.get_ticket(config.timeout_ticket_id)
                                if ticket_data and ticket_data.get("closedFlag", False):
                                    logger.info(
                                        f"Timeout ticket #{config.timeout_ticket_id} for '{config.name}' "
                                        "was manually closed. Clearing to allow a new alert."
                                    )
                                    config.timeout_ticket_id = None
                                    db.session.commit()
                            except TicketNotFoundError:
                                logger.info(
                                    f"Timeout ticket #{config.timeout_ticket_id} for '{config.name}' "
                                    "was deleted or no longer exists. Clearing to allow a new alert."
                                )
                                config.timeout_ticket_id = None
                            except ConnectWiseError as e:
                                logger.warning(f"Transient error looking up timeout ticket: {e}")

                        import time

                        from .models import WebhookLog
                        from .utils import log_audit, log_to_web

                        if not config.timeout_ticket_id:
                            # No ticket open yet (or was closed), create one
                            summary = f"[TIMEOUT] Webhook Endpoint: {config.name} - No data for {config.timeout_hours}h"
                            description = (
                                f"The webhook endpoint '{config.name}' has not received any data for over "
                                f"{config.timeout_hours} hours.\n"
                                f"Last seen: {config.last_seen_at if config.last_seen_at else 'Never'}\n"
                                f"Endpoint ID: {config.id}"
                            )

                            new_ticket = cw_client.create_ticket(
                                summary=summary,
                                description=description,
                                monitor_name=config.name,
                                company_id=config.customer_id_default,
                                board=config.board,
                                status=config.status,
                                priority=config.priority,
                                ticket_type=config.ticket_type,
                                subtype=config.subtype,
                                item=config.item,
                            )

                            if new_ticket:
                                config.timeout_ticket_id = new_ticket["id"]
                                config.last_stale_alert_at = now
                                updates += 1
                                logger.warning(
                                    f"Created timeout ticket #{config.timeout_ticket_id} for endpoint '{config.name}'"
                                )
                                log_msg = (
                                    f"Timeout alert: Created ticket #{config.timeout_ticket_id} "
                                    f"(No data for {config.timeout_hours}h)"
                                )
                                log_to_web(log_msg, "warning", config.name)
                                log_audit(
                                    "timeout_alert",
                                    config.id,
                                    f"Created timeout ticket #{config.timeout_ticket_id}",
                                    commit=False,
                                )

                                req_id = f"timeout-{int(time.time())}"
                                log_entry = WebhookLog(
                                    config_id=config.id,
                                    request_id=req_id,
                                    payload=json.dumps(
                                        {"alert": "stale_endpoint", "timeout_hours": config.timeout_hours}
                                    ),
                                    status="processed",
                                    action="create",
                                    ticket_id=config.timeout_ticket_id,
                                    source_ip="system",
                                    error_message=f"Created timeout ticket #{config.timeout_ticket_id}",
                                )
                                db.session.add(log_entry)
                                db.session.commit()
                            else:
                                logger.warning(f"Failed to create timeout ticket for endpoint '{config.name}'.")
                                log_to_web(
                                    f"Timeout alert failure: Could not create ticket for {config.name}",
                                    "error",
                                    config.name,
                                )
                                log_audit(
                                    "timeout_error",
                                    config.id,
                                    "Failed to create timeout ticket in CW API",
                                    commit=False,
                                )

                                req_id = f"timeout-err-{int(time.time())}"
                                log_entry = WebhookLog(
                                    config_id=config.id,
                                    request_id=req_id,
                                    payload=json.dumps(
                                        {"alert": "stale_endpoint", "timeout_hours": config.timeout_hours}
                                    ),
                                    status="failed",
                                    action="create",
                                    source_ip="system",
                                    error_message="Failed to create ticket in ConnectWise API.",
                                )
                                db.session.add(log_entry)
                                db.session.commit()
                        else:
                            # Ticket is already open, add a repeating alert note
                            note_text = (
                                f"[REPEAT ALERT] Endpoint '{config.name}' is still stale.\n"
                                f"No data received for over {hours_since_activity:.1f} hours.\n"
                                f"Threshold: {config.timeout_hours}h"
                            )
                            req_id = f"timeout-repeat-{int(time.time())}"
                            try:
                                note_success = cw_client.add_ticket_note(config.timeout_ticket_id, note_text)
                                if note_success:
                                    config.last_stale_alert_at = now
                                    updates += 1
                                    logger.info(
                                        f"Added repeat alert note to ticket #{config.timeout_ticket_id} "
                                        f"for '{config.name}'"
                                    )
                                    log_to_web(
                                        f"Timeout alert repeated: Added note to ticket #{config.timeout_ticket_id}",
                                        "warning",
                                        config.name,
                                    )

                                    log_entry = WebhookLog(
                                        config_id=config.id,
                                        request_id=req_id,
                                        payload=json.dumps(
                                            {
                                                "alert": "stale_endpoint_repeat",
                                                "timeout_hours": config.timeout_hours,
                                                "timeout_ticket_id": config.timeout_ticket_id,
                                                "last_stale_alert_at": (
                                                    config.last_stale_alert_at.isoformat()
                                                    if config.last_stale_alert_at
                                                    else None
                                                ),
                                                "created_at": config.created_at.isoformat(),
                                                "hours_stale": round(hours_since_activity, 2),
                                            }
                                        ),
                                        status="processed",
                                        action="update",
                                        ticket_id=config.timeout_ticket_id,
                                        source_ip="system",
                                        error_message=f"Added repeat alert note to ticket #{config.timeout_ticket_id}",
                                    )
                                    db.session.add(log_entry)
                                    db.session.commit()
                                else:
                                    logger.error(
                                        f"Failed to add repeat alert note to ticket #{config.timeout_ticket_id} "
                                        f"for endpoint {config.id}: ConnectWise API returned False"
                                    )
                                    log_entry = WebhookLog(
                                        config_id=config.id,
                                        request_id=req_id,
                                        payload=json.dumps(
                                            {
                                                "alert": "stale_endpoint_repeat_failed",
                                                "timeout_hours": config.timeout_hours,
                                                "timeout_ticket_id": config.timeout_ticket_id,
                                                "hours_stale": round(hours_since_activity, 2),
                                            }
                                        ),
                                        status="failed",
                                        action="update",
                                        ticket_id=config.timeout_ticket_id,
                                        source_ip="system",
                                        error_message=(
                                            f"ConnectWise API failed to add repeat alert note to ticket "
                                            f"#{config.timeout_ticket_id}"
                                        ),
                                    )
                                    db.session.add(log_entry)
                                    db.session.commit()
                            except Exception as note_e:
                                logger.error(
                                    f"Failed to add repeat alert note to ticket #{config.timeout_ticket_id}: {note_e}"
                                )
            except Exception as loop_e:
                logger.error(f"Error processing timeout for endpoint '{config.name}': {loop_e}")
                db.session.rollback()

        if updates > 0:
            logger.info(f"Timeout check completed. Updated {updates} configuration(s)/ticket(s).")
        else:
            logger.info("Timeout check completed. No alerts written; endpoints may be stale or pending repeat.")

    except Exception as e:
        logger.error(f"Webhook timeout check task failed: {e}")
        db.session.rollback()


@celery.task(bind=True, name="hookwise.process_webhook", max_retries=5)  # type: ignore[untyped-decorator]
def process_webhook_task(
    self: Any,
    config_id: str,
    data: Dict[str, Any],
    request_id: str,
    source_ip: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    log_id: Optional[str] = None,
) -> None:
    """Process a delivery with endpoint-specific bounded retries and lineage."""
    retries = int(getattr(self.request, "retries", 0) or 0)
    log_entry: Optional[WebhookLog] = None
    attempt: Optional[WebhookRetryAttempt] = None
    # Workers are long-lived while endpoint edits are committed by a different
    # web process. Force a round trip so a replay or retry never uses an object
    # cached before the endpoint's authentication or delivery settings changed.
    config = _load_current_endpoint(config_id)
    if log_id:
        log_entry = WebhookLog.query.get(log_id)
    if log_entry is None:
        # Compatibility for queued work created before stable log IDs existed.
        log_entry = (
            WebhookLog.query.filter_by(config_id=config_id, request_id=request_id)
            .order_by(WebhookLog.created_at.desc())
            .first()
        )

    if log_entry is not None:
        now = datetime.now(timezone.utc)
        if log_entry.processing_started_at is None:
            log_entry.processing_started_at = now
        log_entry.status = "processing"
        attempt = WebhookRetryAttempt(
            log_id=log_entry.id,
            attempt_number=retries + 1,
            started_at=now,
            status="processing",
        )
        db.session.add(attempt)
        db.session.commit()

    if config is None:
        now = datetime.now(timezone.utc)
        if log_entry is not None:
            log_entry.status = "skipped"
            log_entry.error_type = "endpoint_deleted"
            log_entry.error_message = "Endpoint was deleted before the queued delivery was processed."
            log_entry.completed_at = now
        if attempt is not None:
            attempt.status = "skipped"
            attempt.error_message = "Endpoint was deleted before processing."
            attempt.completed_at = now
        db.session.commit()
        return

    try:
        handle_webhook_logic(
            config_id,
            data,
            request_id,
            source_ip=source_ip,
            retry_count=retries,
            headers=headers,
            log_id=log_entry.id if log_entry else None,
        )
        if attempt is not None:
            attempt.status = "processed"
            attempt.completed_at = datetime.now(timezone.utc)
            db.session.commit()
    except Exception as exc:
        enabled, max_attempts, base_delay, max_delay = _bounded_retry_policy(
            config, int(getattr(self, "max_retries", 5) or 5)
        )
        non_retryable = isinstance(exc, TicketCreationRejected) and not exc.retryable
        exhausted = non_retryable or not enabled or (retries + 1) >= max_attempts
        safe_error = _sanitize_error(exc)
        logger.error("Task failed (attempt %s/%s): %s", retries + 1, max_attempts, safe_error)
        if log_entry is not None:
            _append_error_chain(log_entry, exc, retries)
            log_entry.retry_count = retries
            log_entry.error_message = safe_error
            log_entry.error_type = type(exc).__name__
            log_entry.completed_at = datetime.now(timezone.utc) if exhausted else None
        if attempt is not None:
            attempt.completed_at = datetime.now(timezone.utc)
            attempt.error_message = safe_error

        if exhausted:
            if log_entry is not None:
                log_entry.status = "dlq"
                log_entry.retry_exhausted_at = datetime.now(timezone.utc)
                prefix = "Non-retryable failure" if non_retryable else "Max retries exceeded"
                log_entry.error_message = f"{prefix}: {safe_error}"
            if attempt is not None:
                attempt.status = "dlq"
            db.session.commit()
            check_failure_threshold(config_id)
            return

        jitter = random.uniform(0.8, 1.2)
        countdown = min(max_delay, base_delay * (2**retries)) * jitter
        requested_delay = getattr(exc, "retry_after_seconds", None)
        if isinstance(requested_delay, (int, float)):
            countdown = max(countdown, min(max_delay, max(1.0, float(requested_delay))))
        if log_entry is not None:
            log_entry.status = "retrying"
        if attempt is not None:
            attempt.status = "retry_scheduled"
            attempt.retry_interval_seconds = countdown
        db.session.commit()
        raise self.retry(exc=exc, countdown=countdown) from exc


def is_in_maintenance(config: WebhookConfig) -> bool:
    """Check if current time is within a maintenance window."""
    if not config.maintenance_windows:
        return False
    try:
        windows = json.loads(config.maintenance_windows)
        now = datetime.now(timezone.utc)

        for window in windows:
            if _is_window_active(window, now):
                return True
    except Exception as e:
        logger.error(f"Error checking maintenance window: {e}")
    return False


def _is_window_active(window: Dict[str, Any], now: datetime) -> bool:
    """Check if a specific maintenance window is active."""
    w_type = window.get("type", "once")
    start_str = window.get("start")
    end_str = window.get("end")

    if not start_str or not end_str:
        return False

    if w_type == "once":
        return _check_once_window(start_str, end_str, now)
    if w_type in ["daily", "weekly"]:
        return _check_recurring_window(window, w_type, start_str, end_str, now)
    return False


def _check_once_window(start_str: str, end_str: str, now: datetime) -> bool:
    """Check if a 'once' window is active."""
    try:
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return start <= now <= end
    except ValueError, TypeError:
        return False


def _check_recurring_window(window: Dict[str, Any], w_type: str, start_str: str, end_str: str, now: datetime) -> bool:
    """Check if a 'daily' or 'weekly' window is active."""
    if w_type == "weekly":
        now_weekday = now.strftime("%a")
        if now_weekday not in window.get("days", []):
            return False

    try:
        # Parse times
        s_h, s_m = map(int, start_str.split(":"))
        e_h, e_m = map(int, end_str.split(":"))
        now_time = now.time()
        start_time = now.replace(hour=s_h, minute=s_m, second=0, microsecond=0).time()
        end_time = now.replace(hour=e_h, minute=e_m, second=0, microsecond=0).time()

        if start_time < end_time:
            # Normal range within a single day
            return start_time <= now_time <= end_time
        # Overnight range (e.g., 22:00 to 02:00)
        return now_time >= start_time or now_time <= end_time
    except ValueError, AttributeError, TypeError:
        return False


def _resolve_timeout_alert(config: WebhookConfig) -> None:
    """Update heartbeat timestamp and close any open timeout tickets."""
    from .models import db

    config.last_seen_at = datetime.now(timezone.utc)
    config.last_stale_alert_at = None

    if config.timeout_ticket_id:
        ticket_id = config.timeout_ticket_id
        resolution = f"Webhook data received again for endpoint '{config.name}'. Automatically closing timeout alert."

        try:
            if cw_client.close_ticket(ticket_id, resolution, status_name=config.close_status):
                logger.info(f"Closed timeout ticket #{ticket_id} for endpoint '{config.name}'")
                log_to_web(f"Timeout alert resolved: Closed ticket #{ticket_id}", "success", config.name)

                import time

                from .models import WebhookLog
                from .utils import log_audit

                log_audit(
                    "timeout_resolve", config.id, f"Automatically closed timeout ticket #{ticket_id}", commit=False
                )
                log_entry = WebhookLog(
                    config_id=config.id,
                    request_id=f"timeout-resolved-{int(time.time())}",
                    payload=json.dumps({"alert": "timeout_resolved", "ticket_id": ticket_id}),
                    status="processed",
                    action="close",
                    ticket_id=ticket_id,
                    source_ip="system",
                )
                db.session.add(log_entry)
                config.timeout_ticket_id = None
            else:
                logger.warning(f"Failed to close timeout ticket #{ticket_id} for endpoint '{config.name}'. ")
        except TicketNotFoundError:
            logger.warning(
                f"Timeout ticket #{ticket_id} for endpoint '{config.name}' "
                "is already closed or deleted. Clearing ID to prevent deadlock."
            )
            config.timeout_ticket_id = None
        except ConnectWiseError as e:
            logger.error(f"Transient error closing timeout ticket #{ticket_id}: {e}")

    db.session.commit()


def handle_webhook_logic(
    config_id: str,
    data: Dict[str, Any],
    request_id: str,
    source_ip: Optional[str] = None,
    retry_count: int = 0,
    headers: Optional[Dict[str, str]] = None,
    log_id: Optional[str] = None,
) -> None:
    """Core logic: process webhook payload and route to ConnectWise."""
    from flask import current_app as app

    extra = {"request_id": request_id, "config_id": config_id}
    start_time = time.time()

    with app.app_context():
        config = _load_current_endpoint(config_id)
        if not config:
            logger.error(f"Config {config_id} not found", extra=extra)
            return
        # 1. Create or update Webhook History Log
        from .utils import mask_secrets

        log_entry = WebhookLog.query.get(log_id) if log_id else None
        if log_entry is None:
            log_entry = (
                WebhookLog.query.filter_by(config_id=config_id, request_id=request_id)
                .order_by(WebhookLog.created_at.desc())
                .first()
            )
        if not log_entry:
            now = datetime.now(timezone.utc)
            log_entry = WebhookLog(
                config_id=config_id,
                request_id=request_id,
                payload=json.dumps(data),
                headers=json.dumps(mask_secrets(headers)) if headers else None,
                source_ip=source_ip,
                status="processing",
                correlation_id=request_id[:100],
                received_at=now,
                queued_at=now,
                processing_started_at=now,
            )
            db.session.add(log_entry)

        log_entry.retry_count = retry_count
        if log_entry.processing_started_at is None:
            log_entry.processing_started_at = datetime.now(timezone.utc)
        log_entry.status = "processing"
        if source_ip:
            config.last_ip = source_ip
        db.session.commit()

        try:
            # 2. Check Maintenance Window
            if is_in_maintenance(config):
                # Ensure heartbeat is updated even in maintenance
                _resolve_timeout_alert(config)

                log_entry.status = "skipped"
                log_entry.error_message = "Skipped: Maintenance Window Active"
                log_entry.processing_time = time.time() - start_time
                db.session.commit()
                log_to_web("Webhook skipped (Maintenance Window Active)", "info", config.name, data=data)
                return

            config_name = config.name
            ticket_prefix = config.ticket_prefix
            board = config.board
            status = config.status
            ticket_type = config.ticket_type
            subtype = config.subtype
            item = config.item
            priority = config.priority
            customer_id_default = config.customer_id_default
            description_template = config.description_template

            # Heartbeat update and timeout resolution
            _resolve_timeout_alert(config)

            raw_task_info = data.get("TaskInfo")
            task_info = raw_task_info if isinstance(raw_task_info, dict) else {}
            exclude_patterns: tuple[str, ...] = ()
            if task_info.get("Command") == "Get-CIPPAlertAppCertificateExpiry":
                stored_exclude_patterns = redis_client.get(CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY)
                if isinstance(stored_exclude_patterns, bytes):
                    exclude_patterns = parse_cipp_app_certificate_exclude_patterns(
                        stored_exclude_patterns.decode("utf-8")
                    )
                elif isinstance(stored_exclude_patterns, str):
                    exclude_patterns = parse_cipp_app_certificate_exclude_patterns(stored_exclude_patterns)

            data, excluded_cipp_app_names = filter_cipp_app_certificate_expiry_results(data, exclude_patterns)
            if excluded_cipp_app_names:
                excluded_names_text = ", ".join(excluded_cipp_app_names)
                logger.info(
                    "Excluded %d CIPP application certificate-expiry result(s): %s",
                    len(excluded_cipp_app_names),
                    excluded_names_text,
                    extra=extra,
                )
                if not data.get("Results"):
                    log_entry.status = "skipped"
                    log_entry.error_message = "Skipped: All CIPP application results were globally excluded"
                    log_entry.processing_time = time.time() - start_time
                    db.session.commit()
                    log_to_web(
                        f"Webhook skipped (all CIPP applications excluded: {excluded_names_text})",
                        "info",
                        config_name,
                        data=data,
                    )
                    return

            routing_config = config.to_dict()
            routing_config["ticket_prefix"] = ticket_prefix or os.environ.get("CW_TICKET_PREFIX", "Alert:")
            decision = evaluate_routing(data, routing_config)
            mapped_vals = decision.values
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

            if decision.matched_rules:
                patterns = [str(rule["regex"]) for rule in decision.matched_rules]
                log_entry.matched_rule = "Matches: " + ", ".join(patterns)
                if mapped_vals.get("drop"):
                    log_entry.status = "skipped"
                    log_entry.error_message = f"Skipped: Dropped by routing rule ({patterns[-1]})"
                    log_entry.processing_time = time.time() - start_time
                    db.session.commit()
                    log_to_web(
                        f"Webhook skipped (Dropped by routing rule: {patterns[-1]})",
                        "warning",
                        config_name,
                        data=data,
                    )
                    return

            monitor_name = resolve_monitor_name(data)
            msg = data.get("msg", data.get("message", "No message"))
            alert_type = decision.alert_type
            prefix = str(routing_config["ticket_prefix"])
            ticket_summary = decision.summary

            if config.summary_remove_strings:
                for s in config.summary_remove_strings.split(","):
                    ticket_summary = ticket_summary.replace(s, "")

            if len(ticket_summary) > 99:
                ticket_summary = ticket_summary[:96] + "..."

            if not ticket_summary.strip():
                ticket_summary = f"{prefix} Summary unavailable" if prefix else "Summary unavailable"

            cache_key = f"{CACHE_PREFIX}{config_id}:{ticket_summary}"

            ticket_id = None
            log_entry.connectwise_started_at = datetime.now(timezone.utc)
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
                        _add_ticket_note_once(log_entry, ticket_id, note_text, operation_name="duplicate_note")
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
                    _add_ticket_note_once(log_entry, ticket_id, note_text, operation_name="duplicate_note")
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
                        all_mappings = get_all_global_mappings()
                        mapping = None

                        # 1. Try exact match (in-memory)
                        for m in all_mappings:
                            if m.get("tenant_value") == tenant_val:
                                mapping = m
                                break

                        # 2. Try wildcard matches if no exact match found (in-memory)
                        if not mapping:
                            import fnmatch

                            for w_mapping in all_mappings:
                                t_val = w_mapping.get("tenant_value")
                                if isinstance(t_val, str) and ("*" in t_val or "?" in t_val):
                                    if fnmatch.fnmatch(tenant_val, t_val):
                                        mapping = w_mapping
                                        break
                        # 3. Try LLM semantic match if still no match
                        if not mapping:
                            from .utils import call_llm

                            # Get all companies from ConnectWise
                            companies = cw_client.get_companies()
                            if companies:
                                # Create a list of identifiers (typically "identifier" or "name")
                                available_companies = [
                                    str(c.get("identifier")) for c in companies if c.get("identifier")
                                ]

                                if available_companies:
                                    companies_str = ", ".join(available_companies)
                                    llm_prompt = (
                                        f'Match this incoming tenant string: "{tenant_val}" to the best option '
                                        f"from this list of company identifiers from ConnectWise: {companies_str}. "
                                        "Respond with ONLY the exact string from the list that matches best. "
                                        'If none match reasonably well, reply with exactly "NONE".'
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
                                            log_entry.matched_rule or ""
                                        ) + f" [LLM Global: {tenant_val} -> {company_id}]"

                        if mapping and not company_id:
                            company_id = mapping.get("company_id")
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
                    if "{{ cipp_results }}" in description:
                        description = description.replace("{{ cipp_results }}", format_cipp_results(safe_data))
                else:
                    description = (
                        f"Source: {monitor_name}\n"
                        f"Message: {msg}\n"
                        f"Request ID: {request_id}\n"
                        f"Payload: {json.dumps(safe_data)}"
                    )

                operation, acquired = reserve(log_entry.id, "create_ticket")
                new_ticket: dict[str, Any] | None = None
                if not acquired and operation.status == "completed" and operation.ticket_id:
                    new_ticket = {"id": operation.ticket_id}
                elif not acquired:
                    recovered = cw_client.find_open_ticket(ticket_summary, close_status=config.close_status)
                    if recovered:
                        recovered_id = int(recovered["id"])
                        complete(operation, recovered_id)
                        new_ticket = {"id": recovered_id}
                    elif may_take_over(operation):
                        acquired = True
                    else:
                        raise TicketOperationInProgress(
                            "Ticket creation is already owned by another worker",
                            retry_after_seconds=seconds_until_takeover(operation),
                        )

                if acquired:
                    try:
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
                    except TicketCreationRejected:
                        release(operation)
                        raise
                if not new_ticket:
                    release(operation)
                    raise TicketCreationRejected(
                        "ConnectWise rejected ticket creation without a ticket response",
                        retryable=True,
                    )

                ticket_id = int(new_ticket["id"])
                if operation.status != "completed":
                    complete(operation, ticket_id)
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
                        _add_ticket_note_once(
                            log_entry,
                            ticket_id,
                            note_text,
                            operation_name="rca_note",
                            is_internal=True,
                        )
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
                    try:
                        close_operation, acquired = reserve(log_entry.id, "close_ticket")
                        if not acquired and close_operation.status == "completed":
                            success = True
                        elif not acquired and not may_take_over(close_operation):
                            raise TicketOperationInProgress(
                                "Ticket closure is already owned by another worker",
                                retry_after_seconds=seconds_until_takeover(close_operation),
                            )
                        else:
                            success = cw_client.close_ticket(ticket_id, resolution, status_name=config.close_status)
                            if success:
                                complete(close_operation, ticket_id)
                            else:
                                release(close_operation)
                        if success:
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
                                f"UP alert: Failed to close ticket (ID: {ticket_id})",
                                "error",
                                config_name,
                                data=data,
                                ticket_id=ticket_id,
                            )
                            PSA_TASK_COUNT.labels(type="close", result="failure")
                            log_psa_task(task_type="close", result="failure")
                            log_entry.action = "failed"
                    except TicketNotFoundError:
                        redis_client.delete(cache_key)
                        log_to_web(
                            f"UP alert: Ticket (ID: {ticket_id}) was already closed/missing",
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
            log_entry.connectwise_responded_at = datetime.now(timezone.utc)
            log_entry.completed_at = datetime.now(timezone.utc)
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            log_webhook_processed(config_id=config_id, status="failed")
            log_entry.status = "failed"

            error_msg = _sanitize_error(e)
            _append_error_chain(log_entry, e, retry_count)
            log_entry.error_message = error_msg
            log_entry.error_type = type(e).__name__
            log_entry.processing_time = time.time() - start_time
            db.session.commit()
            logger.error(f"Error handling webhook: {error_msg}", extra=extra)
            check_failure_threshold(config_id)
            raise e
        finally:
            # Early successful/skipped paths return before the normal finalizer.
            if log_entry.status in {"processed", "skipped"}:
                now = datetime.now(timezone.utc)
                if log_entry.completed_at is None:
                    log_entry.completed_at = now
                if log_entry.connectwise_started_at and log_entry.connectwise_responded_at is None:
                    log_entry.connectwise_responded_at = now
                if log_entry.connectwise_started_at and log_entry.connectwise_responded_at:
                    started = log_entry.connectwise_started_at
                    responded = log_entry.connectwise_responded_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if responded.tzinfo is None:
                        responded = responded.replace(tzinfo=timezone.utc)
                    log_entry.connectwise_duration_ms = int((responded - started).total_seconds() * 1000)
                db.session.commit()
