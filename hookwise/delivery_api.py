"""Manual delivery testing and side-effect-free routing preview routes."""

import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, request

from .models import WebhookConfig, WebhookLog
from .services.delivery_queue import commit_and_dispatch, stage_delivery
from .services.routing import evaluate_routing
from .tasks import is_in_maintenance, process_webhook_task
from .utils import auth_required, log_to_web


def register_delivery_routes(blueprint: Blueprint) -> None:
    @auth_required
    def test_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        request_id = f"test_{int(time.time())}_{secrets.token_hex(4)}"
        data = {
            "monitor": {"name": f"Test Monitor for {config.name}"},
            "status": "0",
            "msg": "Common test message for webhook verification",
            "heartbeat": {"status": "0"},
            "title": "Manual Test Trigger",
            "message": "This is a simulated webhook payload.",
        }
        # Nr. 16: der statische Payload traf nur Endpoints, deren Trigger
        # zufaellig heartbeat.status ist. Der Test setzt jetzt zusaetzlich den
        # KONFIGURIERTEN Trigger-Pfad dieses Endpoints auf seinen open_value --
        # damit prueft er den echten Routing-Weg, nicht ein Beispiel.
        if config.trigger_field and config.open_value is not None:
            pfad = str(config.trigger_field)
            # trigger_field ist ein JSONPath: fuehrendes "$." abstreifen.
            # Index-Pfade ([0]) lassen sich in einen frischen Payload nicht
            # sinnvoll synthetisieren -- dann bleibt der Beispiel-Payload.
            if pfad.startswith("$."):
                pfad = pfad[2:]
            elif pfad.startswith("$"):
                pfad = pfad[1:]
            teile = [teil for teil in pfad.split(".") if teil]
            if teile and not any("[" in teil for teil in teile):
                # open_value kann eine Kommaliste sein -- der Test setzt den
                # ersten Wert, wie ihn ein einzelner echter Alarm liefert.
                wert = str(config.open_value).split(",")[0].strip()
                ziel: Any = data
                for teil in teile[:-1]:
                    if not isinstance(ziel.get(teil), dict):
                        ziel[teil] = {}
                    ziel = ziel[teil]
                ziel[teile[-1]] = wert
        now = datetime.now(timezone.utc)
        log_entry = WebhookLog(
            config_id=config_id,
            request_id=request_id,
            correlation_id=request_id[:100],
            payload=json.dumps(data),
            status="pending_enqueue",
            source_ip="system",
            received_at=now,
            queued_at=now,
        )
        outbox = stage_delivery(log_entry, data, source_ip="system")
        if not commit_and_dispatch(outbox, process_webhook_task):
            return jsonify({"status": "enqueue_failed", "message": "Test retained for retry"}), 503
        log_to_web(f"Manual test triggered for {config.name} (ID: {request_id})", "info", config.name, data=data)
        return jsonify({"status": "success", "message": "Test webhook queued", "request_id": request_id})

    @auth_required
    def dry_run_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "JSON object body is required"}), 400
        maintenance_active = is_in_maintenance(config)
        steps: list[dict[str, Any]] = [
            {
                "step": "Maintenance Window",
                "active": maintenance_active,
                "result": "skipped" if maintenance_active else "ok",
            }
        ]
        if maintenance_active:
            return jsonify({"action": "skip", "reason": "maintenance_window", "steps": steps})
        routing_config = config.to_dict()
        routing_config["ticket_prefix"] = config.ticket_prefix or os.environ.get("CW_TICKET_PREFIX", "Alert:")
        decision = evaluate_routing(data, routing_config)
        steps.extend(decision.steps)
        return jsonify(
            {
                "action": decision.action,
                "alert_type": decision.alert_type,
                "ticket_summary": decision.summary,
                "description": decision.values.get("description") or data.get("msg", ""),
                "company_id": decision.values.get("customer_id", config.customer_id_default or ""),
                "board": decision.values.get("board", config.board or ""),
                "steps": steps,
            }
        )

    blueprint.add_url_rule(
        "/endpoint/test/<config_id>", endpoint="test_endpoint", view_func=test_endpoint, methods=["POST"]
    )
    blueprint.add_url_rule(
        "/endpoint/dry-run/<config_id>", endpoint="dry_run_endpoint", view_func=dry_run_endpoint, methods=["POST"]
    )
