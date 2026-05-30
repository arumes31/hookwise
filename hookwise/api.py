"""API, stats, health, admin, history, settings, debug, and metrics routes."""

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any, cast

from flask import Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import User, WebhookConfig, WebhookLog
from .tasks import cw_client, redis_client
from .utils import auth_required, log_audit, resolve_jsonpath

QUEUE_SIZE = Gauge("hookwise_celery_queue_size", "Approximate number of tasks in queue")


def _count_webhook_stats_base_query(today_start: datetime) -> Any:
    """Helper to get the base query for WebhookLog counting."""
    return WebhookLog.query.join(WebhookConfig).filter(
        WebhookConfig.is_draft.is_(False),
        WebhookLog.created_at >= today_start,
    )


def _count_webhook_stats(today_start: datetime, status_list: list[str] | str, action: str | None = None) -> int:
    """Helper to count WebhookLogs based on status and action since a given start time."""
    query = _count_webhook_stats_base_query(today_start)

    if isinstance(status_list, list):
        query = query.filter(WebhookLog.status.in_(status_list))
    else:
        query = query.filter(WebhookLog.status == status_list)

    if action:
        query = query.filter(WebhookLog.action == action)

    return int(query.count())


def _calculate_avg_processing_time(today_start: datetime) -> float:
    """Helper to calculate the average processing time for processed webhooks today."""
    from sqlalchemy import func

    avg_proc = (
        db.session.query(func.avg(WebhookLog.processing_time))
        .filter(WebhookLog.created_at >= today_start, WebhookLog.status == "processed")
        .scalar()
        or 0
    )
    return float(avg_proc)


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
                    payload_data = json.loads(log.payload)
                except (json.JSONDecodeError, TypeError):
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
        except Exception as e:
            current_app.logger.error(f"Failed to enqueue timeout check: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500

    @main_bp.route("/api/activity/logs")
    @auth_required
    def get_logs() -> Any:
        # For historical reasons or frontend compatibility, this might be needed
        return get_activity_history()

    @main_bp.route("/api/activity/purge", methods=["POST"])
    @auth_required
    def purge_logs() -> Any:
        try:
            days = int(request.form.get("days", 30))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            deleted = WebhookLog.query.filter(WebhookLog.created_at < cutoff).delete()
            db.session.commit()
            log_audit("purge_logs", None, f"Purged {deleted} logs older than {days} days")
            return jsonify({"status": "success", "count": deleted})
        except Exception as e:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(e)}), 500

    # --- Health & Monitoring ---

    @main_bp.route("/health")
    def health() -> Any:
        """Lightweight health check for load balancers."""
        try:
            # Check Redis
            redis_client.ping()
            # Check DB
            db.session.execute(db.text("SELECT 1"))
            return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            current_app.logger.error(f"Health check failed: {e}")
            return jsonify({"status": "error", "message": "Service unhealthy"}), 500

    @main_bp.route("/health/services")
    @auth_required
    def health_services() -> Any:
        """Detailed service status for admin dashboard."""
        status = {"redis": "up", "database": "up", "celery": "up", "connectwise": "up"}
        try:
            redis_client.ping()
        except Exception:
            status["redis"] = "down"
        try:
            db.session.execute(db.text("SELECT 1"))
        except Exception:
            status["database"] = "down"
        try:
            from .tasks import celery as celery_tasks

            inspect = celery_tasks.control.inspect()
            if not inspect.stats():
                status["celery"] = "down"
        except Exception:
            status["celery"] = "down"
        try:
            # Just test connectivity to CW API
            cw_client.get_ticket_statuses()
        except Exception:
            status["connectwise"] = "down"

        return jsonify(status)

    def _get_llm_health() -> dict[str, Any]:
        import time as _time

        from .utils import call_llm

        t0 = _time.monotonic()
        try:
            # Use a very short, simple prompt for health check
            # We don't use 'ping' because we want to verify the model is actually loaded and responding
            res = call_llm("Respond with exactly one word: OK", timeout=10)
            return {
                "status": "ok" if res and "OK" in res.upper() else "degraded",
                "response": res.strip() if res else None,
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

    @main_bp.route("/health/llm")
    @auth_required
    def health_llm() -> Any:
        return jsonify(_get_llm_health())

    @main_bp.route("/api/health/llm")
    @auth_required
    def api_health_llm() -> Any:
        return jsonify(_get_llm_health())

    @main_bp.route("/endpoint/dry-run-llm/<id>", methods=["POST"])
    @auth_required
    def dry_run_llm(id: str) -> Any:
        """Enqueue an LLM RCA task and return the task_id immediately — avoids proxy timeouts."""
        try:
            config = WebhookConfig.query.get_or_404(id)
            data = request.get_json(force=True, silent=True) or {}
            from .tasks import run_llm_rca

            task = run_llm_rca.delay(id, data, config.ai_prompt_template)
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

        from .tasks import celery as celery_tasks

        result = AsyncResult(task_id, app=celery_tasks)
        if result.state == "PENDING" or result.state == "STARTED":
            return jsonify({"status": "pending"})
        if result.state == "SUCCESS":
            return jsonify(result.result)
        # FAILURE or other
        return jsonify({"status": "error", "rca": f"Task failed: {result.state}"}), 500

    @main_bp.route("/api/stats")
    @auth_required
    def get_stats() -> Any:
        today_start = datetime.combine(datetime.now(timezone.utc).date(), dtime.min)

        tickets_created = _count_webhook_stats(today_start, "processed", "create")
        tickets_updated = _count_webhook_stats(today_start, "processed", "update")
        tickets_closed = _count_webhook_stats(today_start, "processed", "close")
        failed_attempts = _count_webhook_stats(today_start, ["failed", "dlq"])

        # Calculate success rate
        total_today = _count_webhook_stats_base_query(today_start).count()
        successful_attempts = _count_webhook_stats(today_start, ["processed", "skipped"])
        success_rate = (successful_attempts / total_today * 100) if total_today > 0 else 100

        avg_proc = _calculate_avg_processing_time(today_start)

        return jsonify(
            {
                "created_today": tickets_created,
                "updated_today": tickets_updated,
                "closed_today": tickets_closed,
                "failed_today": failed_attempts,
                "success_rate": round(success_rate, 1),
                "avg_processing_time": round(float(avg_proc), 2),
            }
        )

    @main_bp.route("/api/stats/history")
    @auth_required
    def get_stats_history() -> Response:
        period = request.args.get("period", "daily")

        if period == "weekly":
            days = 28
        elif period == "monthly":
            days = 180
        else:
            days = 7

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

        counts_by_group = {}
        for row in rows:
            action = row[1]
            count = row[2]

            if isinstance(row[0], date):
                d = row[0]
            else:
                try:
                    d = date.fromisoformat(str(row[0]).split(" ")[0])
                except ValueError:
                    try:
                        d = datetime.strptime(str(row[0]).split(" ")[0], "%Y-%m-%d").date()
                    except ValueError as e:
                        import logging

                        logging.error(f"Failed to parse date '{row[0]}': {e}")
                        continue

            if period == "weekly":
                year, week, _ = d.isocalendar()
                group_key = f"{year}-W{week}"
            elif period == "monthly":
                group_key = d.strftime("%Y-%m")
            else:
                group_key = d.strftime("%m-%d")

            if group_key not in counts_by_group:
                counts_by_group[group_key] = {"create": 0, "update": 0, "close": 0}
            if action in counts_by_group[group_key]:
                counts_by_group[group_key][action] = count

        # Sort and fill zeros for sparkline
        sorted_keys = sorted(counts_by_group.keys())
        history_data = {
            "labels": sorted_keys,
            "created": [counts_by_group[k]["create"] for k in sorted_keys],
            "updated": [counts_by_group[k]["update"] for k in sorted_keys],
            "closed": [counts_by_group[k]["close"] for k in sorted_keys],
        }
        return jsonify(history_data)

    @main_bp.route("/maintenance-mode")
    @auth_required
    def get_maintenance_mode() -> Any:
        mode = redis_client.get("hookwise_maintenance_mode")
        return jsonify({"maintenance_mode": mode and cast(bytes, mode).decode() == "true"})

    @main_bp.route("/settings")
    @auth_required
    def settings() -> Any:
        retention = redis_client.get("hookwise_log_retention_days")
        retention = cast(bytes, retention).decode() if retention else os.environ.get("LOG_RETENTION_DAYS", "30")
        health_webhook = redis_client.get("hookwise_health_webhook")
        health_webhook = cast(bytes, health_webhook).decode() if health_webhook else ""
        api_key = redis_client.get("hookwise_master_api_key")
        api_key = cast(bytes, api_key).decode() if api_key else "Not Generated"
        user = User.query.get(session["user_id"])
        return render_template(
            "settings.html",
            log_retention_days=retention,
            master_api_key=api_key,
            health_webhook=health_webhook,
            user_2fa_enabled=user.is_2fa_enabled,
        )

    @main_bp.route("/settings/update", methods=["POST"])
    @auth_required
    def update_settings() -> Any:
        retention = request.form.get("log_retention_days")
        health_webhook = request.form.get("health_webhook")
        if retention:
            redis_client.set("hookwise_log_retention_days", retention)
        if health_webhook:
            redis_client.set("hookwise_health_webhook", health_webhook)
        flash("Settings updated successfully!")
        return redirect(url_for("main.settings"))

    @main_bp.route("/admin/clear-cache", methods=["POST"])
    @auth_required
    def clear_cache() -> Any:
        count = 0
        try:
            for key in redis_client.scan_iter("hookwise_cw_*"):
                redis_client.delete(key)
                count += 1
            log_audit("clear_cache", None, f"Cleared {count} ConnectWise API cache keys")
            return jsonify({"status": "success", "count": count})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @main_bp.route("/admin/generate-api-key", methods=["POST"])
    @auth_required
    def generate_api_key() -> Any:
        new_key = secrets.token_urlsafe(64)
        redis_client.set("hookwise_master_api_key", new_key)
        log_audit("generate_master_api_key", None, "New master API key generated")
        return jsonify({"status": "success", "api_key": new_key})

    @main_bp.route("/admin/llm-test", methods=["POST"])
    @auth_required
    def llm_test() -> Any:
        from .utils import call_llm

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "JSON body as dictionary is required"}), 400

        prompt = data.get("prompt")
        if not prompt:
            return jsonify({"status": "error", "message": "Prompt is required"}), 400

        result = call_llm(prompt)
        if result:
            return jsonify({"status": "success", "result": result})
        return jsonify({"status": "error", "message": "LLM call failed or returned empty result"}), 500

    @main_bp.route("/admin/backup", methods=["GET"])
    @auth_required
    def backup_config() -> Any:
        configs = WebhookConfig.query.all()
        data = [c.to_dict(include_token=True) for c in configs]
        return Response(
            json.dumps(data, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment;filename=hookwise_backup.json"},
        )

    @main_bp.route("/admin/restore", methods=["POST"])
    @auth_required
    def restore_config() -> Any:
        file = request.files.get("backup_file")
        if not file:
            return jsonify({"status": "error", "message": "No file"}), 400
        try:
            data = json.load(file)
            for c in data:
                config = WebhookConfig.query.get(c["id"])
                if not config:
                    config = WebhookConfig(id=c["id"])
                    db.session.add(config)
                fields = [
                    "name",
                    "customer_id_default",
                    "board",
                    "status",
                    "ticket_type",
                    "subtype",
                    "item",
                    "priority",
                    "trigger_field",
                    "open_value",
                    "close_value",
                    "ticket_prefix",
                    "json_mapping",
                    "routing_rules",
                    "maintenance_windows",
                    "trusted_ips",
                    "is_enabled",
                    "is_pinned",
                    "is_draft",
                    "ai_rca_enabled",
                    "ai_prompt_template",
                    "bearer_token",
                    "description_template",
                    "hmac_secret",
                ]
                for f in fields:
                    if f in c:
                        setattr(config, f, c[f])
            db.session.commit()
            return jsonify({"status": "success"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(e)}), 500

    @main_bp.route("/api/feedback", methods=["POST"])
    @auth_required
    def submit_feedback() -> Any:
        data = request.json
        message = data.get("message")
        log_audit("feedback_submitted", None, f"Feedback: {message} | UA: {data.get('ua')}")
        return jsonify({"status": "success"})

    @main_bp.route("/api/debug/process", methods=["POST"])
    @auth_required
    def debug_process() -> Any:
        data = request.json.get("payload")
        config_data = request.json.get("config", {})
        if not data:
            return jsonify({"status": "error", "message": "No sample payload provided"}), 400

        steps = []
        results = {}

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

        mapping_str = config_data.get("json_mapping")
        if mapping_str:
            try:
                mapping = json.loads(mapping_str)
                for field, path in mapping.items():
                    val = resolve_jsonpath(data, path)
                    if val is not None:
                        results[field] = str(val)
                        steps.append(f"Mapped '{field}' using '{path}' -> '{val}'")
            except Exception as e:
                steps.append(f"Error parsing JSON Mapping: {e}")

        rules_str = config_data.get("routing_rules")
        if rules_str:
            try:
                rules = json.loads(rules_str)
                for i, rule in enumerate(rules):
                    path = rule.get("path")
                    regex = rule.get("regex")
                    if path and regex:
                        val = str(resolve_jsonpath(data, path))
                        if re.search(regex, val, re.IGNORECASE):
                            steps.append(f"Rule {i + 1} matched: '{regex}' on '{path}' (value: '{val}')")
                            overrides = rule.get("overrides", {})
                            for k, v in overrides.items():
                                results[k] = v
                                steps.append(f"Override applied: {k} -> {v}")
                        else:
                            steps.append(f"Rule {i + 1} did NOT match: '{regex}' on '{path}'")
            except Exception as e:
                steps.append(f"Error parsing Routing Rules: {e}")

        monitor = data.get("monitor", {})
        monitor_name = monitor.get("name", data.get("title", data.get("name", "Unknown Source")))
        prefix = config_data.get("ticket_prefix", "Alert:")
        results["summary"] = results.get("summary") or (f"{prefix} {monitor_name}" if prefix else monitor_name)
        steps.append(f"Final Ticket Summary: '{results['summary']}'")

        company_id_match = re.search(r"#CW-?(\w+)", monitor_name)
        results["company"] = results.get("customer_id") or (
            company_id_match.group(1) if company_id_match else config_data.get("customer_id_default")
        )
        steps.append(f"Target Company Identifier: '{results['company']}'")

        return jsonify({"status": "success", "steps": steps, "results": results})

    @main_bp.route("/metrics", methods=["GET"])
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


_register()
