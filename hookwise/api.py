"""API, stats, health, admin, history, settings, debug, and metrics routes."""

import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any, Tuple, cast

from flask import Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

from .extensions import db, limiter
from .models import AuditLog, User, WebhookConfig, WebhookLog
from .tasks import celery, cw_client, process_webhook_task, redis_client
from .utils import auth_required, log_audit, log_to_web, resolve_jsonpath

QUEUE_SIZE = Gauge("hookwise_celery_queue_size", "Approximate number of tasks in queue")


def _register() -> None:
    from .routes import main_bp

    # --- History & Logs ---

    @main_bp.route("/history")
    @auth_required
    def history() -> Any:
        page = request.args.get("page", 1, type=int)
        search = request.args.get("search", "")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        endpoint_id = request.args.get("endpoint_id", "")
        per_page = 25

        query = WebhookLog.query
        if search:
            if search.startswith("#"):
                search_id = search[1:]
                if search_id.isdigit():
                    query = query.filter(WebhookLog.ticket_id == int(search_id))
            elif search.isdigit():
                query = query.filter(WebhookLog.ticket_id == int(search))
            else:
                query = query.filter(
                    (WebhookLog.request_id.ilike(f"%{search}%"))
                    | (WebhookLog.payload.ilike(f"%{search}%"))
                    | (WebhookLog.error_message.ilike(f"%{search}%"))
                )

        if endpoint_id:
            query = query.filter(WebhookLog.config_id == endpoint_id)

        if date_from:
            from datetime import datetime

            query = query.filter(WebhookLog.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            from datetime import datetime, timedelta

            query = query.filter(WebhookLog.created_at <= datetime.fromisoformat(date_to) + timedelta(days=1))

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

    @main_bp.route("/history/replay/<log_id>", methods=["POST"])
    @auth_required
    def replay_webhook(log_id: str) -> Any:
        log_entry = WebhookLog.query.get_or_404(log_id)
        try:
            data = json.loads(log_entry.payload)
            request_id = f"replay_{int(time.time())}_{log_entry.request_id[:8]}"
            process_webhook_task.delay(log_entry.config_id, data, request_id)
            log_to_web(
                f"REPLAY started (Original: {log_entry.request_id[:8]})", "info", log_entry.config.name, data=data
            )
            return jsonify({"status": "success", "message": "Replay queued", "request_id": request_id})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @main_bp.route("/history/delete/<id>", methods=["POST"])
    @auth_required
    def delete_log(id: str) -> Any:
        log_entry = WebhookLog.query.get_or_404(id)
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

    # --- Endpoint Testing ---

    @main_bp.route("/endpoint/test/<id>", methods=["POST"])
    @auth_required
    def test_endpoint(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        request_id = f"test_{int(time.time())}"
        data = {
            "monitor": {"name": f"Test Monitor for {config.name}"},
            "status": "0",
            "msg": "Common test message for webhook verification",
            "heartbeat": {"status": "0"},
            "title": "Manual Test Trigger",
            "message": "This is a simulated webhook payload.",
        }
        process_webhook_task.delay(id, data, request_id)
        log_to_web(f"Manual test triggered for {config.name} (ID: {request_id})", "info", config.name, data=data)
        return jsonify({"status": "success", "message": "Test webhook queued", "request_id": request_id})

    @main_bp.route("/endpoint/dry-run/<id>", methods=["POST"])
    @auth_required
    def dry_run_endpoint(id: str) -> Any:
        """Simulate webhook processing without calling ConnectWise or Redis."""
        config = WebhookConfig.query.get_or_404(id)
        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:
            return jsonify({"status": "error", "message": "Invalid JSON body"}), 400

        from .tasks import is_in_maintenance
        from .utils import resolve_jsonpath

        steps = []
        result: dict[str, Any] = {}

        # Step 1: Maintenance window
        maintenance_active = is_in_maintenance(config)
        steps.append({
            "step": "Maintenance Window",
            "active": maintenance_active,
            "result": "skipped" if maintenance_active else "ok",
        })
        if maintenance_active:
            return jsonify({"action": "skip", "reason": "maintenance_window", "steps": steps})

        # Step 2: JSON mapping
        json_mapping: dict[str, str] = {}
        if config.json_mapping:
            try:
                json_mapping = json.loads(config.json_mapping)
            except Exception:
                pass

        import re as _re
        mapped_vals: dict[str, str] = {}
        overridable = [
            "summary", "description", "customer_id",
            "ticket_type", "subtype", "item", "priority", "board", "status",
        ]
        for field in overridable:
            if field in json_mapping:
                mapping_val = json_mapping[field]
                if isinstance(mapping_val, str) and " " in mapping_val:
                    token_re = _re.compile(r"(\$\S+|[^\s]+)")
                    tokens = token_re.findall(mapping_val)
                    resolved: list[tuple[str, bool]] = []
                    any_resolved = False
                    for tok in tokens:
                        if tok.startswith("$"):
                            r_val = resolve_jsonpath(data, tok)
                            if r_val is not None and str(r_val).strip():
                                resolved.append((str(r_val).strip(), True))
                                any_resolved = True
                            else:
                                resolved.append(("", True))
                        else:
                            resolved.append((tok, False))
                    if any_resolved:
                        output_parts = []
                        for i, (v, is_var) in enumerate(resolved):
                            if is_var:
                                if v:
                                    output_parts.append(v)
                            else:
                                left_ok = any(resolved[j][0] and resolved[j][1] for j in range(i - 1, -1, -1))
                                right_ok = any(resolved[j][0] and resolved[j][1] for j in range(i + 1, len(resolved)))
                                if left_ok or right_ok:
                                    output_parts.append(v)
                        if output_parts:
                            mapped_vals[field] = " ".join(output_parts)
                else:
                    r = resolve_jsonpath(data, mapping_val)
                    if r is not None:
                        mapped_vals[field] = str(r)
        steps.append({"step": "JSONPath Mapping", "resolved": mapped_vals})

        # Step 3: Routing rules
        routing_rules: list[dict[str, Any]] = []
        if config.routing_rules:
            try:
                routing_rules = json.loads(config.routing_rules)
            except Exception:
                pass
        matched_rules = []
        for rule in routing_rules:
            rule_path = rule.get("path")
            rule_regex = rule.get("regex")
            if rule_path and rule_regex:
                val = str(resolve_jsonpath(data, rule_path))
                if _re.search(rule_regex, val, _re.IGNORECASE):
                    matched_rules.append({
                        "regex": rule_regex,
                        "path": rule_path,
                        "overrides": rule.get("overrides", {}),
                    })
        steps.append({"step": "Routing Rules", "matched": matched_rules})

        # Step 4: Trigger field evaluation
        trigger_field = config.trigger_field or ""
        open_value = config.open_value or ""
        close_value = config.close_value or ""
        actual_val = str(resolve_jsonpath(data, trigger_field)) if trigger_field else ""
        open_triggers = [v.strip() for v in open_value.split(",") if v.strip()]
        close_triggers = [v.strip() for v in close_value.split(",") if v.strip()]
        if actual_val in open_triggers:
            alert_type = "DOWN"
        elif actual_val in close_triggers:
            alert_type = "UP"
        else:
            alert_type = "GENERIC"

        steps.append({
            "step": "Trigger Evaluation",
            "trigger_field": trigger_field,
            "actual_value": actual_val,
            "alert_type": alert_type,
        })

        # Step 5: Predicted action
        prefix = config.ticket_prefix or os.environ.get("CW_TICKET_PREFIX", "Alert:")
        mapped_summary = mapped_vals.get("summary")
        monitor_name = data.get("monitor", {}).get("name", data.get("title", "Unknown Source"))
        ticket_summary = f"{prefix} {mapped_summary}" if mapped_summary else f"{prefix} {monitor_name}"
        predicted_action = (
            "create_ticket" if alert_type == "DOWN"
            else "close_ticket" if alert_type == "UP"
            else "add_note_or_skip"
        )

        result = {
            "action": predicted_action,
            "alert_type": alert_type,
            "ticket_summary": ticket_summary,
            "company_id": mapped_vals.get("customer_id", config.customer_id_default or ""),
            "board": (matched_rules[0] if matched_rules else {}).get("overrides", {}).get(
                "board", mapped_vals.get("board", config.board or "")
            ),
            "steps": steps,
        }
        return jsonify(result)

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
        """Run the LLM RCA prompt against the provided payload without creating any ticket."""
        try:
            config = WebhookConfig.query.get_or_404(id)
            data = request.get_json(force=True, silent=True) or {}
            from .utils import call_llm

            # Truncate payload to avoid bloating the prompt and extending LLM response time
            payload_str = json.dumps(data)
            if len(payload_str) > 4000:
                payload_str = payload_str[:4000] + "… [truncated]"

            rca_prompt = (
                "Analyze this technical alert and suggest 3 possible root causes and 3 troubleshooting "
                f"steps. Be concise and technical. Payload: {payload_str}"
            )
            system_prompt = config.ai_prompt_template or (
                "You are a helpful assistant specialized in ConnectWise ticketing and alert analysis. "
                "Be concise and return only the requested value."
            )
            result = call_llm(rca_prompt, system_prompt=system_prompt)
            if result:
                return jsonify({"status": "ok", "rca": result})
            return jsonify({
                "status": "error",
                "rca": "LLM returned no response — check OLLAMA_HOST and model.",
            }), 502
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).error("dry_run_llm error: %s", e)
            return jsonify({"status": "error", "rca": f"Server error: {type(e).__name__}"}), 500


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
                WebhookLog.action.in_(["create", "update"]),
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
                WebhookLog.status.in_(["failed", "dlq"]),
                WebhookLog.created_at >= today_start,
            )
            .count()
        )

        total_today = (
            WebhookLog.query.join(WebhookConfig)
            .filter(WebhookConfig.is_draft.is_(False), WebhookLog.created_at >= today_start)
            .count()
        )
        success_rate = (tickets_created / total_today * 100) if total_today > 0 else 100

        avg_proc = (
            db.session.query(func.avg(WebhookLog.processing_time))
            .filter(WebhookLog.created_at >= today_start, WebhookLog.status == "processed")
            .scalar()
            or 0
        )

        return jsonify(
            {
                "created_today": tickets_created,
                "closed_today": tickets_closed,
                "failed_today": failed_attempts,
                "success_rate": round(success_rate, 1),
                "avg_processing_time": round(float(avg_proc), 2),
            }
        )

    @main_bp.route("/api/stats/history")
    @auth_required
    def get_stats_history() -> Response:
        days = 7
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()

        # Single query instead of 7 separate COUNT queries
        rows = (
            db.session.query(db.func.date(WebhookLog.created_at).label("day"), db.func.count(WebhookLog.id))
            .filter(db.func.date(WebhookLog.created_at) >= cutoff, WebhookLog.status == "processed")
            .group_by(db.func.date(WebhookLog.created_at))
            .all()
        )

        counts_by_day = {str(row[0]): row[1] for row in rows}

        history_data = []
        for i in range(days - 1, -1, -1):
            date = (datetime.now(timezone.utc) - timedelta(days=i)).date()
            history_data.append({"date": date.strftime("%m-%d"), "count": counts_by_day.get(str(date), 0)})
        return jsonify(history_data)

    # --- ConnectWise Proxy ---

    @main_bp.route("/api/cw/boards")
    @auth_required
    def get_cw_boards() -> Any:
        cache_key = "hookwise_cw_boards"
        cached = redis_client.get(cache_key)
        if cached:
            return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        boards = cw_client.get_boards()
        if boards:
            redis_client.set(cache_key, json.dumps(boards), ex=3600)
        return jsonify(boards)

    @main_bp.route("/api/cw/priorities")
    @auth_required
    def get_cw_priorities() -> Any:
        cache_key = "hookwise_cw_priorities"
        cached = redis_client.get(cache_key)
        if cached:
            return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        priorities = cw_client.get_priorities()
        if priorities:
            redis_client.set(cache_key, json.dumps(priorities), ex=86400)
        return jsonify(priorities)

    @main_bp.route("/api/cw/statuses/<board_id>")
    @auth_required
    def get_cw_statuses(board_id: str) -> Any:
        cache_key = f"hookwise_cw_statuses_{board_id}"
        cached = redis_client.get(cache_key)
        if cached:
            return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        statuses = cw_client.get_board_statuses(int(board_id))
        redis_client.set(cache_key, json.dumps(statuses), ex=3600)
        return jsonify(statuses)

    @main_bp.route("/api/cw/types/<board_id>")
    @auth_required
    def get_cw_types(board_id: str) -> Any:
        cache_key = f"hookwise_cw_types_{board_id}"
        cached = redis_client.get(cache_key)
        if cached:
            return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        types = cw_client.get_board_types(int(board_id))
        redis_client.set(cache_key, json.dumps(types), ex=3600)
        return jsonify(types)

    @main_bp.route("/api/cw/subtypes/<board_id>")
    @auth_required
    def get_cw_subtypes(board_id: str) -> Any:
        cache_key = f"hookwise_cw_subtypes_{board_id}"
        cached = redis_client.get(cache_key)
        if cached:
            return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        subtypes = cw_client.get_board_subtypes(int(board_id))
        redis_client.set(cache_key, json.dumps(subtypes), ex=3600)
        return jsonify(subtypes)

    @main_bp.route("/api/cw/items/<board_id>")
    @auth_required
    def get_cw_items(board_id: str) -> Any:
        cache_key = f"hookwise_cw_items_{board_id}"
        cached = redis_client.get(cache_key)
        if cached:
            return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        items = cw_client.get_board_items(int(board_id))
        redis_client.set(cache_key, json.dumps(items), ex=3600)
        return jsonify(items)

    @main_bp.route("/api/cw/companies")
    @auth_required
    def get_cw_companies() -> Any:
        search = request.args.get("search")
        if not search:
            cache_key = "hookwise_cw_companies_default"
            cached = redis_client.get(cache_key)
            if cached:
                return cast(bytes, cached).decode(), 200, {"Content-Type": "application/json"}
        companies = cw_client.get_companies(search=search)
        if not search and companies:
            redis_client.set("hookwise_cw_companies_default", json.dumps(companies), ex=3600)
        return jsonify(companies)

    # --- Health & Infrastructure ---

    @main_bp.route("/readyz", methods=["GET"])
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
        except Exception as e:
            return jsonify({"status": "not ready", "reason": str(e)}), 503

    @main_bp.route("/health", methods=["GET"])
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

    @main_bp.route("/health/services", methods=["GET"])
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

    @main_bp.route("/admin/maintenance", methods=["GET", "POST"])
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
