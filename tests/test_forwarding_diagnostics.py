"""Forwarding diagnostics use real customer boundaries without accidental alarm sends."""

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import (
    AuditLog,
    CustomerWebhook,
    CustomerWebhookDelivery,
    DeliveryOutbox,
    WebhookConfig,
    WebhookLog,
)
from hookwise.services import customer_webhooks as forwarding


@pytest.fixture
def app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id="forwarding-test-admin", username="test", role="admin")
    return client


@pytest.fixture
def subscription(app):
    config = WebhookConfig(name="Cipp", customer_id_default="123", is_enabled=True)
    db.session.add(config)
    db.session.flush()
    webhook = CustomerWebhook(
        name="Duty-Caller-Test",
        config_id=config.id,
        company_id="123",
        target_url="https://alarm.example/notify?key=receiver-secret",
        score_path="$.Results[*].Score",
        minimum_score=70,
        bearer_token="receiver-bearer",
        signing_secret="receiver-signing",
    )
    db.session.add(webhook)
    db.session.commit()
    return webhook


@pytest.fixture
def cipp_payload():
    return {
        "tenantId": "11111111-2222-3333-4444-555555555555",
        "Tenant": "customer.example",
        "TaskInfo": {
            "Tenant": "AllTenants",
            "Command": "Get-CIPPAlertDefenderIncidents",
            "Parameters": {
                "Headers": {
                    "x-ms-client-principal": "sender-principal-secret",
                    "x-ms-client-principal-name": "sender-identity-secret",
                    "Authorization": "sender-auth-secret",
                    "Cookie": "sender-cookie-secret",
                }
            },
            "PostExecutionResults": "sender-webhook-url-secret",
        },
        "Results": [
            {
                "IncidentName": "'EICAR_Test_File' malware was prevented",
                "Severity": "informational",
                "IncidentID": "1349",
                "IncidentUrl": "https://security.microsoft.com/incident2/1349/overview",
                "Tenant": "customer.example",
            }
        ],
        "AlertComment": "",
    }


def without_score_filter(subscription):
    subscription.score_path, subscription.minimum_score = "", 0
    db.session.commit()


def test_cipp_severity_is_not_a_numeric_score(subscription, cipp_payload):
    diagnosis = forwarding.evaluate_customer_webhook(subscription, cipp_payload)
    assert diagnosis.eligible is False
    assert diagnosis.code == "score_missing"
    assert diagnosis.message
    assert not diagnosis.matches
    assert CustomerWebhookDelivery.query.count() == 0


def test_cipp_without_score_filter_matches_top_level_tenant_and_redacts_sender_headers(subscription, cipp_payload):
    without_score_filter(subscription)
    subscription.config.global_routing_enabled = True
    db.session.commit()
    before = copy.deepcopy(cipp_payload)
    with patch(
        "hookwise.tasks.get_all_global_mappings",
        return_value=[{"tenant_value": "customer.example", "company_id": "123"}],
    ):
        diagnosis = forwarding.evaluate_customer_webhook(subscription, cipp_payload)
    assert diagnosis.eligible is True and diagnosis.code == "ready"
    assert diagnosis.company_id == "123"
    assert diagnosis.matches is not None
    assert len(diagnosis.matches) == 1
    score, alert = diagnosis.matches[0]
    assert score is None
    assert alert["Tenant"] == "customer.example"
    assert alert["Results"][0]["IncidentID"] == "1349"
    serialized = json.dumps(alert)
    for value in (
        "sender-principal-secret",
        "sender-identity-secret",
        "sender-auth-secret",
        "sender-cookie-secret",
        "sender-webhook-url-secret",
    ):
        assert value not in serialized
    assert cipp_payload == before
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, "score_invalid"),
        (True, "score_invalid"),
        ("informational", "score_invalid"),
        ("NaN", "score_invalid"),
        ("inf", "score_invalid"),
        (-1, "score_invalid"),
        (69, "score_below_threshold"),
        (70, "ready"),
        ("91.5", "ready"),
    ],
)
def test_score_diagnostics_distinguish_missing_invalid_and_below_threshold(subscription, value, code):
    diagnosis = forwarding.evaluate_customer_webhook(subscription, {"Results": [{"Score": value}]})
    assert diagnosis.code == code
    assert diagnosis.eligible is (code == "ready")
    assert diagnosis.message


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("forwarding", "forwarding_disabled"),
        ("endpoint", "endpoint_disabled"),
        ("connector", "connector_disabled"),
        ("draft", "draft"),
        ("archived", "archived"),
        ("company", "company_mismatch"),
        ("routing", "routing_drop"),
        ("recovery", "recovery"),
        ("maintenance", "maintenance"),
        ("path", "invalid_score_path"),
        ("destination", "invalid_destinations"),
    ],
)
def test_suppression_reasons_are_actionable_and_never_stage(subscription, change, code):
    data: dict[str, Any] = {"Results": [{"Score": 90}]}
    config = subscription.config
    if change == "forwarding":
        config.forwarding_enabled = False
    elif change == "endpoint":
        config.is_enabled = False
    elif change == "connector":
        subscription.is_enabled = False
    elif change == "draft":
        config.is_draft = True
    elif change == "archived":
        config.archived_at = datetime.now(timezone.utc)
    elif change == "company":
        config.customer_id_default = "other-customer"
    elif change == "routing":
        config.routing_rules = json.dumps([{"path": "$.Results[0].Score", "regex": "90", "overrides": {"drop": True}}])
    elif change == "recovery":
        data["heartbeat"] = {"status": 1}
    elif change == "maintenance":
        config.maintenance_windows = json.dumps(
            [{"type": "once", "start": "2020-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}]
        )
    elif change == "path":
        subscription.score_path = "$..Score"
    elif change == "destination":
        subscription.target_url = "https://127.0.0.1/notify"
    db.session.commit()
    diagnosis = forwarding.evaluate_customer_webhook(subscription, data)
    assert diagnosis.eligible is False and diagnosis.code == code
    assert diagnosis.message
    assert CustomerWebhookDelivery.query.count() == 0


def test_unmapped_shared_tenant_does_not_use_default_customer(subscription, cipp_payload):
    without_score_filter(subscription)
    subscription.config.global_routing_enabled = True
    db.session.commit()
    with patch("hookwise.tasks.get_all_global_mappings", return_value=[]):
        diagnosis = forwarding.evaluate_customer_webhook(subscription, cipp_payload)
    assert diagnosis.code == "tenant_unmapped" and diagnosis.eligible is False
    assert not diagnosis.matches


def test_actual_cipp_ingress_stages_with_score_filter_off(subscription, cipp_payload):
    without_score_filter(subscription)
    subscription.config.global_routing_enabled = True
    log = WebhookLog(config_id=subscription.config_id, request_id="actual-cipp", payload=json.dumps(cipp_payload))
    db.session.add(log)
    db.session.flush()
    with patch(
        "hookwise.tasks.get_all_global_mappings",
        return_value=[{"tenant_value": "customer.example", "company_id": "123"}],
    ):
        forwarding.stage_customer_deliveries(log, cipp_payload)
        forwarding.stage_customer_deliveries(log, cipp_payload)
    db.session.commit()
    delivery = CustomerWebhookDelivery.query.one()
    payload = json.loads(delivery.payload)
    assert payload["company_id"] == "123"
    assert payload["alert"]["Results"][0]["IncidentID"] == "1349"
    assert payload["score"] is None and payload["minimum_score"] is None
    assert payload.get("test") is not True
    assert "sender-principal-secret" not in delivery.payload
    assert "sender-webhook-url-secret" not in delivery.payload
    assert delivery.log_id == log.id


def test_outbound_source_is_stable_and_does_not_disclose_private_ingress_identifier(subscription, cipp_payload):
    first = forwarding.build_alert_payload(subscription, "request-first", None, cipp_payload)
    second = forwarding.build_alert_payload(subscription, "request-second", None, cipp_payload)
    opaque_id = first["source_endpoint_id"]
    assert opaque_id != subscription.config_id
    assert len(opaque_id) == 64 and all(char in "0123456789abcdef" for char in opaque_id)
    assert second["source_endpoint_id"] == opaque_id
    assert first["source"] == second["source"] == {"id": opaque_id, "name": "Cipp"}
    assert subscription.config_id not in json.dumps(first)


def test_legacy_pending_delivery_hides_private_source_identifier_before_send(subscription, cipp_payload):
    body = forwarding.build_alert_payload(subscription, "legacy-request", None, cipp_payload)
    body.pop("source", None)
    body["source_endpoint_id"] = subscription.config_id
    db.session.add(
        CustomerWebhookDelivery(
            webhook_id=subscription.id,
            event_key="legacy-source-privacy",
            target_index=0,
            destination_generation=subscription.destination_generation,
            payload=json.dumps(body),
        )
    )
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as post:
        assert forwarding.dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
    sent = post.call_args.args[1]
    assert subscription.config_id not in sent.decode()
    expected = forwarding.build_alert_payload(subscription, "another-request", None, cipp_payload)
    assert json.loads(sent)["source_endpoint_id"] == expected["source_endpoint_id"]


class HiddenInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name"):
            self.values[values["name"]] = values.get("value", "")


def get_test_form(client, subscription):
    response = client.get(f"/customer-webhooks/{subscription.id}/test")
    assert response.status_code == 200
    parser = HiddenInputs()
    parser.feed(response.get_data(as_text=True))
    return parser.values


def test_dry_run_reports_actual_cipp_reason_without_writes_or_network(client, subscription, cipp_payload):
    form = get_test_form(client, subscription)
    before = AuditLog.query.count()
    with (
        patch("hookwise.services.customer_webhooks.post_webhook") as post,
        patch("hookwise.webhook.process_webhook_task.delay") as broker,
    ):
        response = client.post(
            f"/customer-webhooks/{subscription.id}/test",
            data={**form, "payload": json.dumps(cipp_payload), "action": "preview"},
        )
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Score" in page or "score" in page
    assert "receiver-secret" not in page
    assert "receiver-bearer" not in page and "receiver-signing" not in page
    assert "sender-principal-secret" not in page and "sender-auth-secret" not in page
    post.assert_not_called()
    broker.assert_not_called()
    assert CustomerWebhookDelivery.query.count() == WebhookLog.query.count() == DeliveryOutbox.query.count() == 0
    assert AuditLog.query.count() == before


def test_preview_does_not_expose_private_ingress_identifier(client, subscription, cipp_payload):
    without_score_filter(subscription)
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={**get_test_form(client, subscription), "payload": json.dumps(cipp_payload), "action": "preview"},
    )
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert subscription.config_id not in page
    source = forwarding.build_alert_payload(subscription, "preview-check", None, cipp_payload)["source_endpoint_id"]
    assert source in page


@pytest.mark.parametrize("raw", ["{", "[]", '"text"', "null", "42", "true", '{"value": NaN}', '{"value": Infinity}'])
def test_dry_run_rejects_invalid_or_non_object_json(client, subscription, raw):
    form = get_test_form(client, subscription)
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test", data={**form, "payload": raw, "action": "preview"}
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == WebhookLog.query.count() == 0


@pytest.mark.parametrize("missing", ["confirm_send", "test_token"])
def test_real_test_requires_confirmation_and_bound_token(client, subscription, cipp_payload, missing):
    without_score_filter(subscription)
    form = {
        **get_test_form(client, subscription),
        "payload": json.dumps(cipp_payload),
        "action": "send",
        "confirm_send": "on",
    }
    form.pop(missing, None)
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        response = client.post(f"/customer-webhooks/{subscription.id}/test", data=form)
    assert response.status_code == 400
    post.assert_not_called()
    assert CustomerWebhookDelivery.query.count() == 0


def test_confirmed_test_is_marked_target_bound_idempotent_and_independent_of_tickets(
    client, subscription, cipp_payload
):
    without_score_filter(subscription)
    form = {
        **get_test_form(client, subscription),
        "payload": json.dumps({**cipp_payload, "target_url": "https://unconfigured.example/steal"}),
        "target_url": "https://unconfigured.example/steal",
        "action": "send",
        "confirm_send": "on",
    }
    with (
        patch("hookwise.services.customer_webhooks.post_webhook") as post,
        patch("hookwise.webhook.process_webhook_task.delay") as broker,
    ):
        response = client.post(f"/customer-webhooks/{subscription.id}/test", data=form)
        duplicate = client.post(f"/customer-webhooks/{subscription.id}/test", data=form)
    assert response.status_code == duplicate.status_code == 302
    post.assert_not_called()
    broker.assert_not_called()
    assert WebhookLog.query.count() == DeliveryOutbox.query.count() == 0
    delivery = CustomerWebhookDelivery.query.one()
    payload = json.loads(delivery.payload)
    assert payload["event"] == "hookwise.customer_alert" and payload["test"] is True
    assert payload["company_id"] == subscription.company_id
    assert payload["alert"]["Results"][0]["IncidentID"] == "1349"
    assert delivery.event_key.startswith("test:")
    assert delivery.destination_generation == subscription.destination_generation
    assert delivery.target_index == 0 and delivery.status == "pending"
    assert delivery.log_id is None
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as post:
        assert forwarding.dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
    url, body, headers = post.call_args.args
    assert url == "https://alarm.example/notify?key=receiver-secret"
    assert subscription.config_id not in body.decode()
    assert json.loads(body)["test"] is True
    assert headers["Authorization"] == "Bearer receiver-bearer"
    assert headers["X-HookWise-Signature"].startswith("sha256=")
    assert headers["Idempotency-Key"] == delivery.id


def test_test_token_cannot_be_reused_for_another_connector(client, subscription, cipp_payload):
    without_score_filter(subscription)
    form = get_test_form(client, subscription)
    other = CustomerWebhook(
        name="Other receiver",
        config_id=subscription.config_id,
        company_id="123",
        target_url="https://other.example/notify",
        score_path="",
        minimum_score=0,
    )
    db.session.add(other)
    db.session.commit()
    response = client.post(
        f"/customer-webhooks/{other.id}/test",
        data={**form, "payload": json.dumps(cipp_payload), "action": "send", "confirm_send": "on"},
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0


def test_unmatched_real_test_is_not_queued(client, subscription, cipp_payload):
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={
            **get_test_form(client, subscription),
            "payload": json.dumps(cipp_payload),
            "action": "send",
            "confirm_send": "on",
        },
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize("mode", ["on", "off", "log"])
def test_viewer_cannot_preview_or_send_alerts(client, subscription, cipp_payload, app, mode):
    form = get_test_form(client, subscription)
    app.config["RBAC_ENFORCE"] = mode
    with client.session_transaction() as session:
        session["role"] = "viewer"
    path = f"/customer-webhooks/{subscription.id}/test"
    assert client.get(path).status_code == 403
    assert client.post(path, data={**form, "payload": json.dumps(cipp_payload)}).status_code == 403
    assert CustomerWebhookDelivery.query.count() == 0


def test_test_confirmation_expires_when_saved_destinations_change(client, subscription, cipp_payload):
    without_score_filter(subscription)
    form = get_test_form(client, subscription)
    forwarding.set_destination(subscription, "https://replacement.example/notify", {})
    db.session.commit()
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={**form, "payload": json.dumps(cipp_payload), "action": "send", "confirm_send": "on"},
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0


def test_test_send_rechecks_delivery_limit_after_lock(client, subscription, cipp_payload):
    without_score_filter(subscription)
    form = get_test_form(client, subscription)
    original = forwarding.evaluate_customer_webhook(subscription, cipp_payload)
    assert original.matches is not None
    larger_after_lock = replace(original, matches=original.matches * 101)
    with patch("hookwise.forwarding_test.evaluate_customer_webhook", side_effect=[original, larger_after_lock]):
        response = client.post(
            f"/customer-webhooks/{subscription.id}/test",
            data={**form, "payload": json.dumps(cipp_payload), "action": "send", "confirm_send": "on"},
        )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize("mode", ["on", "off", "log"])
def test_loading_history_requires_real_history_permission(client, subscription, mode, app):
    app.config["RBAC_ENFORCE"] = mode
    with patch("hookwise.forwarding_test.current_permissions", return_value=frozenset({"endpoint:test"})):
        response = client.get(f"/customer-webhooks/{subscription.id}/test?latest=1")
    assert response.status_code == 403


def test_only_own_test_status_is_available_without_history_permission(client, subscription, cipp_payload, app):
    without_score_filter(subscription)
    form = get_test_form(client, subscription)
    permissions = frozenset({"endpoint:test"})
    with patch("hookwise.forwarding_test.current_permissions", return_value=permissions):
        response = client.post(
            f"/customer-webhooks/{subscription.id}/test",
            data={**form, "payload": json.dumps(cipp_payload), "action": "send", "confirm_send": "on"},
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        for _ in range(2):
            status = client.get(location)
            assert status.status_code == 200
            assert CustomerWebhookDelivery.query.one().id in status.get_data(as_text=True)

        other_client = app.test_client()
        with other_client.session_transaction() as session:
            session.update(user_id="other-operator", username="other", role="operator")
        assert other_client.get(location).status_code == 403


def test_test_status_is_scoped_to_connector(client, subscription, cipp_payload):
    without_score_filter(subscription)
    form = get_test_form(client, subscription)
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={**form, "payload": json.dumps(cipp_payload), "action": "send", "confirm_send": "on"},
    )
    other = CustomerWebhook(
        name="Other receiver",
        config_id=subscription.config_id,
        company_id="123",
        target_url="https://other.example/notify",
        score_path="",
        minimum_score=0,
    )
    db.session.add(other)
    db.session.commit()
    location = response.headers["Location"].replace(subscription.id, other.id)
    assert CustomerWebhookDelivery.query.one().id not in client.get(location).get_data(as_text=True)


def test_pagerduty_preview_shows_real_wire_format_without_routing_key(client, subscription, cipp_payload):
    without_score_filter(subscription)
    forwarding.set_destination(subscription, "https://events.eu.pagerduty.com/v2/enqueue", {})
    routing_key = "c" * 32  # Synthetic Events API fixture.
    forwarding.set_payload_format(subscription, "pagerduty", routing_key)
    db.session.commit()
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={**get_test_form(client, subscription), "payload": json.dumps(cipp_payload), "action": "preview"},
    )
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "routing_key" in page and "event_action" in page
    assert "[TEST]" in page and "dedup_key" in page
    assert routing_key not in page
    assert "receiver-bearer" not in page and "receiver-signing" not in page
    assert CustomerWebhookDelivery.query.count() == 0


def test_test_send_enforces_csrf_even_with_valid_confirmation(client, subscription, cipp_payload, app):
    without_score_filter(subscription)
    app.config["WTF_CSRF_ENABLED"] = True
    form = get_test_form(client, subscription)
    assert form.get("csrf_token")
    form.pop("csrf_token")
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={**form, "payload": json.dumps(cipp_payload), "action": "send", "confirm_send": "on"},
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0
