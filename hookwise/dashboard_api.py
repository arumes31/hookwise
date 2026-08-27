"""Read-only, bounded dashboard metrics and analytics endpoints."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import jsonify, request, session
from sqlalchemy import case, func

from .extensions import db
from .models import User, UserPreference, WebhookConfig, WebhookLog
from .utils import auth_required

_RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}
_MAX_CUSTOM_DAYS = 366
_MAX_ANALYTICS_ROWS = 50_000
_KPI_KEYS = {
    "total_endpoints", "active_endpoints", "total_events", "processed_events", "success_rate",
    "average_latency", "dead_letter_queue", "skipped_no_action", "failed_events", "failing_endpoints",
    "stale_endpoints",
}
_REFRESH_INTERVALS = {0, 15, 30, 60, 300}


def _parse_window() -> tuple[datetime, datetime, str]:
    """Return a validated UTC window; custom ranges are capped to one year."""
    now = datetime.now(timezone.utc)
    range_name = request.args.get("range", "24h")
    if range_name in _RANGES:
        return now - _RANGES[range_name], now, range_name
    if range_name != "custom":
        raise ValueError("range must be one of 24h, 7d, 30d, 90d, or custom")
    try:
        start = datetime.fromisoformat(request.args["from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(request.args["to"].replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise ValueError("custom ranges require ISO-8601 from and to values") from exc
    start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
    end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end.astimezone(timezone.utc)
    if start >= end or end > now + timedelta(minutes=1) or end - start > timedelta(days=_MAX_CUSTOM_DAYS):
        raise ValueError("custom range must be ordered, end no later than now, and at most 366 days")
    return start, end, "custom"


def _timezone() -> ZoneInfo:
    name = request.args.get("timezone", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _base_logs(start: datetime, end: datetime) -> Any:
    return (
        WebhookLog.query.join(WebhookConfig)
        .filter(
            WebhookConfig.is_draft.is_(False),
            WebhookLog.created_at >= start,
            WebhookLog.created_at < end,
        )
    )


def _metric_values(start: datetime, end: datetime) -> dict[str, float | int]:
    logs = _base_logs(start, end)
    total = logs.count()
    processed = logs.filter(WebhookLog.status == "processed").count()
    failures = logs.filter(WebhookLog.status.in_(["failed", "dlq"])).count()
    dlq = logs.filter(WebhookLog.status == "dlq").count()
    skipped = logs.filter(WebhookLog.status == "skipped").count()
    latency = (
        logs.filter(WebhookLog.processing_time.is_not(None))
        .with_entities(func.avg(WebhookLog.processing_time))
        .scalar()
        or 0
    )
    return {
        "total_events": total,
        "processed_events": processed,
        "successful_events": processed + skipped,
        "success_rate": round((processed + skipped) / total * 100, 1) if total else 100.0,
        "average_latency": round(float(latency), 3),
        "dead_letter_queue": dlq,
        "skipped_no_action": skipped,
        "failed_events": failures,
    }


def _percentile(values: list[float], percent: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)


def _stale_config_ids(now: datetime) -> list[str]:
    configs = WebhookConfig.query.filter(
        WebhookConfig.is_draft.is_(False), WebhookConfig.timeout_alerts_enabled.is_(True)
    ).all()
    stale: list[str] = []
    for config in configs:
        seen = config.last_seen_at or config.created_at
        if seen is None:
            continue
        seen = seen.replace(tzinfo=timezone.utc) if seen.tzinfo is None else seen.astimezone(timezone.utc)
        if seen + timedelta(hours=max(config.timeout_hours or 24, 1)) < now:
            stale.append(config.id)
    return stale


def _overview() -> Any:
    try:
        start, end, range_name = _parse_window()
    except ValueError:
        return jsonify({"error": "Invalid dashboard range."}), 400
    current = _metric_values(start, end)
    duration = end - start
    previous = _metric_values(start - duration, start)
    config_rows = WebhookConfig.query.filter(WebhookConfig.is_draft.is_(False)).all()
    all_ids = [config.id for config in config_rows]
    active_ids = [config.id for config in config_rows if config.is_enabled]
    log_rows = (
        _base_logs(start, end)
        .with_entities(WebhookLog.config_id, WebhookLog.status, WebhookLog.processing_time)
        .group_by(WebhookLog.config_id, WebhookLog.status, WebhookLog.processing_time)
        .all()
    )
    activity_ids = sorted({row[0] for row in log_rows})
    processed_ids = sorted({row[0] for row in log_rows if row[1] == "processed"})
    failed_ids = sorted({row[0] for row in log_rows if row[1] in {"failed", "dlq"}})
    successful_ids = sorted({row[0] for row in log_rows if row[1] in {"processed", "skipped"}})
    skipped_ids = sorted({row[0] for row in log_rows if row[1] == "skipped"})
    dlq_ids = sorted({row[0] for row in log_rows if row[1] == "dlq"})
    latency_ids = sorted({row[0] for row in log_rows if row[2] is not None})
    stale_ids = _stale_config_ids(end)
    current.update(
        {
            "total_endpoints": len(all_ids),
            "active_endpoints": len(active_ids),
            "failing_endpoints": len(failed_ids),
            "stale_endpoints": len(stale_ids),
        }
    )
    deltas: dict[str, float | None] = {}
    for key in (
        "total_events",
        "processed_events",
        "success_rate",
        "average_latency",
        "dead_letter_queue",
        "skipped_no_action",
        "failed_events",
    ):
        old, new = float(previous[key]), float(current[key])
        deltas[key] = None if old == 0 else round((new - old) / old * 100, 1)
    return jsonify(
        {
            "kpis": current,
            "deltas": deltas,
            "range": range_name,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "updated_at": end.isoformat(),
            "filters": {
                "total_endpoints": all_ids,
                "active_endpoints": active_ids,
                "total_events": activity_ids,
                "processed_events": processed_ids,
                "success_rate": successful_ids,
                "average_latency": latency_ids,
                "dead_letter_queue": dlq_ids,
                "skipped_no_action": skipped_ids,
                "failed_events": failed_ids,
                "failing_endpoints": failed_ids,
                "stale_endpoints": stale_ids,
            },
        }
    )


def _analytics() -> Any:
    try:
        start, end, range_name = _parse_window()
        tz = _timezone()
    except ValueError:
        return jsonify({"error": "Invalid dashboard analytics parameters."}), 400
    logs = _base_logs(start, end).order_by(WebhookLog.created_at.asc()).limit(_MAX_ANALYTICS_ROWS + 1).all()
    if len(logs) > _MAX_ANALYTICS_ROWS:
        return jsonify({"error": "This range contains too many events; choose a shorter range."}), 413
    buckets: dict[str, list[WebhookLog]] = defaultdict(list)
    for log in logs:
        created = log.created_at.replace(tzinfo=timezone.utc) if log.created_at.tzinfo is None else log.created_at
        buckets[created.astimezone(tz).strftime("%Y-%m-%d")].append(log)
    points: list[dict[str, Any]] = []
    for key in sorted(buckets):
        rows = buckets[key]
        total = len(rows)
        failures = sum(row.status in {"failed", "dlq"} for row in rows)
        processed = sum(row.status == "processed" for row in rows)
        successful = sum(row.status in {"processed", "skipped"} for row in rows)
        times = [float(row.processing_time) for row in rows if row.processing_time is not None]
        points.append(
            {
                "label": key,
                "volume": total,
                "processed": processed,
                "successful": successful,
                "failed": failures,
                "failure_rate": round(failures / total * 100, 1) if total else 0,
                "average_latency": round(mean(times), 3) if times else 0,
                "p50": _percentile(times, 50),
                "p95": _percentile(times, 95),
                "p99": _percentile(times, 99),
            }
        )
    volumes = [point["volume"] for point in points]
    failure_rates = [point["failure_rate"] for point in points]
    volume_threshold = (
        mean(volumes) + 2 * (sum((x - mean(volumes)) ** 2 for x in volumes) / len(volumes)) ** 0.5
        if len(volumes) > 1
        else float("inf")
    )


    failure_threshold = (
        mean(failure_rates)
        + 2 * (sum((x - mean(failure_rates)) ** 2 for x in failure_rates) / len(failure_rates)) ** 0.5
        if len(failure_rates) > 1
        else float("inf")
    )
    for point in points:
        point["anomaly"] = point["volume"] >= volume_threshold or point["failure_rate"] >= failure_threshold
        point["busiest"] = bool(points and point["volume"] == max(volumes))
        point["highest_failure"] = bool(points and point["failure_rate"] == max(failure_rates) and point["failed"] > 0)
    endpoint_rows = (
        _base_logs(start, end)
        .with_entities(
            WebhookLog.config_id,
            WebhookConfig.name,
            func.count(WebhookLog.id),
            func.sum(case((WebhookLog.status.in_(["failed", "dlq"]), 1), else_=0)),
        )
        .group_by(WebhookLog.config_id, WebhookConfig.name)
        .order_by(func.count(WebhookLog.id).desc())
        .limit(12)
        .all()
    )
    return jsonify(
        {
            "range": range_name,
            "timezone": str(tz),
            "points": points,
            "endpoint_activity": [
                {"id": row[0], "name": row[1], "processed": row[2], "failed": int(row[3] or 0)} for row in endpoint_rows
            ],
            "updated_at": end.isoformat(),
        }
    )


def _preferences() -> Any:
    user_id = str(session["user_id"])
    row = UserPreference.query.filter_by(user_id=user_id).first()
    defaults = {
        "layout": [], "hidden": [], "compact": False, "interval": 30,
        "timezone": "UTC", "activity_buffer_size": 200,
        "browser_notifications_enabled": False, "sound_notifications_enabled": False,
    }
    if request.method == "GET":
        if not row:
            return jsonify(defaults)
        try:
            layout = json.loads(row.dashboard_layout or "[]")
            hidden = json.loads(row.hidden_kpis or "[]")
        except json.JSONDecodeError:
            layout, hidden = [], []
        return jsonify({
            "layout": layout, "hidden": hidden, "compact": row.dashboard_compact_mode,
            "interval": row.dashboard_refresh_interval, "timezone": row.timezone or "UTC",
            "activity_buffer_size": row.activity_buffer_size,
            "browser_notifications_enabled": row.browser_notifications_enabled,
            "sound_notifications_enabled": row.sound_notifications_enabled,
        })
    if request.method == "DELETE":
        if row:
            db.session.delete(row)
            db.session.commit()
        return jsonify(defaults)
    body = request.get_json(silent=True) or {}
    layout, hidden = body.get("layout", []), body.get("hidden", [])
    interval = body.get("interval", 30)
    timezone_name = body.get("timezone", "UTC")
    buffer_size = body.get("activity_buffer_size", 200)
    if not isinstance(layout, list) or set(layout) - _KPI_KEYS or len(layout) != len(set(layout)):
        return jsonify({"error": "Invalid KPI layout."}), 400
    if not isinstance(hidden, list) or set(hidden) - _KPI_KEYS:
        return jsonify({"error": "Invalid hidden KPI list."}), 400
    if interval not in _REFRESH_INTERVALS or buffer_size not in {100, 200, 500}:
        return jsonify({"error": "Invalid refresh interval or activity buffer size."}), 400
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return jsonify({"error": "Invalid timezone."}), 400
    if not User.query.filter_by(id=user_id).first():
        return jsonify({"error": "Preferences require a database-backed user."}), 409
    row = row or UserPreference(user_id=user_id)
    row.dashboard_layout = json.dumps(layout)
    row.hidden_kpis = json.dumps(hidden)
    row.dashboard_compact_mode = bool(body.get("compact", False))
    row.dashboard_refresh_interval = interval
    row.timezone = timezone_name
    row.activity_buffer_size = buffer_size
    row.browser_notifications_enabled = bool(body.get("browser_notifications_enabled", False))
    row.sound_notifications_enabled = bool(body.get("sound_notifications_enabled", False))
    db.session.add(row)
    db.session.commit()
    return jsonify({"status": "saved"})


def _register() -> None:
    from .routes import main_bp

    main_bp.add_url_rule("/api/dashboard/overview", "dashboard_overview", auth_required(_overview))
    main_bp.add_url_rule("/api/dashboard/analytics", "dashboard_analytics", auth_required(_analytics))
    main_bp.add_url_rule(
        "/api/dashboard/preferences", "dashboard_preferences", auth_required(_preferences),
        methods=["GET", "PATCH", "DELETE"],
    )


_register()
