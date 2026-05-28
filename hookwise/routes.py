"""
Main routes module - hub that creates main_bp and imports all sub-route modules.
Each sub-module imports main_bp and registers its routes directly on it,
so all url_for('main.xxx') references in templates continue to work.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, render_template, request
from sqlalchemy import func

from .extensions import db
from .models import WebhookConfig, WebhookLog
from .utils import auth_required

main_bp = Blueprint("main", __name__)

# ---- Dashboard (index) ----


def _get_aggregated_counts(since: Optional[datetime] = None) -> Dict[str, Dict[str, int]]:
    """Aggregates log counts grouped by config and status."""
    query = db.session.query(WebhookLog.config_id, WebhookLog.status, func.count(WebhookLog.id))
    if since:
        query = query.filter(WebhookLog.created_at >= since)

    rows = query.group_by(WebhookLog.config_id, WebhookLog.status).all()

    counts: Dict[str, Dict[str, int]] = {}
    for cid, status, cnt in rows:
        if status == "dlq":
            status = "failed"
        counts.setdefault(cid, {})
        counts[cid][status] = counts[cid].get(status, 0) + cnt
    return counts


def _get_latest_log_info() -> Tuple[Dict[str, str], Dict[str, Optional[str]]]:
    """Retrieves the latest status and error message for each config."""
    latest_subq = (
        db.session.query(WebhookLog.config_id, func.max(WebhookLog.created_at).label("max_created"))
        .group_by(WebhookLog.config_id)
        .subquery()
    )

    latest_logs = (
        db.session.query(WebhookLog)
        .join(
            latest_subq,
            (WebhookLog.config_id == latest_subq.c.config_id) & (WebhookLog.created_at == latest_subq.c.max_created),
        )
        .all()
    )

    last_statuses = {}
    last_errors = {}
    for log in latest_logs:
        status = "failed" if log.status == "dlq" else log.status
        last_statuses[log.config_id] = status
        last_errors[log.config_id] = log.error_message if status == "failed" else None

    return last_statuses, last_errors


def _get_sparkline_data(configs: List[WebhookConfig]) -> Dict[str, List[int]]:
    """Generates activity sparklines (last 7 days) for each config."""
    now = datetime.now(timezone.utc)
    seven_days_ago = (now - timedelta(days=7)).date()

    rows = (
        db.session.query(WebhookLog.config_id, func.date(WebhookLog.created_at).label("day"), func.count(WebhookLog.id))
        .filter(func.date(WebhookLog.created_at) >= seven_days_ago)
        .group_by(WebhookLog.config_id, func.date(WebhookLog.created_at))
        .all()
    )

    spark_map: Dict[str, Dict[str, int]] = {}
    for config_id, day, cnt in rows:
        spark_map.setdefault(config_id, {})[str(day)] = cnt

    sparklines = {}
    for config in configs:
        cid = config.id
        config_spark = []
        for i in range(6, -1, -1):
            day = str((now - timedelta(days=i)).date())
            config_spark.append(spark_map.get(cid, {}).get(day, 0))
        sparklines[cid] = config_spark

    return sparklines


def _calculate_next_stale_times(configs: List[WebhookConfig]) -> Dict[str, datetime]:
    """Calculates the next expected stale alert time for relevant configs."""
    next_stale_times = {}
    for config in configs:
        if not config.timeout_alerts_enabled:
            continue

        last_activity = config.last_seen_at or config.created_at
        if not last_activity:
            continue

        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=timezone.utc)

        timeout_delta = timedelta(hours=config.timeout_hours or 24)
        next_alert_from_seen = last_activity + timeout_delta

        if config.last_stale_alert_at:
            last_alert = config.last_stale_alert_at
            if last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=timezone.utc)
            next_alert_from_alert = last_alert + timeout_delta
            next_stale_times[config.id] = max(next_alert_from_seen, next_alert_from_alert)
        else:
            next_stale_times[config.id] = next_alert_from_seen

    return next_stale_times


@main_bp.route("/")
@auth_required
def index() -> Any:
    configs = WebhookConfig.query.order_by(
        WebhookConfig.is_pinned.desc(), WebhookConfig.display_order.asc(), WebhookConfig.created_at.desc()
    ).all()

    last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = _get_aggregated_counts(since=last_24h)
    total_counts = _get_aggregated_counts()
    last_statuses, last_errors = _get_latest_log_info()
    sparklines = _get_sparkline_data(configs)
    next_stale_times = _calculate_next_stale_times(configs)

    # Initialize defaults for configs without activity
    for config in configs:
        cid = config.id
        counts.setdefault(cid, {"processed": 0, "failed": 0, "skipped": 0})
        total_counts.setdefault(cid, {"processed": 0, "failed": 0, "skipped": 0})
        last_statuses.setdefault(cid, "none")
        last_errors.setdefault(cid, None)

    base_url = request.url_root.rstrip("/")
    debug_mode = os.environ.get("DEBUG_MODE", "false").lower() == "true"
    cw_url = os.environ.get("CW_URL", "https://api-na.myconnectwise.net/v4_6_release/apis/3.0").rstrip("/")

    return render_template(
        "index.html",
        configs=configs,
        counts=counts,
        total_counts=total_counts,
        last_statuses=last_statuses,
        last_errors=last_errors,
        sparklines=sparklines,
        next_stale_times=next_stale_times,
        base_url=base_url,
        debug_mode=debug_mode,
        cw_url=cw_url,
    )


# Import sub-route modules so they register their routes on main_bp
from . import api, auth, endpoints, tenantmap, webhook  # noqa: E402, F401
