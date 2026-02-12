"""
Main routes module - hub that creates main_bp and imports all sub-route modules.
Each sub-module imports main_bp and registers its routes directly on it,
so all url_for('main.xxx') references in templates continue to work.
"""
import os
from datetime import datetime, timedelta
from typing import Tuple

from flask import Blueprint, Response, jsonify, render_template, request
from sqlalchemy import func

from .extensions import db
from .models import WebhookConfig, WebhookLog
from .tasks import redis_client
from .utils import auth_required

main_bp = Blueprint('main', __name__)

# ---- Dashboard (index) ----

@main_bp.route('/')
@auth_required
def index():
    configs = WebhookConfig.query.order_by(WebhookConfig.is_pinned.desc(), WebhookConfig.display_order.asc(), WebhookConfig.created_at.desc()).all()

    last_24h = datetime.utcnow() - timedelta(hours=24)

    # Aggregated 24h counts (1 query instead of N)
    count_rows = db.session.query(
        WebhookLog.config_id, func.count(WebhookLog.id)
    ).filter(
        WebhookLog.status == 'processed',
        WebhookLog.action == 'create',
        WebhookLog.created_at >= last_24h
    ).group_by(WebhookLog.config_id).all()
    counts = {row[0]: row[1] for row in count_rows}

    # Latest log per config (1 query instead of N)
    latest_subq = db.session.query(
        WebhookLog.config_id,
        func.max(WebhookLog.created_at).label('max_created')
    ).group_by(WebhookLog.config_id).subquery()

    latest_logs = db.session.query(WebhookLog).join(
        latest_subq,
        (WebhookLog.config_id == latest_subq.c.config_id) &
        (WebhookLog.created_at == latest_subq.c.max_created)
    ).all()

    last_statuses = {}
    last_errors = {}
    for log in latest_logs:
        last_statuses[log.config_id] = log.status
        last_errors[log.config_id] = log.error_message if log.status == 'failed' else None

    # Sparkline data: counts per config per day for last 7 days (1 query instead of 7*N)
    seven_days_ago = (datetime.utcnow() - timedelta(days=7)).date()
    sparkline_rows = db.session.query(
        WebhookLog.config_id,
        func.date(WebhookLog.created_at).label('day'),
        func.count(WebhookLog.id)
    ).filter(
        func.date(WebhookLog.created_at) >= seven_days_ago
    ).group_by(WebhookLog.config_id, func.date(WebhookLog.created_at)).all()

    # Build sparkline lookup: {config_id: {date: count}}
    spark_map = {}
    for config_id, day, cnt in sparkline_rows:
        spark_map.setdefault(config_id, {})[str(day)] = cnt

    sparklines = {}
    for config in configs:
        cid = config.id
        counts.setdefault(cid, 0)
        last_statuses.setdefault(cid, 'none')
        last_errors.setdefault(cid, None)
        config_spark = []
        for i in range(6, -1, -1):
            day = str((datetime.utcnow() - timedelta(days=i)).date())
            config_spark.append(spark_map.get(cid, {}).get(day, 0))
        sparklines[cid] = config_spark

    base_url = request.url_root.rstrip('/')
    debug_mode = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'
    return render_template('index.html', configs=configs, counts=counts, last_statuses=last_statuses, last_errors=last_errors, sparklines=sparklines, base_url=base_url, debug_mode=debug_mode)


# Import sub-route modules so they register their routes on main_bp
from . import auth, endpoints, webhook, api  # noqa: E402, F401
