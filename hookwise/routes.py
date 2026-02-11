import ipaddress
import json
import os
import secrets
import time
from typing import Tuple

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from .extensions import db
from .models import WebhookConfig, WebhookLog
from .tasks import PSA_TASK_COUNT, celery, cw_client, process_webhook_task, redis_client
from .utils import auth_required, decrypt_string, encrypt_string, log_audit, log_to_web

main_bp = Blueprint('main', __name__)

WEBHOOK_COUNT = Counter('hookwise_webhooks_received_total', 'Total webhooks received', ['status', 'config_name'])
QUEUE_SIZE = Gauge('hookwise_celery_queue_size', 'Approximate number of tasks in queue')

@main_bp.route('/')
@auth_required
def index():
    configs = WebhookConfig.query.order_by(WebhookConfig.created_at.desc()).all()
    base_url = request.url_root.rstrip('/')
    debug_mode = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'
    return render_template('index.html', configs=configs, base_url=base_url, debug_mode=debug_mode)

@main_bp.route('/endpoint/new', methods=['GET', 'POST'])
@auth_required
def new_endpoint():
    if request.method == 'POST':
        config = WebhookConfig(
            name=request.form.get('name'),
            customer_id_default=request.form.get('customer_id_default'),
            board=request.form.get('board'),
            status=request.form.get('status'),
            ticket_type=request.form.get('ticket_type'),
            subtype=request.form.get('subtype'),
            item=request.form.get('item'),
            priority=request.form.get('priority'),
            trigger_field=request.form.get('trigger_field') or "heartbeat.status",
            open_value=request.form.get('open_value') or "0",
            close_value=request.form.get('close_value') or "1",
            ticket_prefix=request.form.get('ticket_prefix'),
            json_mapping=request.form.get('json_mapping'),
            routing_rules=request.form.get('routing_rules'),
            maintenance_windows=request.form.get('maintenance_windows'),
            trusted_ips=request.form.get('trusted_ips')
        )
        db.session.add(config)
        db.session.commit()
        log_audit("create", config.id, f"Endpoint {config.name} created")
        flash(f'Endpoint "{config.name}" created successfully!')
        return redirect(url_for('main.index'))
    return render_template('form.html')

@main_bp.route('/endpoint/edit/<id>', methods=['GET', 'POST'])
@auth_required
def edit_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    if request.method == 'POST':
        config.name = request.form.get('name')
        config.customer_id_default = request.form.get('customer_id_default')
        config.board = request.form.get('board')
        config.status = request.form.get('status')
        config.ticket_type = request.form.get('ticket_type')
        config.subtype = request.form.get('subtype')
        config.item = request.form.get('item')
        config.priority = request.form.get('priority')
        config.trigger_field = request.form.get('trigger_field') or "heartbeat.status"
        config.open_value = request.form.get('open_value') or "0"
        config.close_value = request.form.get('close_value') or "1"
        config.ticket_prefix = request.form.get('ticket_prefix')
        config.json_mapping = request.form.get('json_mapping')
        config.routing_rules = request.form.get('routing_rules')
        config.maintenance_windows = request.form.get('maintenance_windows')
        config.trusted_ips = request.form.get('trusted_ips')
        
        db.session.commit()
        log_audit("update", config.id, f"Endpoint {config.name} updated")
        flash(f'Endpoint "{config.name}" updated successfully!')
        return redirect(url_for('main.index'))
    return render_template('form.html', config=config)

@main_bp.route('/endpoint/toggle/<id>', methods=['POST'])
@auth_required
def toggle_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    config.is_enabled = not config.is_enabled
    db.session.commit()
    action = "enable" if config.is_enabled else "disable"
    log_audit(action, id, f"Endpoint {config.name} {action}d")
    return jsonify({"status": "success", "is_enabled": config.is_enabled})

@main_bp.route('/endpoint/rotate-token/<id>', methods=['POST'])
@auth_required
def rotate_token(id):
    config = WebhookConfig.query.get_or_404(id)
    new_token = secrets.token_urlsafe(32)
    config.bearer_token = encrypt_string(new_token)
    from datetime import datetime
    config.last_rotated_at = datetime.utcnow()
    db.session.commit()
    log_audit("rotate_token", id, f"Token for {config.name} rotated")
    flash(f'Token for "{config.name}" rotated successfully!')
    return redirect(url_for('main.index'))

@main_bp.route('/endpoint/delete/<id>', methods=['POST'])
@auth_required
def delete_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    name = config.name
    db.session.delete(config)
    db.session.commit()
    log_audit("delete", id, f"Endpoint {name} deleted")
    flash(f'Endpoint "{name}" deleted.')
    return redirect(url_for('main.index'))

@main_bp.route('/endpoint/bulk/delete', methods=['POST'])
@auth_required
def bulk_delete_endpoints():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400
    WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    log_audit("bulk_delete", None, f"Deleted endpoints: {', '.join(ids)}")
    return jsonify({"status": "success", "message": f"Deleted {len(ids)} endpoints"})

@main_bp.route('/endpoint/bulk/pause', methods=['POST'])
@auth_required
def bulk_pause_endpoints():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400
    WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).update({"is_enabled": False}, synchronize_session=False)
    db.session.commit()
    log_audit("bulk_pause", None, f"Paused endpoints: {', '.join(ids)}")
    return jsonify({"status": "success", "message": f"Paused {len(ids)} endpoints"})

@main_bp.route('/endpoint/bulk/resume', methods=['POST'])
@auth_required
def bulk_resume_endpoints():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400
    WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).update({"is_enabled": True}, synchronize_session=False)
    db.session.commit()
    log_audit("bulk_resume", None, f"Resumed endpoints: {', '.join(ids)}")
    return jsonify({"status": "success", "message": f"Resumed {len(ids)} endpoints"})

@main_bp.route('/w/<config_id>', methods=['POST'])
def dynamic_webhook(config_id: str) -> Tuple[Response, int]:
    request_id = g.request_id
    config = WebhookConfig.query.get(config_id)
    if not config:
        return jsonify({"status": "error", "message": "Endpoint not found"}), 404
    
    if not config.is_enabled:
        WEBHOOK_COUNT.labels(status='disabled', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "Endpoint is disabled"}), 403
    
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        WEBHOOK_COUNT.labels(status='unauthorized', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "Missing Bearer Token"}), 401
    
    token = auth_header.split(' ')[1]
    if token != decrypt_string(config.bearer_token):
        WEBHOOK_COUNT.labels(status='unauthorized', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "Invalid Bearer Token"}), 401

    # IP Whitelisting
    if config.trusted_ips:
        client_ip = request.remote_addr
        trusted = False
        for trusted_range in [ip.strip() for ip in config.trusted_ips.split(',')]:
            try:
                if ipaddress.ip_address(client_ip) in ipaddress.ip_network(trusted_range):
                    trusted = True
                    break
            except ValueError:
                continue
        if not trusted:
            WEBHOOK_COUNT.labels(status='forbidden', config_name=config.name).inc()
            return jsonify({"status": "error", "message": f"IP {client_ip} not allowed"}), 403

    data = request.json
    if not data:
        WEBHOOK_COUNT.labels(status='bad_request', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "No JSON payload", "request_id": request_id}), 400

    process_webhook_task.delay(config_id, data, request_id)
    WEBHOOK_COUNT.labels(status='queued', config_name=config.name).inc()
    log_to_web(f"Webhook received and queued (ID: {request_id})", "info", config.name, data=data)
    return jsonify({"status": "queued", "message": "Webhook received", "request_id": request_id}), 202

@main_bp.route('/history')
@auth_required
def history():
    page = request.args.get('page', 1, type=int)
    per_page = 25
    pagination = WebhookLog.query.order_by(WebhookLog.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('history.html', pagination=pagination, logs=pagination.items)

@main_bp.route('/history/replay/<log_id>', methods=['POST'])
@auth_required
def replay_webhook(log_id):
    log_entry = WebhookLog.query.get_or_404(log_id)
    try:
        data = json.loads(log_entry.payload)
        request_id = f"replay_{int(time.time())}_{log_entry.request_id[:8]}"
        process_webhook_task.delay(log_entry.config_id, data, request_id)
        log_to_web(f"REPLAY triggered for {log_entry.config.name} (Original ID: {log_entry.request_id})", "info", log_entry.config.name, data=data)
        return jsonify({"status": "success", "message": "Replay queued", "request_id": request_id})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@main_bp.route('/history/delete/<id>', methods=['POST'])
@auth_required
def delete_log(id):
    log_entry = WebhookLog.query.get_or_404(id)
    db.session.delete(log_entry)
    db.session.commit()
    return jsonify({"status": "success"})

    db.session.commit()
    return jsonify({"status": "success"})

@main_bp.route('/endpoint/test/<id>', methods=['POST'])
@auth_required
def test_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    request_id = f"test_{int(time.time())}"
    
    # Dummy payload for testing
    data = {
        "monitor": {"name": f"Test Monitor for {config.name}"},
        "status": "0",
        "msg": "Common test message for webhook verification",
        "heartbeat": {"status": "0"},
        "title": "Manual Test Trigger",
        "message": "This is a simulated webhook payload."
    }
    
    process_webhook_task.delay(id, data, request_id)
    log_to_web(f"Manual test triggered for {config.name} (ID: {request_id})", "info", config.name, data=data)
    return jsonify({"status": "success", "message": "Test webhook queued", "request_id": request_id})

@main_bp.route('/api/stats')
@auth_required
def get_stats():
    from datetime import datetime
    from datetime import time as dtime
    today_start = datetime.combine(datetime.utcnow().date(), dtime.min)
    
    tickets_created = WebhookLog.query.filter(
        WebhookLog.status == 'processed',
        WebhookLog.action.in_(['create', 'update']),
        WebhookLog.created_at >= today_start
    ).count()

    tickets_closed = WebhookLog.query.filter(
        WebhookLog.status == 'processed',
        WebhookLog.action == 'close',
        WebhookLog.created_at >= today_start
    ).count()
    
    failed_attempts = WebhookLog.query.filter(
        WebhookLog.status.in_(['failed', 'dlq']),
        WebhookLog.created_at >= today_start
    ).count()

    total_today = WebhookLog.query.filter(WebhookLog.created_at >= today_start).count()
    success_rate = (tickets_created / total_today * 100) if total_today > 0 else 100

    return jsonify({
        "created_today": tickets_created,
        "closed_today": tickets_closed,
        "failed_today": failed_attempts,
        "success_rate": round(success_rate, 1)
    })

@main_bp.route('/api/stats/history')
@auth_required
def get_stats_history():
    from datetime import datetime, timedelta
    days = 7
    history = []
    for i in range(days):
        date = (datetime.utcnow() - timedelta(days=i)).date()
        count = WebhookLog.query.filter(
            db.func.date(WebhookLog.created_at) == date,
            WebhookLog.status == 'processed'
        ).count()
        history.append({"date": date.strftime('%m-%d'), "count": count})
    return jsonify(history[::-1])

@main_bp.route('/api/cw/boards')
@auth_required
def get_cw_boards():
    cache_key = "hookwise_cw_boards"
    cached = redis_client.get(cache_key)
    if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    boards = cw_client.get_boards()
    redis_client.set(cache_key, json.dumps(boards), ex=3600) # 1 hour cache
    return jsonify(boards)

@main_bp.route('/api/cw/priorities')
@auth_required
def get_cw_priorities():
    cache_key = "hookwise_cw_priorities"
    cached = redis_client.get(cache_key)
    if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    priorities = cw_client.get_priorities()
    redis_client.set(cache_key, json.dumps(priorities), ex=86400) # 24 hour cache
    return jsonify(priorities)

@main_bp.route('/api/cw/statuses/<board_id>')
@auth_required
def get_cw_statuses(board_id):
    cache_key = f"hookwise_cw_statuses_{board_id}"
    cached = redis_client.get(cache_key)
    if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    statuses = cw_client.get_board_statuses(board_id)
    redis_client.set(cache_key, json.dumps(statuses), ex=3600)
    return jsonify(statuses)

@main_bp.route('/api/cw/companies')
@auth_required
def get_cw_companies():
    search = request.args.get('search')
    if not search:
        # For general list, maybe cache first 50
        cache_key = "hookwise_cw_companies_default"
        cached = redis_client.get(cache_key)
        if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    companies = cw_client.get_companies(search=search)
    if not search:
        redis_client.set("hookwise_cw_companies_default", json.dumps(companies), ex=3600)
    return jsonify(companies)

@main_bp.route('/readyz', methods=['GET'])
def readyz() -> Tuple[Response, int]:
    try:
        # Check DB
        db.session.execute(db.text('SELECT 1'))
        # Check Redis
        redis_client.ping()
        return jsonify({"status": "ready"}), 200
    except Exception as e:
        return jsonify({"status": "not ready", "reason": str(e)}), 503

@main_bp.route('/health', methods=['GET'])
def health() -> Tuple[Response, int]:
    try:
        redis_client.ping()
        db.session.execute(db.text('SELECT 1'))
        return jsonify({"status": "ok", "timestamp": time.time()}), 200
    except Exception:
        return jsonify({"status": "error", "message": "Service unreachable"}), 503

@main_bp.route('/health/services', methods=['GET'])
def health_services() -> Tuple[Response, int]:
    health_data = {"redis": "down", "database": "down", "celery": "down", "timestamp": time.time()}
    status_code = 200
    
    try:
        redis_client.ping()
        health_data["redis"] = "up"
    except Exception as e:
        current_app.logger.error(f"Redis health check failed: {e}")
        status_code = 503

    try:
        db.session.execute(db.text('SELECT 1'))
        health_data["database"] = "up"
    except Exception as e:
        current_app.logger.error(f"Database health check failed: {e}")
        status_code = 503

    try:
        inspect = celery.control.inspect()
        stats = inspect.stats()
        active = inspect.active()
        health_data["celery"] = "up" if stats else "warning"
        health_data["celery_active"] = sum(len(tasks) for tasks in active.values()) if active else 0
    except Exception as e:
        current_app.logger.error(f"Celery health check failed: {e}")
        health_data["celery"] = "down"

    return jsonify(health_data), status_code

@main_bp.route('/metrics', methods=['GET'])
def metrics() -> Response:
    try:
        # Approximate queue size from Redis
        size = redis_client.llen('celery')
        QUEUE_SIZE.set(size)
    except Exception:
        pass
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
