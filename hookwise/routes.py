"""
Main routes module - hub that creates main_bp and imports all sub-route modules.
Each sub-module imports main_bp and registers its routes directly on it,
so all url_for('main.index') references in templates continue to work.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import func

from .extensions import db
from .models import WebhookConfig, WebhookLog
from .utils import auth_required

main_bp = Blueprint("main", __name__)

_PAGE_TITLES = {
    "main.index": "Dashboard",
    "main.webhooks": "Webhooks",
    "main.webhook_detail": "Webhook",
    "main.tenantmap": "TenantMap",
    "main.history": "History",
    "main.audit_logs": "Audit Log",
    "main.settings": "Settings",
    "main.new_endpoint": "New Endpoint",
    "main.edit_endpoint": "Edit Endpoint",
    "main.setup_2fa": "Two-Factor Authentication",
    "main.admin_maintenance": "Maintenance",
    "main.login": "Sign In",
}

# ---- Dashboard (index) ----


def _get_aggregated_counts(since: Optional[datetime] = None) -> Dict[str, Dict[str, int]]:
    query = db.session.query(WebhookLog.config_id, WebhookLog.status, func.count(WebhookLog.id))
    if since:
        query = query.filter(WebhookLog.created_at >= since)
    count_rows = query.group_by(WebhookLog.config_id, WebhookLog.status).all()

    counts: Dict[str, Dict[str, int]] = {}
    for cid, status, cnt in count_rows:
        if status == "dlq":
            status = "failed"
        counts.setdefault(cid, {})
        counts[cid][status] = counts[cid].get(status, 0) + cnt
    return counts


def _get_latest_log_info() -> Tuple[Dict[str, str], Dict[str, Optional[str]]]:
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
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date()
    sparkline_rows = (
        db.session.query(WebhookLog.config_id, func.date(WebhookLog.created_at).label("day"), func.count(WebhookLog.id))
        .filter(func.date(WebhookLog.created_at) >= seven_days_ago)
        .group_by(WebhookLog.config_id, func.date(WebhookLog.created_at))
        .all()
    )

    spark_map: Dict[str, Dict[str, int]] = {}
    for config_id, day, cnt in sparkline_rows:
        spark_map.setdefault(config_id, {})[str(day)] = cnt

    sparklines = {}
    for config in configs:
        cid = config.id
        config_spark = []
        for i in range(6, -1, -1):
            day = str((datetime.now(timezone.utc) - timedelta(days=i)).date())
            config_spark.append(spark_map.get(cid, {}).get(day, 0))
        sparklines[cid] = config_spark
    return sparklines


def _get_next_stale_times(configs: List[WebhookConfig]) -> Dict[str, datetime]:
    next_stale_times = {}
    for config in configs:
        if config.timeout_alerts_enabled:
            last_activity = config.last_seen_at or config.created_at
            if last_activity:
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


def _get_navigation_notifications() -> List[Dict[str, Any]]:
    """Build the actionable notifications shown in the global navbar."""
    notifications: List[Dict[str, Any]] = []

    unhealthy_configs = (
        WebhookConfig.query.filter(
            WebhookConfig.is_draft.is_(False),
            WebhookConfig.archived_at.is_(None),
            WebhookConfig.config_health_status.in_(["WARNING", "ERROR"]),
        )
        .order_by(WebhookConfig.config_health_status.asc(), WebhookConfig.name.asc())
        .limit(4)
        .all()
    )
    for config in unhealthy_configs:
        severity = "danger" if config.config_health_status == "ERROR" else "warning"
        notifications.append(
            {
                "id": f"endpoint-health:{config.id}:{config.config_health_status}:{config.config_health_message or ''}",
                "severity": severity,
                "title": f"{config.name} needs attention",
                "message": config.config_health_message or f"Endpoint health is {config.config_health_status.lower()}.",
                "timestamp": "Endpoint health",
                "url": url_for("main.edit_endpoint", config_id=config.id),
            }
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_failures = (
        WebhookLog.query.join(WebhookConfig)
        .filter(
            WebhookConfig.is_draft.is_(False),
            WebhookLog.status.in_(["failed", "dlq"]),
            WebhookLog.created_at >= cutoff,
        )
        .order_by(WebhookLog.created_at.desc())
        .limit(6)
        .all()
    )
    for log in recent_failures:
        is_dlq = log.status == "dlq"
        notifications.append(
            {
                "id": f"webhook-failure:{log.id}:{log.status}",
                "severity": "danger" if is_dlq else "warning",
                "title": f"{'Dead-lettered' if is_dlq else 'Failed'} webhook: {log.config.name}",
                "message": log.error_message or f"Request {log.request_id} requires review.",
                "timestamp": log.created_at.strftime("%Y-%m-%d %H:%M UTC"),
                "url": url_for("main.history", status=log.status, search=log.request_id),
            }
        )

    return notifications[:8]


@main_bp.app_context_processor
def inject_navigation_context() -> Dict[str, Any]:
    """Provide a consistent page title and notifications to shared chrome."""
    context: Dict[str, Any] = {
        "page_title": _PAGE_TITLES.get(request.endpoint or "", "HookWise"),
        "navigation_notifications": [],
    }
    partial = request.headers.get("HX-Request", "").lower() == "true" or request.args.get("partial") == "true"
    if session.get("user_id") and not partial:
        try:
            context["navigation_notifications"] = _get_navigation_notifications()
        except Exception:
            current_app.logger.exception("Unable to load navigation notifications")
    return context


def _dashboard_context() -> dict[str, Any]:
    """Kontext fuer Dashboard und Webhook-Verwaltung.

    Beide Seiten zeigen Ausschnitte derselben Daten: das Dashboard die
    Kennzahlen, den Live-Feed und das Diagramm, die Webhook-Seite die Liste.
    Der Kontext wird deshalb einmal gebaut und von beiden Routen genutzt --
    die Abfragen bleiben Zeile fuer Zeile die bisherigen.
    """
    alle = WebhookConfig.query.order_by(
        WebhookConfig.is_pinned.desc(), WebhookConfig.display_order.asc(), WebhookConfig.created_at.desc()
    ).all()
    # Nr. 12: Archivierte tauchen nicht in der aktiven Liste auf; die
    # Webhook-Seite zeigt sie gesammelt in einer eigenen Archivgruppe.
    configs = [c for c in alle if c.archived_at is None]
    archived_configs = [c for c in alle if c.archived_at is not None]

    last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    counts = _get_aggregated_counts(since=last_24h)
    total_counts = _get_aggregated_counts()
    last_statuses, last_errors = _get_latest_log_info()
    sparklines = _get_sparkline_data(configs)
    next_stale_times = _get_next_stale_times(configs)

    # Ensure defaults for all configs
    for config in configs:
        cid = config.id
        counts.setdefault(cid, {"processed": 0, "failed": 0, "skipped": 0})
        total_counts.setdefault(cid, {"processed": 0, "failed": 0, "skipped": 0})
        last_statuses.setdefault(cid, "none")
        last_errors.setdefault(cid, None)

    live_configs = [config for config in configs if not config.is_draft]
    live_config_ids = {config.id for config in live_configs}
    events_24h = sum(sum(counts.get(config_id, {}).values()) for config_id in live_config_ids)
    failed_24h = sum(counts.get(config_id, {}).get("failed", 0) for config_id in live_config_ids)
    dashboard_kpis = {
        "total_endpoints": len(live_configs),
        "active_endpoints": sum(1 for config in live_configs if config.is_enabled),
        "events_24h": events_24h,
        "failed_24h": failed_24h,
    }

    base_url = request.url_root.rstrip("/")
    debug_mode = os.environ.get("DEBUG_MODE", "false").lower() == "true"
    cw_url = os.environ.get("CW_URL", "https://api-na.myconnectwise.net/v4_6_release/apis/3.0").rstrip("/")

    return dict(
        configs=configs,
        counts=counts,
        total_counts=total_counts,
        last_statuses=last_statuses,
        last_errors=last_errors,
        sparklines=sparklines,
        next_stale_times=next_stale_times,
        dashboard_kpis=dashboard_kpis,
        base_url=base_url,
        debug_mode=debug_mode,
        cw_url=cw_url,
        archived_configs=archived_configs,
    )


@main_bp.route("/")
@auth_required
def index() -> Any:
    """Ueberwachen: Kennzahlen, Live-Feed, Diagramm."""
    return render_template("index.html", **_dashboard_context())


@main_bp.route("/webhooks/<config_id>")
@auth_required
def webhook_detail(config_id: str) -> Any:
    """Frueher eine eigene Seite; die Detailansicht lebt jetzt als kompakter
    Drawer auf der Webhooks-Seite. Deep-Links bleiben gueltig: die Route
    leitet dorthin und der Drawer oeffnet sich ueber den Query-Parameter."""
    WebhookConfig.query.get_or_404(config_id)
    return redirect(url_for("main.webhooks", detail=config_id))


@main_bp.route("/favicon.ico")
def favicon_ico() -> Any:
    """Root-Fallback fuer Browser, die /favicon.ico anfragen. Ohne diese Route
    (vorher 404) blieb Chromiums Favicon-Datenbank auf dem alten Icon sitzen."""
    import os as _os
    return send_from_directory(
        _os.path.join(current_app.static_folder, "img"),
        "favicon-hook-32.png",
        mimetype="image/png",
    )


@main_bp.route("/api/webhooks/<config_id>/detail")
@auth_required
def webhook_detail_json(config_id: str) -> Any:
    """Datenquelle des Detail-Drawers: Stammdaten, 24h-Zaehler, die letzten
    40 Latenzen (fuer den Neon-Chart) und die letzten 8 Zustellungen."""
    config = WebhookConfig.query.get_or_404(config_id)
    base_url = request.url_root.rstrip("/")
    logs = (
        WebhookLog.query.filter(WebhookLog.config_id == config_id)
        .order_by(WebhookLog.created_at.desc())
        .limit(40)
        .all()
    )
    logs = list(reversed(logs))
    ms = [round((log.processing_time or 0) * 1000) for log in logs]
    seit24 = datetime.now(timezone.utc) - timedelta(hours=24)

    def anzahl(status_liste: list[str]) -> int:
        return WebhookLog.query.filter(
            WebhookLog.config_id == config_id,
            WebhookLog.created_at >= seit24,
            WebhookLog.status.in_(status_liste),
        ).count()

    sortiert = sorted(ms) or [0]
    p95 = sortiert[min(len(sortiert) - 1, int(len(sortiert) * 0.95))]
    return jsonify({
        "id": config.id,
        "name": config.name,
        "url": f"{base_url}/w/{config.id}",
        "board": config.board or "Default",
        "trigger": f"{config.trigger_field} = {config.open_value}",
        "is_enabled": bool(config.is_enabled),
        "is_pinned": bool(config.is_pinned),
        "archived": config.archived_at is not None,
        "stats": {
            "events24": anzahl(["processed", "failed", "skipped", "dlq", "queued"]),
            "failed24": anzahl(["failed", "dlq"]),
            "avg_ms": round(sum(ms) / len(ms)) if ms else 0,
            "p95_ms": p95,
        },
        "latenzen": [
            {"ms": m, "ok": log.status not in ("failed", "dlq")}
            for m, log in zip(ms, logs, strict=True)
        ],
        "deliveries": [
            {
                "ts": (log.created_at.isoformat() + ("Z" if log.created_at.tzinfo is None else "")),
                "status": log.status,
                "ms": round((log.processing_time or 0) * 1000),
                "request_id": log.request_id,
            }
            for log in reversed(logs[-8:])
        ],
    })


@main_bp.route("/webhooks")
@auth_required
def webhooks() -> Any:
    """Verwalten: Endpoint-Liste mit Filtern und Massenaktionen.

    Bisher lag die Liste auf derselben Seite wie die Ueberwachung. Beides sind
    verschiedene Taetigkeiten: wer eine Stoerung sichtet, legt keine Endpoints
    an. Die Route ist bewusst additiv -- "/" bleibt unveraendert erreichbar.
    """
    return render_template("webhooks.html", **_dashboard_context())


from . import (  # noqa: E402, F401
    activity_api,
    api,
    auth,
    dashboard_api,
    endpoint_summary,
    endpoints,
    history_ops,
    tenantmap,
    webhook,
)
