"""Explicit target editing without disclosing URL credentials on page loads."""

import pytest

from hookwise.extensions import db
from hookwise.models import AuditLog, CustomerWebhook, CustomerWebhookDelivery
from hookwise.services.customer_webhooks import destination_urls, set_destination
from hookwise.utils import decrypt_string

from .test_customer_webhooks import app as app
from .test_customer_webhooks import client as client
from .test_customer_webhooks import form_data, receive
from .test_customer_webhooks import subscription as subscription


def test_saved_target_is_loaded_only_by_explicit_authenticated_action(client, subscription):
    urls = ["https://alarm.example/hook?key=first-secret", "https://other.example/hook?key=second-secret"]
    set_destination(subscription, urls, {})
    db.session.commit()
    generation = subscription.destination_generation
    page = client.get(f"/customer-webhooks/{subscription.id}/edit").get_data(as_text=True)
    assert "Ziel bearbeiten" in page
    assert "alarm.example" in page and "other.example" in page
    assert "first-secret" not in page and "second-secret" not in page
    endpoint = f"/customer-webhooks/{subscription.id}/targets/0/reveal"
    assert client.get(endpoint).status_code == 405
    response = client.post(endpoint)
    assert response.status_code == 200
    assert response.json == {"url": urls[0]}
    assert response.headers["Cache-Control"] == "no-store"
    assert "second-secret" not in response.get_data(as_text=True)
    assert "customer-token" not in response.get_data(as_text=True)
    assert subscription.destination_generation == generation
    audit = AuditLog.query.filter_by(action="customer_webhook_target_reveal").one()
    assert "target 1" in audit.details
    assert "secret" not in audit.details and "https://" not in audit.details


@pytest.mark.parametrize("mode", ["on", "log", "off"])
@pytest.mark.parametrize("permissions", [{"endpoint:write"}, {"secret:reveal"}, set()])
def test_url_reveal_requires_both_write_and_secret_permission(
    client, subscription, app, monkeypatch, mode, permissions
):
    app.config["RBAC_ENFORCE"] = mode
    monkeypatch.setattr("hookwise.customer_webhooks.current_permissions", lambda: frozenset(permissions))
    response = client.post(f"/customer-webhooks/{subscription.id}/targets/0/reveal")
    assert response.status_code == 403
    assert "hidden" not in response.get_data(as_text=True)
    assert AuditLog.query.filter_by(action="customer_webhook_target_reveal").count() == 0


def test_write_only_editor_gets_replacement_control_without_secret_url(client, subscription, monkeypatch):
    monkeypatch.setattr("hookwise.rbac.resolver.has_permission", lambda permission: permission != "secret:reveal")
    page = client.get(f"/customer-webhooks/{subscription.id}/edit").get_data(as_text=True)
    assert "Ziel ersetzen" in page
    assert "data-reveal-url=" not in page
    assert "key=hidden" not in page and "customer-token" not in page


def test_url_reveal_requires_csrf_and_valid_slot(client, subscription, app):
    assert client.post(f"/customer-webhooks/{subscription.id}/targets/99/reveal").status_code == 404
    assert client.post("/customer-webhooks/missing/targets/0/reveal").status_code == 404
    app.config["WTF_CSRF_ENABLED"] = True
    response = client.post(f"/customer-webhooks/{subscription.id}/targets/0/reveal")
    assert response.status_code == 400


def test_editing_one_stored_url_preserves_other_slots_and_receiver_boundary(client, subscription):
    urls = ["https://first.example/hook?key=secret", "https://second.example/hook?key=secret"]
    set_destination(subscription, urls, {"bearer_token": "old-token", "signing_secret": "old-signing"})
    db.session.commit()
    receive(subscription, {"Results": [{"Score": 80}]})
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(subscription, target_slot=["0", "1"], target_url=["", "https://replacement.example/hook"]),
    )
    assert response.status_code == 302
    assert destination_urls(subscription) == [urls[0], "https://replacement.example/hook"]
    assert subscription.bearer_token is None and subscription.signing_secret is None
    assert all(row.status == "cancelled" and row.payload == "{}" for row in CustomerWebhookDelivery.query.all())


def test_cancelled_edit_keeps_stored_target_and_credentials(client, subscription):
    generation = subscription.destination_generation
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit", data=form_data(subscription, target_slot="0", target_url="")
    )
    assert response.status_code == 302
    assert destination_urls(subscription) == ["https://alarm.example/hook?key=hidden"]
    assert subscription.destination_generation == generation
    assert decrypt_string(subscription.bearer_token) == "customer-token"


@pytest.mark.parametrize(
    "url", ["http://public.example/hook", "https://127.0.0.1/hook", "https://u:p@public.example/hook"]
)
def test_replacement_validation_does_not_mutate_or_reflect_sensitive_urls(client, subscription, url):
    previous = destination_urls(subscription)
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit", data=form_data(subscription, target_slot="0", target_url=url)
    )
    assert response.status_code == 400
    assert destination_urls(subscription) == previous
    assert url not in response.get_data(as_text=True)
    assert decrypt_string(subscription.bearer_token) == "customer-token"


def test_pagerduty_configuration_uses_separate_encrypted_integration_key(client, subscription):
    key = "a" * 32  # Synthetic Events API fixture, never a real integration key.
    response = client.post(
        "/customer-webhooks/new",
        data=form_data(
            subscription,
            payload_format="pagerduty",
            pagerduty_routing_key=key,
            target_url="https://events.eu.pagerduty.com/v2/enqueue",
            score_path="",
        ),
    )
    assert response.status_code == 302
    hook = CustomerWebhook.query.filter_by(name="Defender Alarm").one()
    assert hook.payload_format == "pagerduty"
    assert hook.pagerduty_routing_key != key
    assert decrypt_string(hook.pagerduty_routing_key) == key
    page = client.get(f"/customer-webhooks/{hook.id}/edit").get_data(as_text=True)
    assert key not in page
    assert 'value="pagerduty" selected' in page
    assert 'id="pagerduty_routing_key"' in page
    assert "Gespeicherten Integration Key entfernen" in page
    assert "Integration Key im JSON-Body" in page
    response = client.post(
        f"/customer-webhooks/{hook.id}/edit",
        data=form_data(hook, target_slot="0", target_url="", score_path="", pagerduty_routing_key=""),
    )
    assert response.status_code == 302
    assert hook.payload_format == "pagerduty"  # Backward-compatible POST preserves the configured format.
    assert decrypt_string(hook.pagerduty_routing_key) == key


@pytest.mark.parametrize(
    "extra",
    [
        {"pagerduty_routing_key": ""},
        {"pagerduty_routing_key": "not-an-integration-key"},
        {"target_url": "https://arbitrary.example/v2/enqueue"},
        {"target_url": "https://events.eu.pagerduty.com/v2/enqueue?key=do-not-reflect"},
    ],
)
def test_invalid_pagerduty_configuration_is_rejected_without_reflecting_credentials(client, subscription, extra):
    key = "b" * 32
    response = client.post(
        "/customer-webhooks/new",
        data=form_data(
            subscription,
            **{
                "payload_format": "pagerduty",
                "pagerduty_routing_key": key,
                "target_url": "https://events.eu.pagerduty.com/v2/enqueue",
                **extra,
            },
        ),
    )
    assert response.status_code == 400
    assert CustomerWebhook.query.count() == 1
    body = response.get_data(as_text=True)
    assert key not in body and "do-not-reflect" not in body
    assert 'value="pagerduty" selected' in body
