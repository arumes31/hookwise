"""Bounded, secret-safe endpoint summary API for dashboard cards."""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import jsonify, request
from sqlalchemy import case, func
from sqlalchemy.orm import selectinload

from .extensions import db
from .models import WebhookConfig, WebhookLog
from .utils import auth_required

_TOKEN_SUFFIX = re.compile(r"^[A-Za-z0-9_-]{4}$")
_ACTIVE = ("queued", "processing", "retrying")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value else None


def _tags(config: WebhookConfig) -> list[str]:
    return [tag.name for tag in config.tags[:12]]


def _token_suffix_matches(config: WebhookConfig, suffix: str) -> bool:
    if not suffix or not _TOKEN_SUFFIX.fullmatch(suffix):
        return False
    hint = config.bearer_token_last4 or ""
    return len(hint) == 4 and hmac.compare_digest(hint, suffix)


def _latest_rows(config_ids: list[str], statuses: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    ranked = (
        db.session.query(
            WebhookLog.config_id.label("config_id"),
            WebhookLog.created_at.label("created_at"),
            WebhookLog.processing_time.label("processing_time"),
            WebhookLog.error_message.label("error_message"),
            func.row_number()
            .over(partition_by=WebhookLog.config_id, order_by=WebhookLog.created_at.desc())
            .label("rank"),
        )
        .filter(WebhookLog.config_id.in_(config_ids), WebhookLog.status.in_(statuses))
        .subquery()
    )
    return {
        row.config_id: {
            "created_at": row.created_at,
            "processing_time": row.processing_time,
            "error_message": row.error_message,
        }
        for row in db.session.query(ranked).filter(ranked.c.rank == 1).all()
    }


def _build_summaries(configs: list[WebhookConfig]) -> list[dict[str, Any]]:
    config_ids = [config.id for config in configs]
    if not config_ids:
        return []
    rows = (
        db.session.query(
            WebhookLog.config_id,
            func.count(WebhookLog.id),
            func.sum(case((WebhookLog.status == "processed", 1), else_=0)),
            func.sum(case((WebhookLog.status.in_(("failed", "dlq")), 1), else_=0)),
            func.sum(case((WebhookLog.status.in_(_ACTIVE), 1), else_=0)),
            func.max(case((WebhookLog.status.in_(_ACTIVE), WebhookLog.retry_count), else_=0)),
            func.avg(WebhookLog.processing_time),
        )
        .filter(WebhookLog.config_id.in_(config_ids))
        .group_by(WebhookLog.config_id)
        .all()
    )
    stats = {
        row[0]: {
            "activity": int(row[1] or 0), "good": int(row[2] or 0),
            "failed": int(row[3] or 0), "queue": int(row[4] or 0),
            "retries": int(row[5] or 0),
            "latency": round(float(row[6]), 3) if row[6] is not None else None,
        }
        for row in rows
    }
    successes = _latest_rows(config_ids, ("processed",))
    failures = _latest_rows(config_ids, ("failed", "dlq"))
    now = datetime.now(timezone.utc)
    summaries: list[dict[str, Any]] = []
    for config in configs:
        values = stats.get(
            config.id,
            {"activity": 0, "good": 0, "failed": 0, "queue": 0, "retries": 0, "latency": None},
        )
        success = successes.get(config.id)
        failure = failures.get(config.id)
        last_activity = _as_utc(config.last_seen_at) or _as_utc(config.created_at)
        timeout_hours = max(1, int(config.timeout_hours or 24))
        stale = bool(config.is_enabled and last_activity and now - last_activity > timedelta(hours=timeout_hours))
        token_reference = _as_utc(config.last_rotated_at) or _as_utc(config.created_at)
        outcomes = values["good"] + values["failed"]
        summaries.append({
            "id": config.id, "name": config.name, "url": f"/w/{config.id}",
            "board": config.board or "Default", "company": config.customer_id_default or "",
            "status": "enabled" if config.is_enabled else "paused", "is_draft": bool(config.is_draft),
            "is_pinned": bool(config.is_pinned), "tags": _tags(config),
            "health": config.config_health_status or "OK",
            "is_unhealthy": (config.config_health_status or "OK") in {"WARNING", "ERROR"},
            "is_stale": stale, "last_success_at": _iso(success["created_at"]) if success else None,
            "last_failure_at": _iso(failure["created_at"]) if failure else None,
            "last_error": str(failure["error_message"] or "")[:240] if failure else "",
            "last_response_time": success["processing_time"] if success else None,
            "average_latency": values["latency"], "queue_depth": values["queue"],
            "retry_count": values["retries"],
            "uptime": round(values["good"] / outcomes * 100, 1) if outcomes else None,
            "token_age_days": round((now - token_reference).total_seconds() / 86400, 1) if token_reference else None,
            "last_seen_at": _iso(config.last_seen_at), "activity_count": values["activity"],
        })
    return summaries


def _register() -> None:
    from .routes import main_bp

    @main_bp.route("/api/endpoints/summary")
    @auth_required
    def endpoint_summary() -> Any:
        configs = (
            WebhookConfig.query.options(selectinload(WebhookConfig.tags))
            .order_by(WebhookConfig.is_pinned.desc(), WebhookConfig.display_order, WebhookConfig.created_at.desc())
            .all()
        )
        suffix = request.args.get("token_suffix", "").strip()
        matches = [config.id for config in configs if _token_suffix_matches(config, suffix)] if suffix else []
        return jsonify({
            "generated_at": datetime.now(timezone.utc).isoformat(), "endpoints": _build_summaries(configs),
            "token_matches": matches, "token_search_hint": "Use token: followed by the final four token characters.",
        })


_register()
