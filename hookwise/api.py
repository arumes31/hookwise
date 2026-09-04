"""History, statistics, endpoint diagnostics, and metrics handlers."""

import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any, Dict, Tuple, cast

from flask import Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy.orm import joinedload

from .extensions import csrf, db, limiter
from .models import AuditLog, User, WebhookConfig, WebhookLog
from .services.delivery_queue import commit_and_dispatch, stage_delivery
from .services.routing import routing_regex_matches
from .tasks import celery, cw_client, process_webhook_task, redis_client
from .utils import (
    CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY,
    auth_required,
    log_audit,
    log_to_web,
    mask_secrets,
    parse_cipp_app_certificate_exclude_patterns,
    resolve_jsonpath,
    resolve_monitor_name,
)

_DELIVERY_CONTROL_BOUNDS = {
    "rate_limit_per_minute": (60, 1, 10_000),
    "retry_max_attempts": (5, 0, 20),
    "retry_base_delay_seconds": (1, 1, 3_600),
    "retry_max_delay_seconds": (300, 1, 86_400),
}
_DELIVERY_CONTROL_FIELDS = frozenset({*_DELIVERY_CONTROL_BOUNDS, "retry_enabled"})


def _restore_delivery_control(field: str, value: Any) -> bool | int:
    """Normalize restored delivery controls with the endpoint form bounds."""
    if field == "retry_enabled":
        return value if isinstance(value, bool) else True
    default, minimum, maximum = _DELIVERY_CONTROL_BOUNDS[field]
    try:
        parsed = int(value) if not isinstance(value, bool) else default
    except TypeError, ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _routing_regex_matches(pattern: str, value: str) -> bool:
    """Compatibility wrapper for callers importing the legacy API helper."""
    return routing_regex_matches(pattern, value)


QUEUE_SIZE = Gauge("hookwise_celery_queue_size", "Approximate number of tasks in queue")


def _get_cached_connectwise_list(cache_key: str) -> str | None:
    """Return only a valid non-empty cached lookup list."""
    cached = redis_client.get(cache_key)
    if not cached:
        return None
    decoded = cast(bytes, cached).decode()
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    except TypeError:
        return None
    return decoded if isinstance(value, list) and value else None


def _parse_row_date(row_date: Any) -> date | None:
    """Parse a date object or string from a database row."""
    if isinstance(row_date, date):
        return row_date
    try:
        return date.fromisoformat(str(row_date).split(" ")[0])
    except ValueError:
        try:
            return datetime.strptime(str(row_date).split(" ")[0], "%Y-%m-%d").date()
        except ValueError as e:
            import logging

            logging.error(f"Failed to parse date '{row_date}': {e}")
            return None


def _get_group_key(d: date, period: str) -> str:
    """Generate a group key based on the period (daily, weekly, monthly)."""
    if period == "weekly":
        year, week, _ = d.isocalendar()
        return f"{year}-W{week}"
    elif period == "monthly":
        return d.strftime("%Y-%m")
    else:
        return d.strftime("%m-%d")


def _format_history_response(counts_by_group: dict[str, dict[str, int]], period: str) -> list[dict[str, Any]]:
    """Format and fill gaps in the history data for the response."""
    history_data: list[dict[str, Any]] = []
    now: date = datetime.now(timezone.utc).date()

    if period == "weekly":

        def generate_weeks(start_date: date, count: int):
            seen: set[tuple[int, int]] = set()
            for j in range(60):
                d = start_date - timedelta(days=j)
                year, week, _ = d.isocalendar()
                if (year, week) not in seen:
                    seen.add((year, week))
                    yield year, week
                    if len(seen) == count:
                        break

        weeks_data: list[dict[str, Any]] = []
        for year, week in generate_weeks(now, 4):
            k = f"{year}-W{week}"
            weeks_data.append(
                {
                    "date": f"W{week}",
                    "created": counts_by_group.get(k, {}).get("created", 0),
                    "updated": counts_by_group.get(k, {}).get("updated", 0),
                    "closed": counts_by_group.get(k, {}).get("closed", 0),
                }
            )
        history_data.extend(reversed(weeks_data))
    elif period == "monthly":
        for i in range(5, -1, -1):
            total_months = now.year * 12 + now.month - 1 - i
            y, m_0 = divmod(total_months, 12)
            m = m_0 + 1
            k = f"{y}-{m:02d}"
            month_name = datetime(y, m, 1).strftime("%b")
            history_data.append(
                {
                    "date": month_name,
                    "created": counts_by_group.get(k, {}).get("created", 0),
                    "updated": counts_by_group.get(k, {}).get("updated", 0),
                    "closed": counts_by_group.get(k, {}).get("closed", 0),
                }
            )
    else:
        for i in range(6, -1, -1):
            d = now - timedelta(days=i)
            k = d.strftime("%m-%d")
            history_data.append(
                {
                    "date": k,
                    "created": counts_by_group.get(k, {}).get("created", 0),
                    "updated": counts_by_group.get(k, {}).get("updated", 0),
                    "closed": counts_by_group.get(k, {}).get("closed", 0),
                }
            )
    return history_data


def _register() -> None:
    from .routes import main_bp

    # --- History & Logs ---

    @main_bp.route("/api/activity/history")
    @auth_required
    def get_activity_history() -> Any:
        logs = (
            WebhookLog.query.options(joinedload(WebhookLog.config))  # type: ignore[arg-type]
            .order_by(WebhookLog.created_at.desc())
            .limit(50)
            .all()
        )
        history = []
        for log in logs:
            # Reconstruct the message based on status and action
            # This mimics the log_to_web calls in tasks.py
            message = log.error_message or "Processed"
            level = "info"

            if log.status == "failed":
                message = log.error_message or "Unknown error"
                level = "error"
            elif log.status == "skipped":
                err_msg = log.error_message or "No action required"
                prefix = "Skipped: "
                # Prevent double-prefixing if the message already starts with "Skipped:"
                if err_msg.strip().startswith("Skipped:"):
                    message = err_msg
                else:
                    message = f"{prefix}{err_msg}"
                level = "info"
            elif log.status == "processed":
                if log.action == "create":
                    message = f"Created NEW ticket (ID: {log.ticket_id})"
                    level = "warning"
                elif log.action == "update":
                    if not log.error_message:
                        message = f"Updated existing ticket (ID: {log.ticket_id})"
                    level = "info"
                elif log.action == "close":
                    message = f"Closed ticket (ID: {log.ticket_id})"
                    level = "success"
                # Removed the dead 'skipped' action branch as it's handled by log.status

            payload_data = {"raw": log.payload}
            if log.payload and log.payload.startswith(("{", "[")):
                try:
                    payload_data = mask_secrets(json.loads(log.payload))
                except json.JSONDecodeError, TypeError:
                    pass

            history.append(
                {
                    "timestamp": log.created_at.isoformat(),
                    "message": message,
                    "level": level,
                    "config_name": log.config.name if log.config else "System",
                    "payload": payload_data,
                    "ticket_id": log.ticket_id,
                }
            )
        return jsonify(history)

    @main_bp.route("/api/activity/trigger-timeout-check", methods=["POST"])
    @auth_required
    def trigger_timeout_check() -> Any:
        from .tasks import check_webhook_timeouts

        try:
            # Trigger the task in the background
            task = check_webhook_timeouts.delay()
            return jsonify(
                {"status": "success", "message": "Manual timeout check triggered in background.", "task_id": task.id}
            )
        except Exception:
            current_app.logger.exception("Failed to enqueue timeout check")
            return jsonify({"status": "error", "message": "Failed to enqueue timeout check"}), 503

    @main_bp.route("/history")
    @auth_required
    def history() -> Any:
        page = request.args.get("page", 1, type=int)
        search = request.args.get("search", "")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        endpoint_id = request.args.get("endpoint_id", "")
        status = request.args.get("status", "")
        source_ip = request.args.get("source_ip", "")
        per_page = 25

        from .history_ops import _filter_logs

        try:
            query = _filter_logs(request.args)
        except ValueError as exc:
            flash(str(exc), "warning")
            query = WebhookLog.query.filter(db.false())

        if source_ip:
            query = query.filter(WebhookLog.source_ip.ilike(f"%{source_ip}%"))

        pagination = query.order_by(WebhookLog.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        debug_mode = os.environ.get("DEBUG_MODE", "false").lower() == "true"
        cw_url = os.environ.get("CW_URL", "https://api-na.myconnectwise.net/v4_6_release/apis/3.0").rstrip("/")

        all_configs = WebhookConfig.query.filter_by(is_draft=False).order_by(WebhookConfig.name).all()

        if request.args.get("partial") == "true":
            return render_template("history_rows.html", logs=pagination.items, cw_url=cw_url)

        return render_template(
            "history.html",
            pagination=pagination,
            logs=pagination.items,
            search=search,
            date_from=date_from,
            date_to=date_to,
            endpoint_id=endpoint_id,
            status=status,
            source_ip=source_ip,
            all_configs=all_configs,
            debug_mode=debug_mode,
            cw_url=cw_url,
        )

    @main_bp.route("/audit")
    @auth_required
    def audit_logs() -> Any:
        page = request.args.get("page", 1, type=int)
        per_page = 50
        pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        return render_template("audit.html", pagination=pagination, logs=pagination.items)

    @main_bp.route("/api/logs/<log_id>/replay", methods=["POST"])
    @main_bp.route("/history/replay/<log_id>", methods=["POST"])
    @auth_required
    def replay_webhook(log_id: str) -> Any:
        # Frueher die alte Ein-Rollen-Spalte; jetzt das Registry-Recht (history:retry).
        from .rbac.decorators import routen_recht_fehlt

        if fehlt := routen_recht_fehlt():
            return jsonify({"error": f"Missing permission: {fehlt}", "required": fehlt}), 403
        log_entry = WebhookLog.query.get_or_404(log_id)
        try:
            data = json.loads(log_entry.payload)
            request_id = f"replay_{int(time.time())}_{secrets.token_hex(4)}_{log_entry.request_id[:8]}"[:100]
            now = datetime.now(timezone.utc)
            replay_log = WebhookLog(
                config_id=log_entry.config_id,
                request_id=request_id,
                correlation_id=log_entry.correlation_id or log_entry.request_id[:100],
                payload=json.dumps(data),
                status="pending_enqueue",
                received_at=now,
                queued_at=now,
                replay_of_log_id=log_entry.id,
            )
            outbox = stage_delivery(replay_log, data)
            if not commit_and_dispatch(outbox, process_webhook_task):
                return jsonify({"status": "enqueue_failed", "message": "Replay retained for retry"}), 503
            log_to_web(
                f"REPLAY started (Original: {log_entry.request_id[:8]})", "info", log_entry.config.name, data=data
            )
            return jsonify({"status": "success", "message": "Replay queued", "request_id": request_id})
        except Exception:
            current_app.logger.exception("Failed to replay webhook")
            return jsonify({"status": "error", "message": "Replay failed"}), 500

    @main_bp.route("/history/delete/<log_id>", methods=["POST"])
    @auth_required
    def delete_log(log_id: str) -> Any:
        log_entry = WebhookLog.query.get_or_404(log_id)
        db.session.delete(log_entry)
        db.session.commit()
        return jsonify({"status": "success"})

    @main_bp.route("/history/delete-all", methods=["POST"])
    @auth_required
    def delete_all_logs() -> Any:
        WebhookLog.query.delete()
        db.session.commit()
        return jsonify({"status": "success", "message": "All logs deleted"})

    @main_bp.route("/history/bulk-delete", methods=["POST"])
    @auth_required
    def bulk_delete_logs() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        WebhookLog.query.filter(WebhookLog.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        return jsonify({"status": "success"})

    # --- LLM Health ---

    def _get_llm_health() -> dict[str, Any]:
        import time as _time

        import requests as _req

        ollama_host = os.environ.get("OLLAMA_HOST", "http://hookwise-llm:11434")
        t0 = _time.monotonic()
        try:
            resp = _req.get(f"{ollama_host}/api/tags", timeout=5)
            resp.raise_for_status()
            payload = resp.json()
            models = [m.get("name") for m in payload.get("models", [])]
            return {
                "status": "ok",
                "models": models,
                "model": models[0] if models else "unknown",
                "response_ms": round((_time.monotonic() - t0) * 1000),
            }
        except Exception as e:
            import logging as _logging

            _logging.getLogger(__name__).warning("LLM health check failed: %s", e)
            return {
                "status": "error",
                "error": type(e).__name__,
                "response_ms": round((_time.monotonic() - t0) * 1000),
            }

    @auth_required
    def health_llm() -> Any:
        return jsonify(_get_llm_health())

    @auth_required
    def api_health_llm() -> Any:
        return jsonify(_get_llm_health())

    @main_bp.route("/endpoint/dry-run-llm/<config_id>", methods=["POST"])
    @auth_required
    def dry_run_llm(config_id: str) -> Any:
        """Enqueue an LLM RCA task and return the task_id immediately — avoids proxy timeouts."""
        try:
            config = WebhookConfig.query.get_or_404(config_id)
            data = request.get_json(force=True, silent=True) or {}
            from .tasks import run_llm_rca

            task = run_llm_rca.delay(config_id, data, config.ai_prompt_template)
            return jsonify({"task_id": task.id})
        except Exception as e:
            import logging as _logging

            _logging.getLogger(__name__).error("dry_run_llm enqueue error: %s", e)
            return jsonify({"status": "error", "rca": f"Server error: {type(e).__name__}"}), 500

    @main_bp.route("/endpoint/dry-run-llm/status/<task_id>", methods=["GET"])
    @auth_required
    def dry_run_llm_status(task_id: str) -> Any:
        """Poll the result of an enqueued LLM RCA task."""
        from celery.result import AsyncResult

        from .tasks import celery

        result = AsyncResult(task_id, app=celery)
        if result.state == "PENDING" or result.state == "STARTED":
            return jsonify({"status": "pending"})
        if result.state == "SUCCESS":
            return jsonify(result.result)
        # FAILURE or other
        return jsonify({"status": "error", "rca": f"Task failed: {result.state}"}), 500

    @main_bp.route("/api/stats")
    @auth_required
    def get_stats() -> Any:
        from sqlalchemy import func

        today_start = datetime.combine(datetime.now(timezone.utc).date(), dtime.min)

        tickets_created = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.status == "processed",
                WebhookLog.action == "create",
                WebhookLog.created_at >= today_start,
            )
            .count()
        )

        tickets_updated = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.status == "processed",
                WebhookLog.action == "update",
                WebhookLog.created_at >= today_start,
            )
            .count()
        )

        tickets_closed = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.status == "processed",
                WebhookLog.action == "close",
                WebhookLog.created_at >= today_start,
            )
            .count()
        )

        failed_attempts = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.status == "failed",
                WebhookLog.created_at >= today_start,
            )
            .count()
        )

        dlq_today = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.status == "dlq",
                WebhookLog.created_at >= today_start,
            )
            .count()
        )

        # "No action" = webhooks that were handled but resulted in no ticket
        # change (skipped, or processed without a create/update/close action).
        non_action_today = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.created_at >= today_start,
                db.or_(
                    WebhookLog.status == "skipped",
                    db.and_(WebhookLog.status == "processed", WebhookLog.action.is_(None)),
                ),
            )
            .count()
        )

        total_today = (
            WebhookLog.query.join(WebhookConfig)
            .filter(WebhookConfig.is_draft.is_(False), WebhookLog.created_at >= today_start)
            .count()
        )
        successful_attempts = (
            WebhookLog.query.join(WebhookConfig)
            .filter(
                WebhookConfig.is_draft.is_(False),
                WebhookLog.status.in_(["processed", "skipped"]),
                WebhookLog.created_at >= today_start,
            )
            .count()
        )
        success_rate = (successful_attempts / total_today * 100) if total_today > 0 else 100
        avg_proc = (
            db.session.query(func.avg(WebhookLog.processing_time))
            .filter(WebhookLog.created_at >= today_start, WebhookLog.status == "processed")
            .scalar()
            or 0
        )

        return jsonify(
            {
                "created_today": tickets_created,
                "updated_today": tickets_updated,
                "closed_today": tickets_closed,
                "failed_today": failed_attempts,
                "dlq_today": dlq_today,
                "non_action_today": non_action_today,
                "success_rate": round(success_rate, 1),
                "avg_processing_time": round(float(avg_proc), 2),
            }
        )

    @main_bp.route("/api/stats/history")
    @auth_required
    def get_stats_history() -> Response:
        period = request.args.get("period", "daily")
        days = {"weekly": 28, "monthly": 180}.get(period, 7)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

        rows = (
            db.session.query(
                db.func.date(WebhookLog.created_at).label("day"),
                WebhookLog.action,
                db.func.count(WebhookLog.id),
            )
            .filter(db.func.date(WebhookLog.created_at) >= cutoff, WebhookLog.status == "processed")
            .group_by(db.func.date(WebhookLog.created_at), WebhookLog.action)
            .all()
        )

        counts_by_group: dict[str, dict[str, int]] = {}
        for row in rows:
            d = _parse_row_date(row[0])
            if not d:
                continue

            group_key = _get_group_key(d, period)
            if group_key not in counts_by_group:
                counts_by_group[group_key] = {"created": 0, "updated": 0, "closed": 0}

            action = row[1]
            count = row[2]
            if action == "create":
                counts_by_group[group_key]["created"] += count
            elif action == "update":
                counts_by_group[group_key]["updated"] += count
            elif action == "close":
                counts_by_group[group_key]["closed"] += count

        return jsonify(_format_history_response(counts_by_group, period))

    # --- ConnectWise Proxy ---

    @auth_required
    def get_cw_boards() -> Any:
        cache_key = "hookwise_cw_boards"
        cached = _get_cached_connectwise_list(cache_key)
        if cached is not None:
            return cached, 200, {"Content-Type": "application/json"}
        boards = cw_client.get_boards()
        if boards:
            redis_client.set(cache_key, json.dumps(boards), ex=3600)
        return jsonify(boards)

    @auth_required
    def get_cw_priorities() -> Any:
        cache_key = "hookwise_cw_priorities"
        cached = _get_cached_connectwise_list(cache_key)
        if cached is not None:
            return cached, 200, {"Content-Type": "application/json"}
        priorities = cw_client.get_priorities()
        if priorities:
            redis_client.set(cache_key, json.dumps(priorities), ex=86400)
        return jsonify(priorities)

    @auth_required
    def get_cw_statuses(board_id: str) -> Any:
        cache_key = f"hookwise_cw_statuses_{board_id}"
        cached = _get_cached_connectwise_list(cache_key)
        if cached is not None:
            return cached, 200, {"Content-Type": "application/json"}
        statuses = cw_client.get_board_statuses(int(board_id))
        if statuses:
            redis_client.set(cache_key, json.dumps(statuses), ex=3600)
        return jsonify(statuses)

    @auth_required
    def get_cw_types(board_id: str) -> Any:
        cache_key = f"hookwise_cw_types_{board_id}"
        cached = _get_cached_connectwise_list(cache_key)
        if cached is not None:
            return cached, 200, {"Content-Type": "application/json"}
        types = cw_client.get_board_types(int(board_id))
        if types:
            redis_client.set(cache_key, json.dumps(types), ex=3600)
        return jsonify(types)

    @auth_required
    def get_cw_subtypes(board_id: str) -> Any:
        cache_key = f"hookwise_cw_subtypes_{board_id}"
        cached = _get_cached_connectwise_list(cache_key)
        if cached is not None:
            return cached, 200, {"Content-Type": "application/json"}
        subtypes = cw_client.get_board_subtypes(int(board_id))
        if subtypes:
            redis_client.set(cache_key, json.dumps(subtypes), ex=3600)
        return jsonify(subtypes)

    @auth_required
    def get_cw_items(board_id: str) -> Any:
        cache_key = f"hookwise_cw_items_{board_id}"
        cached = _get_cached_connectwise_list(cache_key)
        if cached is not None:
            return cached, 200, {"Content-Type": "application/json"}
        items = cw_client.get_board_items(int(board_id))
        if items:
            redis_client.set(cache_key, json.dumps(items), ex=3600)
        return jsonify(items)

    @auth_required
    def get_cw_companies() -> Any:
        search = request.args.get("search")
        if not search:
            cache_key = "hookwise_cw_companies_default"
            cached = _get_cached_connectwise_list(cache_key)
            if cached is not None:
                return cached, 200, {"Content-Type": "application/json"}
        companies = cw_client.get_companies(search=search)
        if not search and companies:
            redis_client.set("hookwise_cw_companies_default", json.dumps(companies), ex=3600)
        return jsonify(companies)

    # --- Health & Infrastructure ---

    def readyz() -> Tuple[Response, int]:
        try:
            db.session.execute(db.text("SELECT 1"))
        except Exception as e:
            current_app.logger.error("Database readiness check failed", exc_info=e)
            return jsonify({"status": "not ready", "reason": "Database error"}), 503
        finally:
            db.session.remove()
        try:
            redis_client.ping()
            return jsonify({"status": "ready"}), 200
        except Exception:
            current_app.logger.exception("Redis readiness check failed")
            return jsonify({"status": "not ready", "reason": "Redis error"}), 503

    def health() -> Tuple[Response, int]:
        try:
            db.session.execute(db.text("SELECT 1"))
        except Exception as e:
            current_app.logger.error(f"Database health check failed: {e}")
            return jsonify({"status": "error", "message": "Database error"}), 503
        finally:
            db.session.remove()
        try:
            redis_client.ping()
            return jsonify({"status": "ok", "timestamp": time.time()}), 200
        except Exception:
            return jsonify({"status": "error", "message": "Service unreachable"}), 503

    @limiter.exempt
    def health_services() -> Tuple[Response, int]:
        health_data = {"redis": "down", "database": "down", "celery": "down", "timestamp": time.time()}
        status_code = 200

        try:
            db.session.execute(db.text("SELECT 1"))
            health_data["database"] = "up"
        except Exception as e:
            current_app.logger.error(f"Database health check failed: {e}")
            status_code = 503
        finally:
            db.session.remove()

        try:
            redis_client.ping()
            health_data["redis"] = "up"
        except Exception as e:
            current_app.logger.error(f"Redis health check failed: {e}")
            status_code = 503

        try:
            inspect = celery.control.inspect(timeout=1.0)
            stats = inspect.stats()
            active = inspect.active()
            health_data["celery"] = "up" if stats else "warning"
            health_data["celery_active"] = sum(len(tasks) for tasks in active.values()) if active else 0
        except Exception as e:
            current_app.logger.error(f"Celery health check failed: {e}")
            health_data["celery"] = "down"

        return jsonify(health_data), status_code

    # --- Admin ---

    @auth_required
    def maintenance_mode() -> Response:
        if request.method == "POST":
            current = redis_client.get("hookwise_maintenance_mode")
            new_state = "true" if not current or cast(bytes, current).decode() != "true" else "false"
            redis_client.set("hookwise_maintenance_mode", new_state)
            log_audit("maintenance_toggle", None, f"Maintenance mode set to {new_state}")
            return jsonify({"status": "success", "maintenance_mode": new_state == "true"})
        mode = redis_client.get("hookwise_maintenance_mode")
        return jsonify({"maintenance_mode": mode and cast(bytes, mode).decode() == "true"})

    @auth_required
    def settings() -> Any:
        retention = redis_client.get("hookwise_log_retention_days")
        retention = cast(bytes, retention).decode() if retention else os.environ.get("LOG_RETENTION_DAYS", "30")
        health_webhook = redis_client.get("hookwise_health_webhook")
        health_webhook = cast(bytes, health_webhook).decode() if health_webhook else ""
        cipp_app_certificate_exclude_names = redis_client.get(CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY)
        cipp_app_certificate_exclude_names = (
            cast(bytes, cipp_app_certificate_exclude_names).decode() if cipp_app_certificate_exclude_names else ""
        )
        api_key = redis_client.get("hookwise_master_api_key")
        api_key = cast(bytes, api_key).decode() if api_key else "Not Generated"
        user = User.query.get(session["user_id"])
        return render_template(
            "settings.html",
            log_retention_days=retention,
            master_api_key=api_key,
            health_webhook=health_webhook,
            cipp_app_certificate_exclude_names=cipp_app_certificate_exclude_names,
            user_2fa_enabled=user.is_2fa_enabled,
        )

    @auth_required
    def update_settings() -> Any:
        retention = request.form.get("log_retention_days")
        health_webhook = request.form.get("health_webhook")
        cipp_app_certificate_exclude_names = request.form.get("cipp_app_certificate_exclude_names", "")
        if retention:
            redis_client.set("hookwise_log_retention_days", retention)
        if health_webhook:
            redis_client.set("hookwise_health_webhook", health_webhook)
        exclude_patterns = parse_cipp_app_certificate_exclude_patterns(cipp_app_certificate_exclude_names)
        redis_client.set(CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY, "\n".join(exclude_patterns))
        flash("Settings updated successfully!")
        return redirect(url_for("main.settings"))

    @auth_required
    def clear_cache() -> Any:
        count = 0
        try:
            for key in redis_client.scan_iter("hookwise_cw_*"):
                redis_client.delete(key)
                count += 1
            log_audit("clear_cache", None, f"Cleared {count} ConnectWise API cache keys")
            return jsonify({"status": "success", "count": count})
        except Exception:
            current_app.logger.exception("Failed to clear ConnectWise cache")
            return jsonify({"status": "error", "message": "Failed to clear cache"}), 500

    @auth_required
    def generate_api_key() -> Any:
        new_key = secrets.token_urlsafe(64)
        redis_client.set("hookwise_master_api_key", new_key)
        log_audit("generate_master_api_key", None, "New master API key generated")
        return jsonify({"status": "success", "api_key": new_key})

    @auth_required
    def llm_test() -> Any:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "JSON body as dictionary is required"}), 400

        prompt = data.get("prompt")
        if not prompt:
            return jsonify({"status": "error", "message": "Prompt is required"}), 400

        from .tasks import LLM_TASK_TIME_LIMIT, run_llm_test

        try:
            task = run_llm_test.delay(prompt)
        except Exception:
            current_app.logger.exception("Failed to enqueue LLM diagnostic")
            return jsonify({"status": "error", "message": "Failed to enqueue LLM diagnostic"}), 503

        return (
            jsonify(
                {
                    "status": "pending",
                    "task_id": task.id,
                    "poll_after_ms": 2000,
                    "timeout_seconds": LLM_TASK_TIME_LIMIT + 30,
                }
            ),
            202,
        )

    @auth_required
    def llm_test_status(task_id: str) -> Any:
        from celery.result import AsyncResult

        from .tasks import celery

        result = AsyncResult(task_id, app=celery)
        if result.state in {"PENDING", "RECEIVED", "STARTED", "RETRY"}:
            return jsonify({"status": "pending", "state": result.state.lower()})
        if result.state == "SUCCESS":
            if isinstance(result.result, dict):
                return jsonify(result.result)
            return jsonify({"status": "error", "message": "LLM diagnostic returned an invalid result"}), 500
        return jsonify({"status": "error", "message": f"LLM diagnostic failed: {result.state}"}), 500

    @auth_required
    def submit_feedback() -> Any:
        data = request.json
        message = data.get("message")
        log_audit("feedback_submitted", None, f"Feedback: {message} | UA: {data.get('ua')}")
        return jsonify({"status": "success"})

    def _determine_alert_type(
        data: Dict[str, Any], config_data: Dict[str, Any], results: Dict[str, Any], steps: list
    ) -> None:
        trigger_field = config_data.get("trigger_field", "heartbeat.status")
        actual_val = str(resolve_jsonpath(data, trigger_field))
        steps.append(f"Trigger field '{trigger_field}' resolved to: '{actual_val}'")

        open_val = config_data.get("open_value", "0")
        close_val = config_data.get("close_value", "1")

        if actual_val in [v.strip() for v in open_val.split(",")]:
            results["alert_type"] = "OPEN (DOWN)"
        elif actual_val in [v.strip() for v in close_val.split(",")]:
            results["alert_type"] = "CLOSE (UP)"
        else:
            results["alert_type"] = "GENERIC"
        steps.append(f"Alert type determined as: {results['alert_type']}")

    def _apply_json_mapping(
        data: Dict[str, Any], config_data: Dict[str, Any], results: Dict[str, Any], steps: list
    ) -> None:
        mapping_str = config_data.get("json_mapping")
        if mapping_str:
            try:
                mapping = json.loads(mapping_str)
                for field, path in mapping.items():
                    val = resolve_jsonpath(data, path)
                    if val is not None:
                        results[field] = str(val)
                        steps.append(f"Mapped '{field}' using '{path}' -> '{val}'")
            except Exception:
                current_app.logger.exception("Failed to parse debug JSON mapping")
                steps.append("Error parsing JSON Mapping")

    def _apply_routing_rules(
        data: Dict[str, Any], config_data: Dict[str, Any], results: Dict[str, Any], steps: list
    ) -> None:
        rules_str = config_data.get("routing_rules")
        if rules_str:
            try:
                rules = json.loads(rules_str)
                for i, rule in enumerate(rules):
                    path = rule.get("path")
                    regex = rule.get("regex")
                    if path and regex:
                        val = str(resolve_jsonpath(data, path))
                        if _routing_regex_matches(str(regex), val):
                            steps.append(f"Rule {i + 1} matched: '{regex}' on '{path}' (value: '{val}')")
                            overrides = rule.get("overrides", {})
                            for k, v in overrides.items():
                                results[k] = v
                                steps.append(f"Override applied: {k} -> {v}")
                        else:
                            steps.append(f"Rule {i + 1} did NOT match: '{regex}' on '{path}'")
            except Exception:
                current_app.logger.exception("Failed to parse debug routing rules")
                steps.append("Error parsing Routing Rules")

    def _resolve_summary_and_company(
        data: Dict[str, Any], config_data: Dict[str, Any], results: Dict[str, Any], steps: list
    ) -> None:
        monitor_name = resolve_monitor_name(data)
        prefix = config_data.get("ticket_prefix", "Alert:")
        results["summary"] = results.get("summary") or (f"{prefix} {monitor_name}" if prefix else monitor_name)
        steps.append(f"Final Ticket Summary: '{results['summary']}'")

        company_id_match = re.search(r"#CW-?(\w+)", monitor_name)
        results["company"] = results.get("customer_id") or (
            company_id_match.group(1) if company_id_match else config_data.get("customer_id_default")
        )
        steps.append(f"Target Company Identifier: '{results['company']}'")

    @main_bp.route("/api/debug/process", methods=["POST"])
    @csrf.exempt
    @auth_required
    def debug_process() -> Any:
        data = request.json.get("payload")
        config_data = request.json.get("config", {})
        if not data:
            return jsonify({"status": "error", "message": "No sample payload provided"}), 400

        steps: list[str] = []
        results: Dict[str, Any] = {}

        _determine_alert_type(data, config_data, results, steps)
        _apply_json_mapping(data, config_data, results, steps)
        _apply_routing_rules(data, config_data, results, steps)
        _resolve_summary_and_company(data, config_data, results, steps)

        return jsonify({"status": "success", "steps": steps, "results": results})

    @main_bp.route("/metrics", methods=["GET"])
    @auth_required
    def metrics() -> Any:
        import hookwise.tasks as tasks_mod
        import hookwise.webhook as webhook_mod

        from .metrics import RedisMetricRegistry

        prom_counters = {
            "hookwise_webhooks_received_total": getattr(webhook_mod, "WEBHOOK_COUNT", None),
            "hookwise_webhooks_total": getattr(tasks_mod, "WEBHOOK_TOTAL", None),
            "hookwise_psa_tasks_total": getattr(tasks_mod, "PSA_TASK_COUNT", None),
        }

        # Filter out None and sync from Redis (the source of truth)
        active_counters = {k: v for k, v in prom_counters.items() if v is not None}
        RedisMetricRegistry.sync_to_prometheus(active_counters)

        try:
            size_raw = redis_client.llen("celery")
            size = float(cast(Any, size_raw))
            QUEUE_SIZE.set(size)
        except Exception:
            pass
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    from .admin_api import register_admin_routes
    from .backup_api import register_backup_routes
    from .connectwise_api import register_connectwise_routes
    from .delivery_api import register_delivery_routes
    from .health_api import register_health_routes

    handlers = locals()
    register_health_routes(main_bp, handlers)
    register_connectwise_routes(main_bp, handlers)
    register_admin_routes(main_bp, handlers)
    register_backup_routes(main_bp, handlers)
    register_delivery_routes(main_bp)


_register()
