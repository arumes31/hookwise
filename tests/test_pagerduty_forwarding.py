"""PagerDuty key custody and durable dispatch through the shared outbox."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import CustomerWebhook, CustomerWebhookDelivery, WebhookConfig, WebhookLog
from hookwise.services.customer_webhooks import (
    dispatch_customer_deliveries,
    preview_delivery_payload,
    set_destination,
    set_payload_format,
    stage_customer_deliveries,
)
from hookwise.utils import decrypt_string, encrypt_string

ROUTING_KEY = "a1b2c3d4" * 4
OTHER_KEY = "e5f6g7h8" * 4
EU_URL = "https://events.eu.pagerduty.com/v2/enqueue"
GLOBAL_URL = "https://events.pagerduty.com/v2/enqueue"


@pytest.fixture
def subscription():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})
    with app.app_context():
        db.create_all()
        config = WebhookConfig(name="Cipp", customer_id_default="CUSTOMER", is_enabled=True)
        db.session.add(config)
        db.session.flush()
        hook = CustomerWebhook(
            name="Duty-Caller-Loxone",
            config_id=config.id,
            company_id="CUSTOMER",
            target_url=EU_URL,
            score_path="",
            minimum_score=0,
            bearer_token="legacy-generic-token",
            signing_secret=None,
        )
        db.session.add(hook)
        db.session.flush()
        set_payload_format(hook, "pagerduty", ROUTING_KEY)
        db.session.commit()
        yield hook
        db.session.remove()
        db.drop_all()


def receive(hook, incident_id="1349"):
    data = {
        "Tenant": "customer.example",
        "TaskInfo": {
            "Tenant": "AllTenants",
            "Parameters": {"Headers": {"x-ms-client-principal": "sender-identity"}},
        },
        "Results": [
            {"IncidentName": "EICAR prevented", "Severity": "informational", "IncidentID": incident_id},
        ],
    }
    log = WebhookLog(config_id=hook.config_id, request_id="cipp-request-" + incident_id, payload=json.dumps(data))
    db.session.add(log)
    db.session.flush()
    stage_customer_deliveries(log, data)
    db.session.commit()
    return CustomerWebhookDelivery.query.filter_by(log_id=log.id).one()


def test_dispatch_uses_events_v2_body_and_never_generic_authorization_header(subscription):
    row = receive(subscription)
    assert decrypt_string(subscription.pagerduty_routing_key) == ROUTING_KEY
    assert ROUTING_KEY not in subscription.pagerduty_routing_key
    assert ROUTING_KEY not in row.payload
    assert "sender-identity" not in row.payload
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as post:
        assert dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
    url, raw_body, headers = post.call_args.args
    assert url == EU_URL
    body = json.loads(raw_body)
    assert body["routing_key"] == ROUTING_KEY
    assert body["event_action"] == "trigger"
    assert body["dedup_key"] == "hookwise:" + row.id
    assert body["payload"]["summary"] == "EICAR prevented"
    assert body["payload"]["severity"] == "info"
    assert body["payload"]["custom_details"]["company_id"] == "CUSTOMER"
    assert "Authorization" not in headers
    assert ROUTING_KEY not in json.dumps(headers)
    assert "legacy-generic-token" not in raw_body.decode()
    assert row.status == "delivered" and row.payload == "{}"


def test_retries_keep_pagerduty_dedup_key_and_integration_key_out_of_storage(subscription):
    row = receive(subscription)
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=503) as first:
        dispatch_customer_deliveries()
    first_body = json.loads(first.call_args.args[1])
    assert row.status == "pending" and row.attempts == 1
    assert ROUTING_KEY not in row.payload
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as second:
        dispatch_customer_deliveries()
    second_body = json.loads(second.call_args.args[1])
    assert second_body["dedup_key"] == first_body["dedup_key"]
    assert first.call_args.args[2]["Idempotency-Key"] == second.call_args.args[2]["Idempotency-Key"]
    assert row.status == "delivered" and row.attempts == 2


def test_preview_is_wire_format_with_placeholder_instead_of_key(subscription):
    row = receive(subscription)
    body = preview_delivery_payload(subscription, json.loads(row.payload))
    assert body["routing_key"] == "***"
    assert body["event_action"] == "trigger"
    assert ROUTING_KEY not in json.dumps(body)


@pytest.mark.parametrize("change", ["format", "routing_key", "destination"])
def test_protocol_or_recipient_changes_cancel_pending_deliveries(subscription, change):
    row = receive(subscription)
    generation = subscription.destination_generation
    if change == "format":
        set_payload_format(subscription, "hookwise")
    elif change == "routing_key":
        set_payload_format(subscription, "pagerduty", OTHER_KEY)
    else:
        set_destination(subscription, GLOBAL_URL, {})
    db.session.commit()
    assert subscription.destination_generation > generation
    assert row.status == "cancelled" and row.payload == "{}"
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
    post.assert_not_called()
    if change in ("format", "destination"):
        assert subscription.pagerduty_routing_key is None


def test_unchanged_format_and_key_preserve_pending_deliveries(subscription):
    row = receive(subscription)
    generation = subscription.destination_generation
    set_payload_format(subscription, "pagerduty", ROUTING_KEY)
    db.session.commit()
    assert subscription.destination_generation == generation
    assert row.status == "pending"


@pytest.mark.parametrize(
    "destination", ["https://customer.example/hook", "https://events.pagerduty.com/v2/enqueue?x=1"]
)
def test_pagerduty_key_cannot_be_configured_for_other_receivers(subscription, destination):
    set_destination(subscription, destination, {})
    with pytest.raises(ValueError):
        set_payload_format(subscription, "pagerduty", ROUTING_KEY)
    db.session.rollback()


def test_dispatch_revalidates_saved_destination_before_accessing_network(subscription):
    row = receive(subscription)
    # Simulate an invalid old backup/direct DB edit bypassing the management service.
    subscription.target_url = "https://customer.example/not-pagerduty"
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
    post.assert_not_called()
    assert row.status == "failed"
    assert ROUTING_KEY not in (row.error_message or "")


def test_backup_round_trip_keeps_encrypted_protocol_credentials(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    raw = export_backup()
    assert ROUTING_KEY.encode() not in raw
    document = parse_backup(raw)
    record = document["customer_webhooks"][0]
    assert record["payload_format"] == "pagerduty"
    assert ROUTING_KEY not in record["pagerduty_routing_key"]
    original_id = subscription.id
    db.session.delete(subscription)
    db.session.commit()
    restore_backup(document)
    hook = db.session.get(CustomerWebhook, original_id)
    assert hook is not None
    assert hook.payload_format == "pagerduty"
    assert decrypt_string(hook.pagerduty_routing_key) == ROUTING_KEY


def test_old_backup_without_protocol_fields_restores_as_generic(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    document = parse_backup(export_backup())
    record = document["customer_webhooks"][0]
    record.pop("payload_format")
    record.pop("pagerduty_routing_key")
    restore_backup(document)
    assert subscription.payload_format == "hookwise"
    assert subscription.pagerduty_routing_key is None


def test_backup_restoring_new_pagerduty_service_key_cancels_old_pending_payload(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    row = receive(subscription)
    generation = subscription.destination_generation
    document = parse_backup(export_backup())
    document["customer_webhooks"][0]["pagerduty_routing_key"] = encrypt_string(OTHER_KEY)
    restore_backup(document)
    assert decrypt_string(subscription.pagerduty_routing_key) == OTHER_KEY
    assert subscription.destination_generation > generation
    assert row.status == "cancelled" and row.payload == "{}"
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
    post.assert_not_called()


def test_restoring_same_pagerduty_key_preserves_pending_generation(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    row = receive(subscription)
    generation = subscription.destination_generation
    document = parse_backup(export_backup())
    # A fresh ciphertext for the same key must not be mistaken for a rotation.
    document["customer_webhooks"][0]["pagerduty_routing_key"] = encrypt_string(ROUTING_KEY)
    restore_backup(document)
    assert decrypt_string(subscription.pagerduty_routing_key) == ROUTING_KEY
    assert subscription.destination_generation == generation
    assert row.status == "pending" and row.payload != "{}"
