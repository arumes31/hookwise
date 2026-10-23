"""Explicit forwarding dry-runs and durable test sends, without ticket side effects."""

import json
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import CustomerWebhook, CustomerWebhookDelivery, GlobalMapping, WebhookLog
from .rbac import current_permissions, requires
from .routes import main_bp
from .services.customer_webhooks import (
    _alert_key,
    _sanitized_alert,
    _target_key,
    build_alert_payload,
    evaluate_customer_webhook,
    preview_delivery_payload,
)
from .utils import auth_required, log_audit

MAX_TEST_PAYLOAD_BYTES = 256 * 1024


def _reject_non_json_constant(value: str) -> Any:
    raise ValueError(f"Ungültiger JSON-Wert: {value}")


def _sample_payload(hook: CustomerWebhook) -> dict[str, Any]:
    mapping = GlobalMapping.query.filter_by(company_id=hook.company_id).order_by(GlobalMapping.tenant_value).first()
    tenant = (
        mapping.tenant_value if mapping and not any(c in mapping.tenant_value for c in "*?") else "customer.example"
    )
    return {
        "Tenant": tenant,
        "TaskInfo": {"Command": "Get-CIPPAlertDefenderIncidents", "Tenant": "AllTenants"},
        "Results": [
            {
                "IncidentID": "hookwise-test",
                "IncidentName": "Hookwise Testalarm",
                "Severity": "informational",
                "Tenant": tenant,
            }
        ],
    }


def _load_history(hook: CustomerWebhook) -> tuple[dict[str, Any] | None, str]:
    if not request.args.get("log_id") and request.args.get("latest") != "1":
        return None, ""
    if "history:read" not in current_permissions():
        abort(403)
    query = WebhookLog.query.filter_by(config_id=hook.config_id).filter(
        (WebhookLog.error_type.is_(None)) | (WebhookLog.error_type != "request_rejected"),
    )
    if request.args.get("log_id"):
        log = query.filter_by(id=request.args["log_id"]).first_or_404()
    else:
        log = query.order_by(WebhookLog.created_at.desc()).first()
    if log is None:
        return None, "Für diesen Quell-Endpoint gibt es noch keine gespeicherte Meldung."
    try:
        if len(log.payload.encode()) > MAX_TEST_PAYLOAD_BYTES:
            raise ValueError
        data = json.loads(log.payload, parse_constant=_reject_non_json_constant)
        if not isinstance(data, dict):
            raise ValueError
        data = _sanitized_alert(data)
    except ValueError, TypeError, RecursionError:
        return None, "Diese gespeicherte Meldung eignet sich nicht als Test-Payload."
    return data, f"Meldung {log.request_id} aus dem Quell-Endpoint geladen."


@main_bp.route("/customer-webhooks/<webhook_id>/test", methods=["GET", "POST"])
@auth_required
@requires("endpoint:test")
def test_customer_webhook(webhook_id: str) -> Any:
    # A test can trigger an external customer's alarm. Preserve this boundary
    # even when permission rollout is configured to log or skip other checks.
    if "endpoint:test" not in current_permissions():
        return jsonify({"message": "Insufficient permissions", "required": "endpoint:test"}), 403
    hook = db.get_or_404(CustomerWebhook, webhook_id)
    evaluation = None
    previews: list[str] = []
    targets: list[str | None] = []
    error = None
    duplicates = 0
    source_note = (
        "Beispiel einer CIPP-Meldung ohne numerischen Score. Eigene Payload einfügen oder letzte Meldung laden."
    )
    payload = _sample_payload(hook)
    if request.method == "GET":
        historical, note = _load_history(hook)
        if historical is not None:
            payload = historical
        if note:
            source_note = note
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    tokens = dict(session.get("forwarding_test_tokens", {}))
    if request.method == "GET":
        # Keep only a bounded set of test forms in the signed session.
        tokens = dict(list(tokens.items())[-9:])
        tokens[hook.id] = {"value": secrets.token_hex(16), "generation": hook.destination_generation}
        session["forwarding_test_tokens"] = tokens
    token_record = tokens.get(hook.id, {})
    # String records from a pre-upgrade session must not authorize a send.
    if not isinstance(token_record, dict):
        token_record = {}
    test_token = token_record.get("value", "")
    status_code = 200
    if request.method == "POST":
        source_note = "Prüfung der eingefügten Payload gegen die gespeicherte Konfiguration."
        try:
            raw = request.form.get("payload", "")
            if len(raw.encode("utf-8")) > MAX_TEST_PAYLOAD_BYTES:
                raise ValueError("Die Test-Payload darf höchstens 256 KiB groß sein.")
            try:
                payload = json.loads(raw, parse_constant=_reject_non_json_constant)
            except ValueError, RecursionError:
                raise ValueError("Bitte gültiges JSON einfügen.") from None
            if not isinstance(payload, dict):
                raise ValueError("Die Test-Payload muss ein JSON-Objekt sein.")
            payload_text = json.dumps(_sanitized_alert(payload), ensure_ascii=False, indent=2)
            evaluation = evaluate_customer_webhook(hook, payload)
            matches, urls = evaluation.matches or [], evaluation.targets or []
            targets = [urlsplit(url).hostname for url in urls]
            if len(matches) * len(urls) > 100:
                raise ValueError("Maximal 100 Zustellungen pro Test. Bitte die Payload verkleinern.")
            for score, alert in matches:
                previews.append(
                    json.dumps(
                        preview_delivery_payload(hook, build_alert_payload(hook, "dry-run", score, alert, test=True)),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                key = _alert_key(hook.company_id, alert)
                duplicates += sum(
                    CustomerWebhookDelivery.query.filter_by(
                        webhook_id=hook.id,
                        event_key=_target_key(key, index),
                    ).count()
                    for index in range(len(urls))
                )
            action = request.form.get("action", "preview")
            if action not in ("preview", "send"):
                raise ValueError("Ungültige Testaktion.")
            if action == "send":
                if request.form.get("confirm_send") != "on":
                    raise ValueError("Bitte den tatsächlichen Versand an die gespeicherten Ziele bestätigen.")
                submitted_token = request.form.get("test_token", "")
                if not test_token or not secrets.compare_digest(submitted_token, test_token):
                    raise ValueError("Das Testformular ist abgelaufen. Seite neu laden und erneut prüfen.")
                if token_record.get("generation") != hook.destination_generation:
                    raise ValueError("Die Zustellziele wurden geändert. Seite neu laden und die Ziele erneut prüfen.")
                if not evaluation.eligible:
                    raise ValueError(evaluation.message)
                # Serialize with target edits; never send to URLs supplied by the test form.
                hook = CustomerWebhook.query.filter_by(id=hook.id).populate_existing().with_for_update().one()
                if token_record.get("generation") != hook.destination_generation:
                    raise ValueError("Die Zustellziele wurden geändert. Seite neu laden und die Ziele erneut prüfen.")
                evaluation = evaluate_customer_webhook(hook, payload)
                if not evaluation.eligible:
                    raise ValueError(evaluation.message)
                if len(evaluation.matches or []) * len(evaluation.targets or []) > 100:
                    raise ValueError("Maximal 100 Zustellungen pro Test. Bitte die Payload verkleinern.")
                count = 0
                for alert_index, (score, alert) in enumerate(evaluation.matches or []):
                    body = build_alert_payload(hook, f"test-{test_token}", score, alert, test=True)
                    for index in range(len(evaluation.targets or [])):
                        key = f"test:{test_token}:{index}:{alert_index}"
                        try:
                            with db.session.begin_nested():
                                db.session.add(
                                    CustomerWebhookDelivery(
                                        webhook_id=hook.id,
                                        event_key=key,
                                        target_index=index,
                                        destination_generation=hook.destination_generation,
                                        payload=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                                    )
                                )
                                db.session.flush()
                            count += 1
                        except IntegrityError:
                            continue
                log_audit(
                    "forwarding_test",
                    hook.config_id,
                    f"Connector {hook.id}; {count} test deliveries queued",
                    commit=False,
                )
                db.session.commit()
                batches = dict(list(dict(session.get("forwarding_test_batches", {})).items())[-9:])
                batches[hook.id] = [*batches.get(hook.id, [])[-4:], test_token]
                session["forwarding_test_batches"] = batches
                flash(f"{count} Testzustellungen vorgemerkt. Worker und Beat übernehmen den Versand.", "success")
                return redirect(url_for("main.test_customer_webhook", webhook_id=hook.id, batch=test_token))
        except ValueError as exc:
            db.session.rollback()
            error, status_code = str(exc), 400
        except RecursionError:
            db.session.rollback()
            error, status_code = "Die JSON-Payload ist zu tief verschachtelt.", 400
    batch = request.args.get("batch", "")
    test_deliveries = []
    if re.fullmatch(r"[a-f0-9]{32}", batch):
        own_batches = session.get("forwarding_test_batches", {}).get(hook.id, [])
        if batch not in own_batches and "history:read" not in current_permissions():
            abort(403)
        test_deliveries = (
            CustomerWebhookDelivery.query.filter(
                CustomerWebhookDelivery.webhook_id == hook.id,
                CustomerWebhookDelivery.event_key.like(f"test:{batch}:%"),
            )
            .order_by(CustomerWebhookDelivery.target_index)
            .all()
        )
    return render_template(
        "customer_webhook_test.html",
        hook=hook,
        payload_text=payload_text,
        source_note=source_note,
        evaluation=evaluation,
        previews=previews,
        targets=targets,
        duplicates=duplicates,
        error=error,
        test_token=test_token,
        test_deliveries=test_deliveries,
        batch=batch,
    ), status_code
