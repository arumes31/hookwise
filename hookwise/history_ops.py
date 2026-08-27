"""Secure history troubleshooting, saved-search, replay, and operations APIs."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app, jsonify, request, session

from .extensions import db
from .models import SavedHistorySearch, WebhookConfig, WebhookLog
from .tasks import process_webhook_task, redis_client
from .utils import auth_required, log_audit, mask_secrets

_FILTER_KEYS = {
    "date_from",
    "date_to",
    "endpoint_id",
    "status",
    "search",
    "ticket",
    "request_id",
    "error_type",
    "http_status",
    "min_processing",
    "max_processing",
    "min_retry",
    "max_retry",
    "dlq_only",
}
_MAX_REPLAY_BYTES = 256 * 1024


def _operator_required() -> Any:
    if session.get("role") not in {"admin", "operator"}:
        return jsonify({"error": "An operator role is required for this action."}), 403
    return None


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _filter_logs(args: Any) -> Any:
    query = WebhookLog.query
    if value := args.get("endpoint_id"):
        query = query.filter(WebhookLog.config_id == value[:64])
    if value := args.get("status"):
        allowed = {"queued", "retrying", "processed", "skipped", "failed", "dlq"}
        if value not in allowed:
            raise ValueError("Unsupported status")
        query = query.filter(WebhookLog.status == value)
    if value := args.get("date_from"):
        query = query.filter(WebhookLog.created_at >= _parse_datetime(value))
    if value := args.get("date_to"):
        query = query.filter(WebhookLog.created_at <= _parse_datetime(value) + timedelta(days=1))
    if value := args.get("ticket"):
        if not value.isdigit():
            raise ValueError("ticket must be numeric")
        query = query.filter(WebhookLog.ticket_id == int(value))
    if value := args.get("request_id"):
        query = query.filter(WebhookLog.request_id.ilike(f"%{value[:100]}%"))
    if value := args.get("search"):
        term = value[:200]
        query = query.filter(
            db.or_(WebhookLog.request_id.ilike(f"%{term}%"), WebhookLog.error_message.ilike(f"%{term}%"))
        )
    if value := args.get("error_type"):
        query = query.filter(WebhookLog.error_type.ilike(f"%{value[:100]}%"))
    if value := args.get("http_status"):
        if not value.isdigit() or not 100 <= int(value) <= 599:
            raise ValueError("http_status must be an HTTP status code")
        # Legacy delivery records retain response text, not a separate HTTP column.
        query = query.filter(WebhookLog.error_message.ilike(f"%{value}%"))
    numeric_filters = (
        ("min_processing", WebhookLog.processing_time, ">=", float),
        ("max_processing", WebhookLog.processing_time, "<=", float),
        ("min_retry", WebhookLog.retry_count, ">=", int),
        ("max_retry", WebhookLog.retry_count, "<=", int),
    )
    for arg, column, comparison, parser in numeric_filters:
        if value := args.get(arg):
            try:
                parsed = parser(value)
            except ValueError as exc:
                raise ValueError(f"{arg} must be numeric") from exc
            query = query.filter(column >= parsed if comparison == ">=" else column <= parsed)
    if args.get("dlq_only", "").lower() in {"1", "true", "yes"}:
        query = query.filter(WebhookLog.status == "dlq")
    return query


def _error_chain(log: WebhookLog) -> list[dict[str, Any]]:
    try:
        raw = json.loads(log.error_chain or "[]")
    except json.JSONDecodeError:
        raw = []
    return mask_secrets(raw) if isinstance(raw, list) else []


def _safe_json(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return mask_secrets(json.loads(value))
    except (TypeError, json.JSONDecodeError):
        return {}


def _timeline(log: WebhookLog) -> list[dict[str, str]]:
    fields = [
        ("received", log.received_at or log.created_at),
        ("queued", log.queued_at),
        ("processing started", log.processing_started_at),
        ("ConnectWise started", log.connectwise_started_at),
        ("ConnectWise responded", log.connectwise_responded_at),
        ("completed", log.completed_at),
        ("retry exhausted", log.retry_exhausted_at),
    ]
    return [{"event": name, "at": at.isoformat()} for name, at in fields if at]


def _queue_replay(log: WebhookLog, payload: Any, suffix: str) -> dict[str, str]:
    body = json.dumps(payload, separators=(",", ":"))
    if len(body.encode("utf-8")) > _MAX_REPLAY_BYTES:
        raise ValueError("Replay payload exceeds 256 KiB")
    request_id = f"replay_{int(time.time() * 1000)}_{log.request_id[:16]}_{suffix}"[:100]
    now = datetime.now(timezone.utc)
    queued = WebhookLog(
        config_id=log.config_id,
        request_id=request_id,
        correlation_id=(log.correlation_id or log.request_id)[:100],
        payload=json.dumps(payload),
        status="queued",
        received_at=now,
        queued_at=now,
        replay_of_log_id=log.id,
    )
    db.session.add(queued)
    db.session.commit()
    process_webhook_task.delay(log.config_id, payload, request_id, log_id=queued.id)
    log_audit("history_replay", log.config_id, f"Queued replay for log {log.id}")
    return {"request_id": request_id, "log_id": queued.id}


def _register() -> None:
    from .routes import main_bp

    @main_bp.route("/api/history/advanced")
    @auth_required
    def history_advanced() -> Any:
        try:
            page = max(1, min(request.args.get("page", 1, type=int), 10_000))
            per_page = max(1, min(request.args.get("per_page", 25, type=int), 100))
            pagination = (
                _filter_logs(request.args)
                .order_by(WebhookLog.created_at.desc())
                .paginate(page=page, per_page=per_page, error_out=False)
            )
        except ValueError:
            return jsonify({"error": "Invalid history filters."}), 400
        return jsonify(
            {
                "items": [log.to_dict() for log in pagination.items],
                "total": pagination.total,
                "page": page,
                "pages": pagination.pages,
            }
        )

    @main_bp.route("/api/history/saved-searches", methods=["GET", "POST"])
    @auth_required
    def saved_history_searches() -> Any:
        user_id = str(session["user_id"])
        if request.method == "GET":
            rows = (
                SavedHistorySearch.query.filter_by(user_id=user_id).order_by(SavedHistorySearch.updated_at.desc()).all()
            )
            return jsonify([{"id": row.id, "name": row.name, "filters": json.loads(row.filters)} for row in rows])
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip()
        filters = body.get("filters", {})
        if not 1 <= len(name) <= 100 or not isinstance(filters, dict) or not set(filters) <= _FILTER_KEYS:
            return jsonify({"error": "Provide a name and supported filter values."}), 400
        if any(not isinstance(value, (str, int, float, bool)) or len(str(value)) > 200 for value in filters.values()):
            return jsonify({"error": "Invalid filter values."}), 400
        row = SavedHistorySearch(user_id=user_id, name=name, filters=json.dumps(filters))
        db.session.add(row)
        db.session.commit()
        return jsonify({"id": row.id, "name": row.name, "filters": filters}), 201

    @main_bp.route("/api/history/saved-searches/<search_id>", methods=["DELETE"])
    @auth_required
    def delete_saved_history_search(search_id: str) -> Any:
        row = SavedHistorySearch.query.filter_by(id=search_id, user_id=str(session["user_id"])).first_or_404()
        db.session.delete(row)
        db.session.commit()
        return jsonify({"status": "deleted"})

    @main_bp.route("/api/history/<log_id>/diagnostics")
    @auth_required
    def history_diagnostics(log_id: str) -> Any:
        log = WebhookLog.query.get_or_404(log_id)
        metadata = log.to_dict()
        metadata.pop("payload", None)
        metadata.pop("headers", None)
        return jsonify(
            {
                "log": metadata,
                "timeline": _timeline(log),
                "error_chain": _error_chain(log),
                "retry_attempts": [mask_secrets(row.to_dict()) for row in log.retry_attempts],
                "notice": "Payload and request headers are intentionally excluded from downloadable diagnostics.",
            }
        )

    @main_bp.route("/api/history/<log_id>/retry", methods=["POST"])
    @auth_required
    def retry_history_log(log_id: str) -> Any:
        if denied := _operator_required():
            return denied
        log = WebhookLog.query.get_or_404(log_id)
        if log.status not in {"failed", "dlq", "retrying"}:
            return jsonify({"error": "Only failed or dead-lettered logs can be retried."}), 409
        if WebhookLog.query.filter(
            WebhookLog.replay_of_log_id == log.id, WebhookLog.status.in_(["queued", "retrying"])
        ).first():
            return jsonify({"error": "A replay is already queued for this log."}), 409
        try:
            payload = json.loads(log.payload)
        except json.JSONDecodeError:
            return jsonify({"error": "Stored payload is not valid JSON."}), 409
        try:
            return jsonify({"status": "queued", **_queue_replay(log, payload, "retry")}), 202
        except ValueError:
            return jsonify({"error": "Replay payload exceeds 256 KiB."}), 400
        except Exception:
            current_app.logger.exception("retry-now failed")
            db.session.rollback()
            return jsonify({"error": "Unable to queue retry."}), 503

    @main_bp.route("/api/history/<log_id>/replay-edits", methods=["POST"])
    @auth_required
    def replay_with_edits(log_id: str) -> Any:
        if denied := _operator_required():
            return denied
        log = WebhookLog.query.get_or_404(log_id)
        body = request.get_json(silent=True) or {}
        payload = body.get("payload")
        if not isinstance(payload, (dict, list)):
            return jsonify({"error": "payload must be a JSON object or array."}), 400
        try:
            return jsonify({"status": "queued", **_queue_replay(log, payload, "edited")}), 202
        except ValueError:
            return jsonify({"error": "Replay payload exceeds 256 KiB."}), 400
        except Exception:
            current_app.logger.exception("edited replay failed")
            db.session.rollback()
            return jsonify({"error": "Unable to queue replay."}), 503

    @main_bp.route("/api/history/dlq/replay", methods=["POST"])
    @auth_required
    def replay_dead_letters() -> Any:
        if denied := _operator_required():
            return denied
        ids = (request.get_json(silent=True) or {}).get("ids", [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or any(not isinstance(item, str) for item in ids):
            return jsonify({"error": "Provide between 1 and 50 dead-letter IDs."}), 400
        logs = WebhookLog.query.filter(WebhookLog.id.in_(ids), WebhookLog.status == "dlq").all()
        queued, errors = [], []
        errors.extend(sorted(set(ids) - {log.id for log in logs}))
        for log in logs:
            try:
                queued.append(_queue_replay(log, json.loads(log.payload), "dlq"))
            except (ValueError, json.JSONDecodeError):
                errors.append(log.id)
                db.session.rollback()
        return jsonify({"queued": queued, "errors": errors}), 202 if queued else 409

    @main_bp.route("/api/history/operations")
    @auth_required
    def history_operations() -> Any:
        now = datetime.now(timezone.utc)
        retry_exhausted = WebhookLog.query.filter(
            WebhookLog.retry_exhausted_at.is_not(None), WebhookLog.retry_exhausted_at >= now - timedelta(days=7)
        ).count()
        dlq = WebhookLog.query.filter(WebhookLog.status == "dlq").count()
        configs = WebhookConfig.query.filter(WebhookConfig.is_draft.is_(False)).order_by(WebhookConfig.name).all()
        bucket = int(now.timestamp() // 60)
        endpoint_limits = []
        for config in configs:
            try:
                raw_count = redis_client.get(f"hookwise:rate-limit:{config.id}:{bucket}")
                current = int(raw_count or 0)
            except Exception:
                current = 0
            limit = int(config.rate_limit_per_minute or 60)
            endpoint_limits.append({
                "id": config.id, "name": config.name, "rate_limit_per_minute": limit,
                "current_minute": current, "utilization_percent": round(current / limit * 100, 1),
            })
        quota = None
        try:
            values = redis_client.mget([f"hookwise:cw:quota:{name}" for name in ("limit", "remaining", "reset")])
            decoded = [value.decode() if isinstance(value, bytes) else value for value in values]
            if any(value is not None for value in decoded):
                quota = dict(zip(("limit", "remaining", "reset"), decoded, strict=True))
        except Exception:
            quota = None
        return jsonify(
            {
                "retry_exhausted_last_7d": retry_exhausted,
                "dead_letter_queue": dlq,
                "endpoint_rate_limits": endpoint_limits,
                "connectwise_quota": quota,
                "quota_available": quota is not None,
            }
        )


_register()
