"""Filtered activity feed API used by dashboard controls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import jsonify, request, session
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import EventAnnotation, WebhookConfig, WebhookLog
from .utils import auth_required


def _level(log: WebhookLog) -> str:
    if log.status in {"failed", "dlq"}:
        return "danger"
    if log.status == "processed" and log.action == "create":
        return "warning"
    if log.status == "processed" and log.action == "close":
        return "success"
    return "info"


def _entry(log: WebhookLog, annotation: EventAnnotation | None = None) -> dict[str, Any]:
    return {
        "id": log.id,
        "timestamp": log.created_at.replace(tzinfo=timezone.utc).isoformat()
        if log.created_at.tzinfo is None
        else log.created_at.isoformat(),
        "level": _level(log),
        "status": log.status,
        "action": log.action or "none",
        "config_id": log.config_id,
        "config_name": log.config.name if log.config else "Unknown endpoint",
        "board": log.config.board if log.config else None,
        "request_id": log.request_id,
        "message": (log.error_message or f"{log.status.title()} webhook")[:500],
        "ticket_id": log.ticket_id,
        "annotation": (
            {"text": annotation.text, "is_pinned": annotation.is_pinned} if annotation is not None else None
        ),
    }


def _register() -> None:
    from .routes import main_bp

    @main_bp.route("/api/activity/stream")
    @auth_required
    def activity_stream() -> Any:
        """Return bounded, filterable log data without request payloads or secrets."""
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 250)
        except ValueError:
            limit = 100
        query = WebhookLog.query.options(joinedload(WebhookLog.config))  # type: ignore[arg-type]
        query = query.join(WebhookConfig)
        status = request.args.get("status", "").strip()
        if status in {"queued", "processed", "failed", "skipped", "dlq"}:
            query = query.filter(WebhookLog.status == status)
        action = request.args.get("action", "").strip()
        if action in {"create", "update", "close"}:
            query = query.filter(WebhookLog.action == action)
        endpoint_id = request.args.get("endpoint_id", "").strip()
        if endpoint_id:
            query = query.filter(WebhookLog.config_id == endpoint_id[:64])
        board = request.args.get("board", "").strip()
        if board:
            query = query.filter(WebhookConfig.board == board[:100])
        severity = request.args.get("severity", "").strip()
        if severity == "failure":
            query = query.filter(WebhookLog.status.in_(["failed", "dlq"]))
        elif severity == "success":
            query = query.filter(WebhookLog.status == "processed")
        logs = query.order_by(WebhookLog.created_at.desc()).limit(limit).all()
        annotations = (
            {
                row.log_id: row
                for row in EventAnnotation.query.filter(
                    EventAnnotation.user_id == str(session["user_id"]),
                    EventAnnotation.log_id.in_([log.id for log in logs]),
                ).all()
            }
            if logs
            else {}
        )
        entries = [_entry(log, annotations.get(log.id)) for log in logs]
        return jsonify({"events": entries, "generated_at": datetime.now(timezone.utc).isoformat(), "limit": limit})

    @main_bp.route("/api/activity/events/<log_id>/annotation", methods=["PUT", "DELETE"])
    @auth_required
    def activity_annotation(log_id: str) -> Any:
        WebhookLog.query.get_or_404(log_id)
        user_id = str(session["user_id"])
        annotation = EventAnnotation.query.filter_by(user_id=user_id, log_id=log_id).first()
        if request.method == "DELETE":
            if annotation is not None:
                db.session.delete(annotation)
                db.session.commit()
            return jsonify({"status": "deleted"})
        body = request.get_json(silent=True) or {}
        text = str(body.get("text", "")).strip()
        pinned = body.get("is_pinned", False)
        if len(text) > 280 or not isinstance(pinned, bool):
            return jsonify({"error": "Annotation text must be at most 280 characters."}), 400
        if not text and not pinned:
            if annotation is not None:
                db.session.delete(annotation)
                db.session.commit()
            return jsonify({"status": "deleted"})
        if annotation is None:
            annotation = EventAnnotation(user_id=user_id, log_id=log_id, text=text, is_pinned=pinned)
            db.session.add(annotation)
        else:
            annotation.text = text
            annotation.is_pinned = pinned
        db.session.commit()
        return jsonify({"text": annotation.text, "is_pinned": annotation.is_pinned})


_register()
