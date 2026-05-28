"""Webhook ingestion route."""

import hashlib
import hmac
import ipaddress
import json
import logging
from typing import Any, Optional

from flask import g, jsonify, request
from prometheus_client import Counter

from .extensions import csrf, db, limiter
from .metrics import log_webhook_received
from .models import WebhookConfig, WebhookLog
from .tasks import process_webhook_task
from .utils import decrypt_string, log_to_web, mask_secrets

WEBHOOK_COUNT = Counter("hookwise_webhooks_received_total", "Total webhooks received", ["status", "config_name"])

logger = logging.getLogger(__name__)


def _log_webhook_rejection(config_id: str, request_id: str, error_msg: str, config_name: str) -> None:
    """Log a rejected webhook attempt to the database and update metrics."""
    try:
        try:
            payload_data = request.get_json(silent=True)
            if payload_data is not None:
                payload_str = json.dumps(mask_secrets(payload_data))
            else:
                payload_str = request.get_data(as_text=True) or "{}"
        except Exception:
            payload_str = request.get_data(as_text=True) or "{}"

        headers_dict = dict(request.headers)
        headers_dict.pop("Authorization", None)
        headers_dict.pop("Cookie", None)

        log_entry = WebhookLog(
            config_id=config_id,
            request_id=request_id,
            payload=payload_str,
            headers=json.dumps(mask_secrets(headers_dict)),
            source_ip=request.remote_addr,
            status="failed",
            error_message=error_msg,
            processing_time=0.0,
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as _e:
        logger.error(f"Failed to log webhook rejection: {_e}")
        db.session.rollback()


def _check_bearer_auth(config: WebhookConfig) -> Optional[str]:
    """Check Bearer Token authentication. Returns error message if failed, else None."""
    if not config.bearer_auth_enabled:
        return None

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return "Missing Bearer Token"

    token = auth_header.split(" ")[1]
    if not hmac.compare_digest(token, decrypt_string(config.bearer_token)):
        return "Invalid Bearer Token"

    return None


def _check_hmac_signature(config: WebhookConfig) -> Optional[str]:
    """Check HMAC Signature. Returns error message if failed, else None."""
    if not config.hmac_secret:
        return None

    signature = request.headers.get("X-HookWise-Signature")
    if not signature:
        return "Missing HMAC Signature"

    computed = hmac.HMAC(config.hmac_secret.encode(), request.data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, signature):
        return "Invalid HMAC Signature"

    return None


def _check_ip_whitelist(config: WebhookConfig) -> Optional[str]:
    """Check IP whitelisting. Returns error message if failed, else None."""
    if not config.trusted_ips:
        return None

    client_ip = request.remote_addr
    trusted = False
    for trusted_range in [ip.strip() for ip in config.trusted_ips.split(",")]:
        try:
            if client_ip and ipaddress.ip_address(client_ip) in ipaddress.ip_network(trusted_range):
                trusted = True
                break
        except ValueError:
            continue

    if not trusted:
        return f"IP {client_ip} not allowed"

    return None


def _register() -> None:
    from .routes import main_bp

    @main_bp.route("/w/<config_id>", methods=["POST"])
    @csrf.exempt
    @limiter.limit("60 per minute")
    def dynamic_webhook(config_id: str) -> Any:
        request_id = g.request_id
        config = WebhookConfig.query.get(config_id)
        if not config:
            return jsonify({"status": "error", "message": "Endpoint not found"}), 404

        if not config.is_enabled:
            log_webhook_received(status="disabled", config_name=config.name)
            _log_webhook_rejection(config_id, request_id, "Endpoint is disabled", config.name)
            return jsonify({"status": "error", "message": "Endpoint is disabled"}), 403

        # Authentication & Security Checks
        auth_error = _check_bearer_auth(config)
        if auth_error:
            log_webhook_received(status="unauthorized", config_name=config.name)
            _log_webhook_rejection(config_id, request_id, auth_error, config.name)
            return jsonify({"status": "error", "message": auth_error}), 401

        hmac_error = _check_hmac_signature(config)
        if hmac_error:
            _log_webhook_rejection(config_id, request_id, hmac_error, config.name)
            return jsonify({"status": "error", "message": hmac_error}), 401

        ip_error = _check_ip_whitelist(config)
        if ip_error:
            log_webhook_received(status="forbidden", config_name=config.name)
            _log_webhook_rejection(config_id, request_id, ip_error, config.name)
            return jsonify({"status": "error", "message": ip_error}), 403

        data = request.json
        if not data:
            log_webhook_received(status="bad_request", config_name=config.name)
            _log_webhook_rejection(config_id, request_id, "No JSON payload", config.name)
            return jsonify({"status": "error", "message": "No JSON payload", "request_id": request_id}), 400

        headers = dict(request.headers)
        headers.pop("Authorization", None)
        headers.pop("Cookie", None)

        process_webhook_task.delay(config_id, data, request_id, source_ip=request.remote_addr, headers=headers)
        log_webhook_received(status="queued", config_name=config.name)
        log_to_web(f"Webhook received and queued (ID: {request_id})", "info", config.name, data=data)
        return jsonify({"status": "queued", "message": "Webhook received", "request_id": request_id}), 202


_register()
