import ipaddress
import json
import os
import secrets
import time
from typing import Tuple

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, url_for, session
from werkzeug.security import check_password_hash, generate_password_hash
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from .extensions import db, limiter
from .models import WebhookConfig, WebhookLog, User
from .tasks import PSA_TASK_COUNT, celery, cw_client, process_webhook_task, redis_client
from .utils import auth_required, decrypt_string, encrypt_string, log_audit, log_to_web

main_bp = Blueprint('main', __name__)

import pyotp
import segno
import io

@main_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            if user.is_2fa_enabled:
                session['pending_user_id'] = user.id
                return redirect(url_for('main.login_2fa'))
            
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            log_audit("login", None, f"User {username} logged in")
            return redirect(url_for('main.index'))
        
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@main_bp.route('/login/2fa', methods=['GET', 'POST'])
def login_2fa():
    if 'pending_user_id' not in session:
        return redirect(url_for('main.login'))
    
    if request.method == 'POST':
        otp = request.form.get('otp')
        user = User.query.get(session['pending_user_id'])
        
        if user and pyotp.TOTP(user.otp_secret).verify(otp):
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            session.pop('pending_user_id')
            log_audit("login_2fa", None, f"User {user.username} logged in with 2FA")
            return redirect(url_for('main.index'))
        
        flash('Invalid 2FA code', 'danger')
    
    return render_template('login_2fa.html')

@main_bp.route('/settings/2fa/setup', methods=['GET', 'POST'])
@auth_required
def setup_2fa():
    user = User.query.get(session['user_id'])
    if user.is_2fa_enabled:
        flash('2FA is already enabled', 'info')
        return redirect(url_for('main.settings'))
    
    if request.method == 'POST':
        otp = request.form.get('otp')
        secret = session.get('pending_otp_secret')
        if secret and pyotp.TOTP(secret).verify(otp):
            user.otp_secret = secret
            user.is_2fa_enabled = True
            db.session.commit()
            session.pop('pending_otp_secret')
            log_audit("2fa_enabled", None, f"User {user.username} enabled 2FA")
            flash('2FA has been enabled successfully!', 'success')
            return redirect(url_for('main.settings'))
        flash('Invalid 2FA code', 'danger')

    # GET: Generate secret and QR code
    secret = pyotp.random_base32()
    session['pending_otp_secret'] = secret
    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="HookWise")
    
    # Generate QR code as data URI
    qr = segno.make(totp_uri)
    out = io.BytesIO()
    qr.save(out, kind='png', scale=5)
    qr_data = f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode()}"
    
    return render_template('setup_2fa.html', qr_data=qr_data, secret=secret)

@main_bp.route('/settings/2fa/disable', methods=['POST'])
@auth_required
def disable_2fa():
    user = User.query.get(session['user_id'])
    user.is_2fa_enabled = False
    user.otp_secret = None
    db.session.commit()
    log_audit("2fa_disabled", None, f"User {user.username} disabled 2FA")
    flash('2FA has been disabled.', 'warning')
    return redirect(url_for('main.settings'))

@main_bp.route('/logout')
def logout():
    username = session.get('username')
    session.clear()
    log_audit("logout", None, f"User {username} logged out")
    return redirect(url_for('main.login'))

WEBHOOK_COUNT = Counter('hookwise_webhooks_received_total', 'Total webhooks received', ['status', 'config_name'])
QUEUE_SIZE = Gauge('hookwise_celery_queue_size', 'Approximate number of tasks in queue')

@main_bp.route('/')
@auth_required
def index():
    from datetime import datetime, timedelta
    configs = WebhookConfig.query.order_by(WebhookConfig.is_pinned.desc(), WebhookConfig.display_order.asc(), WebhookConfig.created_at.desc()).all()
    
    # Calculate 24h ticket counts, last status, and sparkline data
    last_24h = datetime.utcnow() - timedelta(hours=24)
    counts = {}
    last_statuses = {}
    last_errors = {}
    sparklines = {}
    for config in configs:
        count = WebhookLog.query.filter(
            WebhookLog.config_id == config.id,
            WebhookLog.status == 'processed',
            WebhookLog.action == 'create',
            WebhookLog.created_at >= last_24h
        ).count()
        counts[config.id] = count
        
        last_log = WebhookLog.query.filter(WebhookLog.config_id == config.id).order_by(WebhookLog.created_at.desc()).first()
        last_statuses[config.id] = last_log.status if last_log else 'none'
        last_errors[config.id] = last_log.error_message if last_log and last_log.status == 'failed' else None

        # Sparkline (Last 7 days)
        config_sparkline = []
        for i in range(7):
            date = (datetime.utcnow() - timedelta(days=i)).date()
            day_count = WebhookLog.query.filter(
                WebhookLog.config_id == config.id,
                db.func.date(WebhookLog.created_at) == date
            ).count()
            config_sparkline.append(day_count)
        sparklines[config.id] = config_sparkline[::-1]

    base_url = request.url_root.rstrip('/')
    debug_mode = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'
    return render_template('index.html', configs=configs, counts=counts, last_statuses=last_statuses, last_errors=last_errors, sparklines=sparklines, base_url=base_url, debug_mode=debug_mode)

@main_bp.route('/endpoint/toggle-pin/<id>', methods=['POST'])
@auth_required
def toggle_pin(id):
    config = WebhookConfig.query.get_or_404(id)
    config.is_pinned = not config.is_pinned
    db.session.commit()
    action = "pin" if config.is_pinned else "unpin"
    log_audit(action, id, f"Endpoint {config.name} {action}ned")
    return jsonify({"status": "success", "is_pinned": config.is_pinned})

@main_bp.route('/endpoint/reorder', methods=['POST'])
@auth_required
def reorder_endpoints():
    order = request.json.get('order', [])
    for index, config_id in enumerate(order):
        config = WebhookConfig.query.get(config_id)
        if config:
            config.display_order = index
    db.session.commit()
    return jsonify({"status": "success"})

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
            description_template=request.form.get('description_template'),
            json_mapping=request.form.get('json_mapping'),
            routing_rules=request.form.get('routing_rules'),
            maintenance_windows=request.form.get('maintenance_windows'),
            trusted_ips=request.form.get('trusted_ips'),
            is_draft=request.form.get('is_draft') == 'true',
            ai_routing_enabled=request.form.get('ai_routing_enabled') == 'true',
            ai_rca_enabled=request.form.get('ai_rca_enabled') == 'true',
            ai_prompt_template=request.form.get('ai_prompt_template')
        )
        db.session.add(config)
        db.session.commit()
        log_audit("create", config.id, f"Endpoint {config.name} created")
        flash(f'Endpoint "{config.name}" {"saved as draft" if config.is_draft else "created successfully"}!')
        
        if request.form.get('create_another') == 'true':
            return redirect(url_for('main.new_endpoint', confetti='true'))
        return redirect(url_for('main.index', confetti='true'))
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
        config.is_draft = request.form.get('is_draft') == 'true'
        config.ai_routing_enabled = request.form.get('ai_routing_enabled') == 'true'
        config.ai_rca_enabled = request.form.get('ai_rca_enabled') == 'true'
        config.ai_prompt_template = request.form.get('ai_prompt_template')

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

@main_bp.route('/endpoint/quick-update/<id>', methods=['POST'])
@auth_required
def quick_update_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    field = request.json.get('field')
    value = request.json.get('value')
    
    if field in ['board', 'priority']:
        setattr(config, field, value)
        db.session.commit()
        log_audit("quick_update", id, f"Endpoint {config.name} {field} updated to {value}")
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Invalid field"}), 400

@main_bp.route('/endpoint/clone/<id>', methods=['POST'])
@auth_required
def clone_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    new_config = WebhookConfig(
        name=f"{config.name} (Copy)",
        customer_id_default=config.customer_id_default,
        board=config.board,
        status=config.status,
        ticket_type=config.ticket_type,
        subtype=config.subtype,
        item=config.item,
        priority=config.priority,
        trigger_field=config.trigger_field,
        open_value=config.open_value,
        close_value=config.close_value,
        ticket_prefix=config.ticket_prefix,
        json_mapping=config.json_mapping,
        routing_rules=config.routing_rules,
        maintenance_windows=config.maintenance_windows,
        trusted_ips=config.trusted_ips
    )
    # Generate a new token for the clone
    new_config.bearer_token = encrypt_string(secrets.token_urlsafe(32))
    
    db.session.add(new_config)
    db.session.commit()
    log_audit("clone", new_config.id, f"Endpoint {new_config.name} cloned from {config.id}")
    flash(f'Endpoint "{config.name}" cloned successfully!')
    return redirect(url_for('main.index'))

@main_bp.route('/endpoint/token/<id>')
@auth_required
def get_endpoint_token(id):
    config = WebhookConfig.query.get_or_404(id)
    return jsonify({"token": decrypt_string(config.bearer_token)})

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

@main_bp.route('/endpoint/bulk/export', methods=['POST'])
@auth_required
def bulk_export_endpoints():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400
    configs = WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).all()
    export_data = [c.to_dict() for c in configs]
    # Remove sensitive/internal fields for export
    for c in export_data:
        c.pop('bearer_token', None)
        c.pop('id', None)
        c.pop('created_at', None)
        c.pop('last_seen_at', None)
    
    return Response(
        json.dumps(export_data, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=hookwise_export.json'}
    )

@main_bp.route('/history/delete-all', methods=['POST'])
@auth_required
def delete_all_logs():
    WebhookLog.query.delete()
    db.session.commit()
    return jsonify({"status": "success", "message": "All logs deleted"})

@main_bp.route('/history/bulk-delete', methods=['POST'])
@auth_required
def bulk_delete_logs():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400
    WebhookLog.query.filter(WebhookLog.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"status": "success"})

@main_bp.route('/w/<config_id>', methods=['POST'])
@limiter.limit("60 per minute")
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

    # HMAC Signature Verification
    if config.hmac_secret:
        import hmac
        import hashlib
        signature = request.headers.get('X-HookWise-Signature')
        if not signature:
            return jsonify({"status": "error", "message": "Missing HMAC Signature"}), 401
        
        computed = hmac.new(config.hmac_secret.encode(), request.data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, signature):
            return jsonify({"status": "error", "message": "Invalid HMAC Signature"}), 401

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

    headers = dict(request.headers)
    # Remove large or sensitive headers
    headers.pop('Authorization', None)
    headers.pop('Cookie', None)

    process_webhook_task.delay(config_id, data, request_id, source_ip=request.remote_addr, headers=headers)
    WEBHOOK_COUNT.labels(status='queued', config_name=config.name).inc()
    log_to_web(f"Webhook received and queued (ID: {request_id})", "info", config.name, data=data)
    return jsonify({"status": "queued", "message": "Webhook received", "request_id": request_id}), 202

@main_bp.route('/history')
@auth_required
def history():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    per_page = 25
    
    query = WebhookLog.query
    if search:
        if search.startswith('#'):
            search_id = search[1:]
            if search_id.isdigit():
                query = query.filter(WebhookLog.ticket_id == int(search_id))
        elif search.isdigit():
            query = query.filter(WebhookLog.ticket_id == int(search))
        else:
            query = query.filter(
                (WebhookLog.request_id.ilike(f"%{search}%")) |
                (WebhookLog.payload.ilike(f"%{search}%")) |
                (WebhookLog.error_message.ilike(f"%{search}%"))
            )
            
    if date_from:
        from datetime import datetime
        query = query.filter(WebhookLog.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        from datetime import datetime, timedelta
        query = query.filter(WebhookLog.created_at <= datetime.fromisoformat(date_to) + timedelta(days=1))

    pagination = query.order_by(WebhookLog.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    debug_mode = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'
    
    if request.args.get('partial') == 'true':
        return render_template('history_rows.html', logs=pagination.items)
        
    return render_template('history.html', pagination=pagination, logs=pagination.items, search=search, date_from=date_from, date_to=date_to, debug_mode=debug_mode)

@main_bp.route('/audit')
@auth_required
def audit_logs():
    from .models import AuditLog
    page = request.args.get('page', 1, type=int)
    per_page = 50
    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('audit.html', pagination=pagination, logs=pagination.items)

@main_bp.route('/history/replay/<log_id>', methods=['POST'])
@auth_required
def replay_webhook(log_id):
    log_entry = WebhookLog.query.get_or_404(log_id)
    try:
        data = json.loads(log_entry.payload)
        request_id = f"replay_{int(time.time())}_{log_entry.request_id[:8]}"
        process_webhook_task.delay(log_entry.config_id, data, request_id)
        log_to_web(f"REPLAY started (Original: {log_entry.request_id[:8]})", "info", log_entry.config.name, data=data)
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
    
    tickets_created = WebhookLog.query.join(WebhookConfig).filter(
        WebhookConfig.is_draft == False,
        WebhookLog.status == 'processed',
        WebhookLog.action.in_(['create', 'update']),
        WebhookLog.created_at >= today_start
    ).count()

    tickets_closed = WebhookLog.query.join(WebhookConfig).filter(
        WebhookConfig.is_draft == False,
        WebhookLog.status == 'processed',
        WebhookLog.action == 'close',
        WebhookLog.created_at >= today_start
    ).count()
    
    failed_attempts = WebhookLog.query.join(WebhookConfig).filter(
        WebhookConfig.is_draft == False,
        WebhookLog.status.in_(['failed', 'dlq']),
        WebhookLog.created_at >= today_start
    ).count()

    total_today = WebhookLog.query.join(WebhookConfig).filter(
        WebhookConfig.is_draft == False,
        WebhookLog.created_at >= today_start
    ).count()
    success_rate = (tickets_created / total_today * 100) if total_today > 0 else 100

    # Calculate Avg Processing Time
    from sqlalchemy import func
    avg_proc = db.session.query(func.avg(WebhookLog.processing_time)).filter(
        WebhookLog.created_at >= today_start,
        WebhookLog.status == 'processed'
    ).scalar() or 0

    return jsonify({
        "created_today": tickets_created,
        "closed_today": tickets_closed,
        "failed_today": failed_attempts,
        "success_rate": round(success_rate, 1),
        "avg_processing_time": round(float(avg_proc), 2)
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

@main_bp.route('/api/cw/types/<board_id>')
@auth_required
def get_cw_types(board_id):
    cache_key = f"hookwise_cw_types_{board_id}"
    cached = redis_client.get(cache_key)
    if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    types = cw_client.get_board_types(board_id)
    redis_client.set(cache_key, json.dumps(types), ex=3600)
    return jsonify(types)

@main_bp.route('/api/cw/subtypes/<board_id>')
@auth_required
def get_cw_subtypes(board_id):
    cache_key = f"hookwise_cw_subtypes_{board_id}"
    cached = redis_client.get(cache_key)
    if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    subtypes = cw_client.get_board_subtypes(board_id)
    redis_client.set(cache_key, json.dumps(subtypes), ex=3600)
    return jsonify(subtypes)

@main_bp.route('/api/cw/items/<board_id>')
@auth_required
def get_cw_items(board_id):
    cache_key = f"hookwise_cw_items_{board_id}"
    cached = redis_client.get(cache_key)
    if cached: return cached.decode(), 200, {'Content-Type': 'application/json'}
    
    items = cw_client.get_board_items(board_id)
    redis_client.set(cache_key, json.dumps(items), ex=3600)
    return jsonify(items)

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
@limiter.exempt
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

@main_bp.route('/admin/maintenance', methods=['GET', 'POST'])
@auth_required
def maintenance_mode():
    if request.method == 'POST':
        current = redis_client.get('hookwise_maintenance_mode')
        new_state = 'true' if not current or current.decode() != 'true' else 'false'
        redis_client.set('hookwise_maintenance_mode', new_state)
        log_audit("maintenance_toggle", None, f"Maintenance mode set to {new_state}")
        return jsonify({"status": "success", "maintenance_mode": new_state == 'true'})
    
    mode = redis_client.get('hookwise_maintenance_mode')
    return jsonify({"maintenance_mode": mode and mode.decode() == 'true'})

@main_bp.route('/settings')
@auth_required
def settings():
    retention = redis_client.get('hookwise_log_retention_days')
    retention = retention.decode() if retention else os.environ.get('LOG_RETENTION_DAYS', '30')
    
    health_webhook = redis_client.get('hookwise_health_webhook')
    health_webhook = health_webhook.decode() if health_webhook else ''
    
    api_key = redis_client.get('hookwise_master_api_key')
    api_key = api_key.decode() if api_key else 'Not Generated'
    
    user = User.query.get(session['user_id'])
    
    return render_template('settings.html', log_retention_days=retention, master_api_key=api_key, health_webhook=health_webhook, user_2fa_enabled=user.is_2fa_enabled)

@main_bp.route('/settings/update', methods=['POST'])
@auth_required
def update_settings():
    retention = request.form.get('log_retention_days')
    health_webhook = request.form.get('health_webhook')
    
    if retention: redis_client.set('hookwise_log_retention_days', retention)
    if health_webhook: redis_client.set('hookwise_health_webhook', health_webhook)
    
    flash('Settings updated successfully!')
    return redirect(url_for('main.settings'))

@main_bp.route('/admin/generate-api-key', methods=['POST'])
@auth_required
def generate_api_key():
    import secrets
    new_key = secrets.token_urlsafe(64)
    redis_client.set('hookwise_master_api_key', new_key)
    log_audit("generate_master_api_key", None, "New master API key generated")
    return jsonify({"status": "success", "api_key": new_key})

@main_bp.route('/admin/backup', methods=['GET'])
@auth_required
def backup_config():
    configs = WebhookConfig.query.all()
    data = [c.to_dict() for c in configs]
    return Response(
        json.dumps(data, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=hookwise_backup.json'}
    )

@main_bp.route('/admin/restore', methods=['POST'])
@auth_required
def restore_config():
    file = request.files.get('backup_file')
    if not file: return jsonify({"status": "error", "message": "No file"}), 400
    try:
        data = json.load(file)
        for c in data:
            config = WebhookConfig.query.get(c['id'])
            if not config:
                config = WebhookConfig(id=c['id'])
                db.session.add(config)
            
            fields = ['name', 'customer_id_default', 'board', 'status', 'ticket_type', 'subtype', 'item', 'priority', 'trigger_field', 'open_value', 'close_value', 'ticket_prefix', 'json_mapping', 'routing_rules', 'maintenance_windows', 'trusted_ips', 'is_enabled', 'is_pinned', 'is_draft', 'ai_routing_enabled', 'ai_rca_enabled', 'ai_prompt_template']
            for f in fields:
                if f in c: setattr(config, f, c[f])
        
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@main_bp.route('/api/feedback', methods=['POST'])
@auth_required
def submit_feedback():
    data = request.json
    message = data.get('message')
    # In a real app, send email or log to DB. Here we log to audit.
    log_audit("feedback_submitted", None, f"Feedback: {message} | UA: {data.get('ua')}")
    return jsonify({"status": "success"})

@main_bp.route('/api/debug/process', methods=['POST'])
@auth_required
def debug_process():
    from .utils import resolve_jsonpath
    import re
    
    data = request.json.get('payload')
    config_data = request.json.get('config', {})
    
    if not data:
        return jsonify({"status": "error", "message": "No sample payload provided"}), 400
        
    steps = []
    results = {}
    
    # 1. Trigger Logic
    trigger_field = config_data.get('trigger_field', 'heartbeat.status')
    actual_val = str(resolve_jsonpath(data, trigger_field))
    steps.append(f"Trigger field '{trigger_field}' resolved to: '{actual_val}'")
    
    open_val = config_data.get('open_value', '0')
    close_val = config_data.get('close_value', '1')
    
    if actual_val in [v.strip() for v in open_val.split(',')]:
        results['alert_type'] = "OPEN (DOWN)"
    elif actual_val in [v.strip() for v in close_val.split(',')]:
        results['alert_type'] = "CLOSE (UP)"
    else:
        results['alert_type'] = "GENERIC"
    steps.append(f"Alert type determined as: {results['alert_type']}")

    # 2. JSON Mapping
    mapping_str = config_data.get('json_mapping')
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

    # 3. Regex Routing
    rules_str = config_data.get('routing_rules')
    if rules_str:
        try:
            rules = json.loads(rules_str)
            for i, rule in enumerate(rules):
                path = rule.get('path')
                regex = rule.get('regex')
                if path and regex:
                    val = str(resolve_jsonpath(data, path))
                    if re.search(regex, val, re.IGNORECASE):
                        steps.append(f"Rule {i+1} matched: '{regex}' on '{path}' (value: '{val}')")
                        overrides = rule.get('overrides', {})
                        for k, v in overrides.items():
                            results[k] = v
                            steps.append(f"Override applied: {k} -> {v}")
                    else:
                        steps.append(f"Rule {i+1} did NOT match: '{regex}' on '{path}'")
        except Exception as e:
            steps.append(f"Error parsing Routing Rules: {e}")

    # 4. Source & Summary
    monitor = data.get('monitor', {})
    monitor_name = monitor.get('name', data.get('title', data.get('name', 'Unknown Source')))
    prefix = config_data.get('ticket_prefix', 'Alert:')
    results['summary'] = results.get('summary') or (f"{prefix} {monitor_name}" if prefix else monitor_name)
    steps.append(f"Final Ticket Summary: '{results['summary']}'")

    # 5. Company Mapping
    company_id_match = re.search(r'#CW(\w+)', monitor_name)
    results['company'] = results.get('customer_id') or (company_id_match.group(1) if company_id_match else config_data.get('customer_id_default'))
    steps.append(f"Target Company Identifier: '{results['company']}'")

    return jsonify({
        "status": "success",
        "steps": steps,
        "results": results
    })

@main_bp.route('/metrics', methods=['GET'])
def metrics() -> Response:
    try:
        # Approximate queue size from Redis
        size = redis_client.llen('celery')
        QUEUE_SIZE.set(size)
    except Exception:
        pass
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
