"""Webhook ingestion route."""

import ipaddress
import json
from typing import Any

from flask import g, jsonify, request
from prometheus_client import Counter

from .extensions import csrf, db, limiter
from .metrics import log_webhook_received
from .models import WebhookConfig, WebhookLog
from .tasks import process_webhook_task
from .utils import decrypt_string, log_to_web, mask_secrets

WEBHOOK_COUNT = Counter("hookwise_webhooks_received_total", "Total webhooks received", ["status", "config_name"])


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

        def _log_rejection(error_msg: str) -> None:
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
                    processing_time=0.0
                )
                db.session.add(log_entry)
                db.session.commit()
            except Exception as _e:
                import logging
                logging.getLogger(__name__).error(f"Failed to log webhook rejection: {_e}")
                db.session.rollback()

        if not config.is_enabled:
            log_webhook_received(status="disabled", config_name=config.name)
            _log_rejection("Endpoint is disabled")
            return jsonify({"status": "error", "message": "Endpoint is disabled"}), 403

        if config.bearer_auth_enabled:
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                log_webhook_received(status="unauthorized", config_name=config.name)
                _log_rejection("Missing Bearer Token")
                return jsonify({"status": "error", "message": "Missing Bearer Token"}), 401

            token = auth_header.split(" ")[1]
            import hmac as _hmac

            if not _hmac.compare_digest(token, decrypt_string(config.bearer_token)):
                log_webhook_received(status="unauthorized", config_name=config.name)
                _log_rejection("Invalid Bearer Token")
                return jsonify({"status": "error", "message": "Invalid Bearer Token"}), 401

        # HMAC Signature Verification
        if config.hmac_secret:
            import hashlib
            import hmac

            signature = request.headers.get("X-HookWise-Signature")
            if not signature:
                _log_rejection("Missing HMAC Signature")
                return jsonify({"status": "error", "message": "Missing HMAC Signature"}), 401

            computed = hmac.HMAC(config.hmac_secret.encode(), request.data, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(computed, signature):
                _log_rejection("Invalid HMAC Signature")
                return jsonify({"status": "error", "message": "Invalid HMAC Signature"}), 401

        # IP Whitelisting
        if config.trusted_ips:
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
                log_webhook_received(status="forbidden", config_name=config.name)
                _log_rejection(f"IP {client_ip} not allowed")
                return jsonify({"status": "error", "message": f"IP {client_ip} not allowed"}), 403

        data = request.json
        if not data:
            log_webhook_received(status="bad_request", config_name=config.name)
            _log_rejection("No JSON payload")
            return jsonify({"status": "error", "message": "No JSON payload", "request_id": request_id}), 400

        headers = dict(request.headers)
        headers.pop("Authorization", None)
        headers.pop("Cookie", None)

        process_webhook_task.delay(config_id, data, request_id, source_ip=request.remote_addr, headers=headers)
        log_webhook_received(status="queued", config_name=config.name)
        log_to_web(f"Webhook received and queued (ID: {request_id})", "info", config.name, data=data)
        return jsonify({"status": "queued", "message": "Webhook received", "request_id": request_id}), 202


_register()
