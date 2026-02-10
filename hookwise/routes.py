import time
import os
from typing import Tuple
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, Response, current_app
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter

from .extensions import db, socketio
from .models import WebhookConfig
from .utils import log_to_web
from .tasks import process_webhook_task, redis_client, celery

main_bp = Blueprint('main', __name__)

WEBHOOK_COUNT = Counter('hookwise_webhooks_received_total', 'Total webhooks received', ['status', 'config_name'])

@main_bp.route('/')
def index():
    configs = WebhookConfig.query.order_by(WebhookConfig.created_at.desc()).all()
    base_url = request.url_root.rstrip('/')
    return render_template('index.html', configs=configs, base_url=base_url)

@main_bp.route('/endpoint/new', methods=['GET', 'POST'])
def new_endpoint():
    if request.method == 'POST':
        config = WebhookConfig(
            name=request.form.get('name'),
            customer_id_default=request.form.get('customer_id_default'),
            board=request.form.get('board'),
            status=request.form.get('status'),
            ticket_type=request.form.get('ticket_type'),
            subtype=request.form.get('subtype'),
            priority=request.form.get('priority'),
            trigger_field=request.form.get('trigger_field') or "heartbeat.status",
            open_value=request.form.get('open_value') or "0",
            close_value=request.form.get('close_value') or "1",
            ticket_prefix=request.form.get('ticket_prefix')
        )
        db.session.add(config)
        db.session.commit()
        flash(f'Endpoint "{config.name}" created successfully!')
        return redirect(url_for('main.index'))
    return render_template('form.html')

@main_bp.route('/endpoint/edit/<id>', methods=['GET', 'POST'])
def edit_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    if request.method == 'POST':
        config.name = request.form.get('name')
        config.customer_id_default = request.form.get('customer_id_default')
        config.board = request.form.get('board')
        config.status = request.form.get('status')
        config.ticket_type = request.form.get('ticket_type')
        config.subtype = request.form.get('subtype')
        config.priority = request.form.get('priority')
        config.trigger_field = request.form.get('trigger_field') or "heartbeat.status"
        config.open_value = request.form.get('open_value') or "0"
        config.close_value = request.form.get('close_value') or "1"
        config.ticket_prefix = request.form.get('ticket_prefix')
        
        db.session.commit()
        flash(f'Endpoint "{config.name}" updated successfully!')
        return redirect(url_for('main.index'))
    return render_template('form.html', config=config)

@main_bp.route('/endpoint/delete/<id>', methods=['POST'])
def delete_endpoint(id):
    config = WebhookConfig.query.get_or_404(id)
    db.session.delete(config)
    db.session.commit()
    flash(f'Endpoint "{config.name}" deleted.')
    return redirect(url_for('main.index'))

@main_bp.route('/endpoint/bulk/delete', methods=['POST'])
def bulk_delete_endpoints():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({"status": "error", "message": "No IDs provided"}), 400
    WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"status": "success", "message": f"Deleted {len(ids)} endpoints"})

@main_bp.route('/endpoint/bulk/pause', methods=['POST'])
def bulk_pause_endpoints():
    return jsonify({"status": "success", "message": "Bulk pause action triggered (Simulation)"})

@main_bp.route('/w/<config_id>', methods=['POST'])
def dynamic_webhook(config_id: str) -> Tuple[Response, int]:
    request_id = g.request_id
    config = WebhookConfig.query.get(config_id)
    if not config:
        return jsonify({"status": "error", "message": "Endpoint not found"}), 404
    
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        WEBHOOK_COUNT.labels(status='unauthorized', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "Missing Bearer Token"}), 401
    
    token = auth_header.split(' ')[1]
    if token != config.bearer_token:
        WEBHOOK_COUNT.labels(status='unauthorized', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "Invalid Bearer Token"}), 401

    data = request.json
    if not data:
        WEBHOOK_COUNT.labels(status='bad_request', config_name=config.name).inc()
        return jsonify({"status": "error", "message": "No JSON payload", "request_id": request_id}), 400

    process_webhook_task.delay(config_id, data, request_id)
    WEBHOOK_COUNT.labels(status='queued', config_name=config.name).inc()
    log_to_web(f"Webhook received and queued (ID: {request_id})", "info", config.name, data=data)
    return jsonify({"status": "queued", "message": "Webhook received", "request_id": request_id}), 202

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
        health_data["celery"] = "up" if stats else "warning"
    except Exception as e:
        current_app.logger.error(f"Celery health check failed: {e}")
        health_data["celery"] = "down"

    return jsonify(health_data), status_code

@main_bp.route('/metrics', methods=['GET'])
def metrics() -> Response:
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
