"""Customer boundaries, durable ingress, retries and signed outbound delivery."""

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import CustomerWebhook, CustomerWebhookDelivery, WebhookConfig, WebhookLog
from hookwise.services.customer_webhooks import (
    dispatch_customer_deliveries,
    post_webhook,
    stage_customer_deliveries,
)
from hookwise.services.delivery_queue import stage_delivery
from hookwise.utils import decrypt_string


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
        session.update(user_id="test-user", username="test", role="admin")
    return client


@pytest.fixture
def subscription(app):
    config = WebhookConfig(name="Defender", customer_id_default="123", is_enabled=True)
    db.session.add(config)
    db.session.flush()
    webhook = CustomerWebhook(
        name="Kundenalarm",
        comment="Interne Notiz: Ansprechpartner Bereitschaft",
        config_id=config.id,
        company_id="123",
        target_url="https://alarm.example/hook?key=hidden",
        score_path="$.Results[*].Score",
        minimum_score=70,
        bearer_token="customer-token",
        signing_secret="signing-key",
    )
    db.session.add(webhook)
    db.session.commit()
    return webhook


def receive(subscription, data, request_id="request-1"):
    log = WebhookLog(config_id=subscription.config_id, request_id=request_id, payload=json.dumps(data))
    stage_delivery(log, data)
    db.session.commit()
    return log


def test_threshold_batch_isolation_dedup_and_encryption(subscription):
    data = {
        "Results": [
            {"AlertId": "low", "Score": 69},
            {"AlertId": "boundary", "Score": 70, "password": "hidden"},
            {"AlertId": "high", "Score": "91.5"},
            {"Score": True},
            {"Score": "NaN"},
            {"Score": "inf"},
            {"Score": None},
            {"Score": {}},
            {"no_score": 99},
        ]
    }
    receive(subscription, data)
    receive(subscription, data, "request-2")
    deliveries = CustomerWebhookDelivery.query.all()
    assert len(deliveries) == 2
    payloads = [json.loads(row.payload) for row in deliveries]
    assert {row["alert"]["AlertId"] for row in payloads} == {"boundary", "high"}
    assert all(row["company_id"] == "123" for row in payloads)
    assert all("Results" not in row["alert"] for row in payloads)
    assert "Interne Notiz" not in "".join(row.payload for row in deliveries)
    assert "hidden" not in "".join(row.payload for row in deliveries)
    assert "customer-token" not in subscription.bearer_token
    assert "signing-key" not in subscription.signing_secret
    assert "hidden" not in subscription.target_url
    assert decrypt_string(subscription.bearer_token) == "customer-token"


def test_optional_score_forwards_whole_sanitized_alarm(client, subscription):
    response = client.post("/customer-webhooks/new", data=form_data(subscription, score_path="", minimum_score=""))
    assert response.status_code == 302
    hook = CustomerWebhook.query.filter_by(name="Defender Alarm").one()
    data = {"message": "No numeric score", "password": "sensitive"}
    receive(hook, data)
    receive(hook, data, "duplicate")
    row = CustomerWebhookDelivery.query.filter_by(webhook_id=hook.id).one()
    payload = json.loads(row.payload)
    assert payload["alert"]["message"] == data["message"]
    assert "sensitive" not in row.payload
    assert payload["score"] is None and payload["minimum_score"] is None


def test_multiple_targets_dispatch_in_parallel_and_retry_independently(client, subscription):
    urls = ["https://first.example/hook", "https://second.example/hook"]
    response = client.post(
        "/customer-webhooks/new",
        data=form_data(subscription, target_url=urls, bearer_token="shared-token", signing_secret="shared-signing"),
    )
    assert response.status_code == 302
    hook = CustomerWebhook.query.filter_by(name="Defender Alarm").one()
    receive(hook, {"score": 90})
    receive(hook, {"score": 90}, "duplicate")
    rows = (
        CustomerWebhookDelivery.query.filter_by(webhook_id=hook.id).order_by(CustomerWebhookDelivery.target_index).all()
    )
    assert len(rows) == 2
    assert "second.example" not in hook.additional_urls
    barrier = threading.Barrier(2)

    def send(url, body, headers):
        assert headers["Authorization"] == "Bearer shared-token"
        expected = hmac.new(
            b"shared-signing", headers["X-HookWise-Timestamp"].encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        assert headers["X-HookWise-Signature"] == "sha256=" + expected
        barrier.wait(timeout=3)
        return 204 if url == urls[0] else 503

    with patch("hookwise.services.customer_webhooks.post_webhook", side_effect=send) as post:
        assert dispatch_customer_deliveries() == {"delivered": 1, "failed": 1}
    assert {call.args[0] for call in post.call_args_list} == set(urls)
    db.session.expire_all()
    assert [row.status for row in rows] == ["delivered", "pending"]
    assert rows[0].payload == "{}" and rows[1].payload != "{}"
    retry_id = rows[1].id
    rows[1].next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=204) as post:
        dispatch_customer_deliveries()
    post.assert_called_once()
    assert post.call_args.args[0] == urls[1]
    assert post.call_args.args[2]["Idempotency-Key"] == retry_id


def test_multiple_targets_edit_preserves_hidden_urls_and_cancels_on_removal(client, subscription):
    from hookwise.services.customer_webhooks import destination_urls, set_destination

    urls = ["https://first.example/hook?key=secret", "https://second.example/hook?key=secret"]
    set_destination(subscription, urls, {"bearer_token": "shared"})
    db.session.commit()
    receive(subscription, {"Results": [{"Score": 90}]})
    generation = subscription.destination_generation
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(subscription, target_url=["", ""], target_slot=["0", "1"]),
    )
    assert response.status_code == 302
    assert destination_urls(subscription) == urls
    assert subscription.destination_generation == generation
    page = client.get(f"/customer-webhooks/{subscription.id}/edit").get_data(as_text=True)
    assert "key=secret" not in page and "shared" not in page
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(subscription, target_url="", target_slot="1"),
    )
    assert response.status_code == 302
    assert destination_urls(subscription) == [urls[1]]
    assert subscription.bearer_token is None
    assert all(row.status == "cancelled" and row.payload == "{}" for row in subscription.deliveries)


def test_optional_score_and_multiple_targets_backup_round_trip(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup
    from hookwise.services.customer_webhooks import destination_urls, set_destination

    urls = ["https://first.example/hook", "https://second.example/hook"]
    set_destination(subscription, urls, {})
    subscription.score_path, subscription.minimum_score = "", 0
    db.session.commit()
    document = parse_backup(export_backup())
    restore_backup(document)
    assert destination_urls(subscription) == urls
    assert subscription.score_path == ""


@pytest.mark.parametrize("urls", [["https://one.example", "https://127.0.0.1"], ["https://one.example"] * 2])
def test_invalid_multiple_targets_are_rejected(client, subscription, urls):
    assert client.post("/customer-webhooks/new", data=form_data(subscription, target_url=urls)).status_code == 400


@pytest.mark.parametrize("change", ["customer", "disabled", "draft", "archived", "maintenance", "drop", "recovery"])
@pytest.mark.parametrize("filter_enabled", [True, False])
def test_unmatched_or_suppressed_alerts_never_stage(subscription, change, filter_enabled):
    if not filter_enabled:
        subscription.score_path, subscription.minimum_score = "", 0
    config = subscription.config
    data: dict[str, Any] = {"Results": [{"Score": 90}]}
    if change == "customer":
        config.customer_id_default = "other-customer"
    elif change == "disabled":
        subscription.is_enabled = False
    elif change == "draft":
        config.is_draft = True
    elif change == "archived":
        config.archived_at = datetime.now(timezone.utc)
    elif change == "maintenance":
        config.maintenance_windows = json.dumps(
            [
                {
                    "type": "once",
                    "start": "2020-01-01T00:00:00Z",
                    "end": "2099-01-01T00:00:00Z",
                }
            ]
        )
    elif change == "drop":
        config.routing_rules = json.dumps([{"path": "$.Results[0].Score", "regex": "90", "overrides": {"drop": True}}])
    elif change == "recovery":
        data["heartbeat"] = {"status": 1}
    db.session.commit()
    receive(subscription, data)
    assert CustomerWebhookDelivery.query.count() == 0


def test_shared_endpoint_requires_explicit_tenant_match(subscription):
    subscription.config.global_routing_enabled = True
    db.session.commit()
    with patch(
        "hookwise.tasks.get_all_global_mappings",
        return_value=[
            {"tenant_value": "customer.example", "company_id": "123"},
            {"tenant_value": "other.example", "company_id": "456"},
        ],
    ):
        receive(subscription, {"Tenant": "unknown.example", "Results": [{"Score": 90}]}, "unknown")
        receive(subscription, {"Tenant": "other.example", "Results": [{"Score": 90}]}, "other")
        assert CustomerWebhookDelivery.query.count() == 0
        receive(subscription, {"TaskInfo": {"Tenant": "customer.example"}, "Results": [{"Score": 90}]}, "correct")
    assert CustomerWebhookDelivery.query.count() == 1


def test_queue_rollback_is_atomic(subscription):
    log = WebhookLog(config_id=subscription.config_id, request_id="rollback", payload="{}")
    stage_delivery(log, {"Results": [{"Score": 99}]})
    db.session.rollback()
    assert WebhookLog.query.count() == CustomerWebhookDelivery.query.count() == 0


def test_ingress_retains_alarm_even_when_ticket_broker_fails(client, subscription):
    with patch("hookwise.webhook.process_webhook_task.delay", side_effect=ConnectionError):
        response = client.post(
            f"/w/{subscription.config_id}",
            json={"Results": [{"Score": 99}]},
            headers={"Authorization": "Bearer " + decrypt_string(subscription.config.bearer_token)},
        )
    assert response.status_code == 503
    assert CustomerWebhookDelivery.query.one().status == "pending"


def test_signed_delivery_and_stable_id(subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=204) as post:
        assert dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
        assert dispatch_customer_deliveries() == {"delivered": 0, "failed": 0}
    url, body, headers = post.call_args.args
    row = CustomerWebhookDelivery.query.one()
    assert url == "https://alarm.example/hook?key=hidden"
    assert headers["Authorization"] == "Bearer customer-token"
    assert headers["Idempotency-Key"] == row.id
    expected = hmac.new(
        b"signing-key", headers["X-HookWise-Timestamp"].encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-HookWise-Signature"] == "sha256=" + expected
    assert row.status == "delivered" and row.attempts == 1


@pytest.mark.parametrize(
    "code,retry", [(400, False), (401, False), (302, False), (408, True), (429, True), (503, True)]
)
def test_http_failures_and_retry_policy(subscription, code, retry):
    receive(subscription, {"Results": [{"Score": 90}]})
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=code) as post:
        dispatch_customer_deliveries()
        row = CustomerWebhookDelivery.query.one()
        assert row.status == ("pending" if retry else "failed")
        assert row.response_status == code
        if retry:
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            row.attempts = 4
            db.session.commit()
            dispatch_customer_deliveries()
            assert row.status == "failed" and row.attempts == 5
            assert (
                post.call_args_list[0].args[2]["Idempotency-Key"] == post.call_args_list[1].args[2]["Idempotency-Key"]
            )


def test_transport_errors_do_not_leak_secrets_and_stale_claims_recover(subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    row = CustomerWebhookDelivery.query.one()
    row.status = "sending"
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook", side_effect=OSError("https://secret:token@host")):
        dispatch_customer_deliveries()
    assert row.status == "pending"
    assert "secret" not in row.error_message


def test_paused_subscription_cancels_pending_alarm(subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    subscription.is_enabled = False
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
        post.assert_not_called()
    assert CustomerWebhookDelivery.query.one().status == "cancelled"


def test_second_dispatcher_cannot_claim_live_delivery(subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    row = CustomerWebhookDelivery.query.one()
    row.status = "sending"
    row.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
        post.assert_not_called()


def test_https_transport_pins_public_dns_and_disables_redirects():
    pool = MagicMock()
    pool.urlopen.return_value.status = 202
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]),
        patch(
            "urllib3.HTTPSConnectionPool",
            return_value=pool,
        ) as factory,
    ):
        assert post_webhook("https://alarm.example/notify?token=x", b"{}", {}) == 202
    assert factory.call_args.args == ("8.8.8.8",)
    assert factory.call_args.kwargs["server_hostname"] == "alarm.example"
    assert factory.call_args.kwargs["assert_hostname"] == "alarm.example"
    assert pool.urlopen.call_args.kwargs["redirect"] is False
    assert pool.urlopen.call_args.kwargs["retries"] is False
    assert pool.urlopen.call_args.kwargs["headers"]["Host"] == "alarm.example"
    pool.close.assert_called_once()


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "fec0::1"]
)
def test_private_dns_never_receives_alarm(address):
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]),
        patch(
            "urllib3.HTTPSConnectionPool",
        ) as pool,
        pytest.raises(ValueError),
    ):
        post_webhook("https://alarm.example/hook", b"{}", {})
    pool.assert_not_called()


def form_data(subscription, **overrides):
    return {
        "name": "Defender Alarm",
        "comment": "Kontakt: Bereitschaft <intern>\nAlarmierungskette A",
        "company_id": "123",
        "config_id": subscription.config_id,
        "target_url": "https://customer.example/notify",
        "score_path": "$.score",
        "minimum_score": "70",
        "is_enabled": "on",
        **overrides,
    }


def test_menu_create_edit_pause_and_history(client, subscription):
    assert b"Forwarding" in client.get("/").data
    assert client.get("/customer-webhooks").status_code == 200
    assert client.get("/customer-webhooks/new").status_code == 200
    response = client.post("/customer-webhooks/new", data=form_data(subscription))
    assert response.status_code == 302
    hook = CustomerWebhook.query.filter_by(name="Defender Alarm").one()
    assert hook.comment == "Kontakt: Bereitschaft <intern>\nAlarmierungskette A"
    overview = client.get("/customer-webhooks").get_data(as_text=True)
    assert "Bereitschaft &lt;intern&gt;" in overview
    assert "Bereitschaft <intern>" not in overview
    edit_page = client.get(f"/customer-webhooks/{hook.id}/edit").get_data(as_text=True)
    assert 'name="comment"' in edit_page and "Alarmierungskette A" in edit_page
    page = client.get(f"/customer-webhooks/{subscription.id}/edit")
    assert page.status_code == 200
    assert b"customer-token" not in page.data and b"signing-key" not in page.data and b"key=hidden" not in page.data
    response = client.post(
        f"/customer-webhooks/{hook.id}/edit", data=form_data(subscription, target_url="", name="Changed", comment="")
    )
    assert response.status_code == 302
    assert hook.name == "Changed" and decrypt_string(hook.target_url) == "https://customer.example/notify"
    assert hook.comment is None
    assert client.post(f"/customer-webhooks/{hook.id}/toggle").status_code == 302
    assert not hook.is_enabled
    assert client.get(f"/customer-webhooks/{hook.id}/deliveries").status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimum_score": "NaN"},
        {"minimum_score": "-1"},
        {"score_path": "$..score"},
        {"target_url": "http://example.com"},
        {"target_url": "https://127.0.0.1"},
        {"target_url": "https://user:password@example.com"},
        {"config_id": "missing"},
        {"bearer_token": "secret\r\nInjected: header"},
        {"comment": "x" * 2001},
    ],
)
def test_invalid_configuration_cannot_be_saved(client, subscription, overrides):
    response = client.post("/customer-webhooks/new", data=form_data(subscription, **overrides))
    assert response.status_code == 400
    assert CustomerWebhook.query.count() == 1


def test_viewer_cannot_change_or_read_secrets(client, subscription):
    with client.session_transaction() as session:
        session["role"] = "viewer"
    assert client.get("/customer-webhooks").status_code == 200
    assert client.get("/customer-webhooks/new").status_code == 403
    assert client.get(f"/customer-webhooks/{subscription.id}/edit").status_code == 403
    assert client.post(f"/customer-webhooks/{subscription.id}/toggle").status_code == 403


def test_csrf_required(client, subscription, app):
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post(f"/customer-webhooks/{subscription.id}/toggle").status_code == 400


def test_customer_boundary_cannot_be_edited(client, subscription):
    response = client.post(f"/customer-webhooks/{subscription.id}/edit", data=form_data(subscription, company_id="456"))
    assert response.status_code == 400
    assert subscription.company_id == "123"


def test_retarget_clears_credentials_and_cancels_old_deliveries(client, subscription):
    receive(subscription, {"Results": [{"Score": 90, "user": "private@example.com"}]})
    response = client.post(f"/customer-webhooks/{subscription.id}/edit", data=form_data(subscription))
    assert response.status_code == 302
    assert subscription.bearer_token is None and subscription.signing_secret is None
    row = CustomerWebhookDelivery.query.one()
    assert row.status == "cancelled" and row.payload == "{}"
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
        post.assert_not_called()


def test_same_destination_preserves_credentials(client, subscription):
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(subscription, target_url=decrypt_string(subscription.target_url)),
    )
    assert response.status_code == 302
    assert decrypt_string(subscription.bearer_token) == "customer-token"
    assert decrypt_string(subscription.signing_secret) == "signing-key"


def test_retarget_accepts_explicitly_reentered_credentials(client, subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(subscription, bearer_token="customer-token", signing_secret="signing-key"),
    )
    assert response.status_code == 302
    assert decrypt_string(subscription.bearer_token) == "customer-token"
    assert decrypt_string(subscription.signing_secret) == "signing-key"
    assert CustomerWebhookDelivery.query.one().status == "cancelled"


def test_invalid_stored_score_path_does_not_block_ingress(client, subscription, caplog):
    from hookwise.models import DeliveryOutbox

    invalid = CustomerWebhook(
        name="Invalid stored subscription",
        config_id=subscription.config_id,
        company_id=subscription.company_id,
        target_url="https://invalid.example/hook",
        score_path="$..Score",
        minimum_score=70,
    )
    db.session.add(invalid)
    db.session.commit()
    with patch("hookwise.webhook.process_webhook_task.delay") as broker:
        response = client.post(
            f"/w/{subscription.config_id}",
            json={"Results": [{"Score": 90}]},
            headers={"Authorization": "Bearer " + decrypt_string(subscription.config.bearer_token)},
        )
    assert response.status_code == 202
    broker.assert_called_once()
    assert DeliveryOutbox.query.count() == 1
    assert CustomerWebhookDelivery.query.one().webhook_id == subscription.id
    assert invalid.id in caplog.text and "invalid score_path" in caplog.text


@pytest.mark.parametrize("change", ["primary", "additional"])
def test_restore_retarget_cannot_reuse_old_credentials(subscription, change):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup
    from hookwise.utils import encrypt_string

    receive(subscription, {"Results": [{"Score": 90}]})
    document = parse_backup(export_backup())
    if change == "primary":
        document["customer_webhooks"][0]["target_url"] = encrypt_string("https://new.example/hook")
    else:
        document["customer_webhooks"][0]["additional_urls"] = encrypt_string(json.dumps(["https://new.example/hook"]))
    restore_backup(document)
    assert subscription.bearer_token is None and subscription.signing_secret is None
    assert CustomerWebhookDelivery.query.one().status == "cancelled"


@pytest.mark.parametrize("status", ["delivered", "failed", "cancelled"])
def test_terminal_delivery_discards_payload(subscription, status):
    receive(subscription, {"Results": [{"Score": 90, "email": "private@example.com"}]})
    if status == "cancelled":
        subscription.is_enabled = False
        db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=204 if status == "delivered" else 401):
        dispatch_customer_deliveries()
    row = CustomerWebhookDelivery.query.one()
    assert row.status == status and row.payload == "{}"
    receive(subscription, {"Results": [{"Score": 90, "email": "private@example.com"}]}, "repeat")
    assert CustomerWebhookDelivery.query.count() == 1


def test_retention_purges_terminal_rows_but_preserves_pending(subscription):
    from hookwise.tasks import cleanup_logs

    receive(subscription, {"Results": [{"Score": 90}, {"Score": 91}]})
    rows = CustomerWebhookDelivery.query.order_by(CustomerWebhookDelivery.created_at).all()
    old = datetime.now(timezone.utc) - timedelta(days=40)
    rows[0].status, rows[0].completed_at = "delivered", old
    for row in rows:
        row.created_at = old
    db.session.commit()
    cleanup_logs.run()
    assert CustomerWebhookDelivery.query.count() == 1
    assert CustomerWebhookDelivery.query.one().status == "pending"


@pytest.mark.parametrize("action", ["single", "bulk", "all"])
def test_history_deletion_removes_forwarding_copies(client, subscription, action):
    log = receive(subscription, {"Results": [{"Score": 90}]})
    url = {"single": f"/history/delete/{log.id}", "bulk": "/history/bulk-delete", "all": "/history/delete-all"}[action]
    response = client.post(url, json={"ids": [log.id]})
    assert response.status_code == 200
    assert CustomerWebhookDelivery.query.count() == 0


def test_retarget_during_request_cannot_resurrect_cancelled_delivery(subscription):
    from hookwise.services.customer_webhooks import set_destination

    receive(subscription, {"Results": [{"Score": 90}]})

    def retarget(url, body, headers):
        assert url == "https://alarm.example/hook?key=hidden"
        assert headers["Authorization"] == "Bearer customer-token"
        set_destination(subscription, "https://new.example/hook", {"bearer_token": "new-token"})
        db.session.commit()
        return 503

    with patch("hookwise.services.customer_webhooks.post_webhook", side_effect=retarget):
        dispatch_customer_deliveries()
    row = CustomerWebhookDelivery.query.one()
    assert row.status == "cancelled" and row.payload == "{}"
    assert decrypt_string(subscription.bearer_token) == "new-token"


def test_old_generation_cannot_follow_retarget(subscription):
    from hookwise.services.customer_webhooks import set_destination

    receive(subscription, {"Results": [{"Score": 90}]})
    set_destination(subscription, "https://new.example/hook", {})
    row = CustomerWebhookDelivery.query.one()
    # Simulate ingestion that started before the edit but committed afterwards.
    row.status, row.payload = "pending", '{"old":"alert"}'
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
        post.assert_not_called()
    assert row.status == "cancelled"


def test_forwarding_only_ingress_does_not_require_ticket_broker(client, subscription):
    from hookwise.models import DeliveryOutbox

    subscription.config.connectwise_delivery_enabled = False
    db.session.commit()
    with patch("hookwise.webhook.process_webhook_task.delay", side_effect=ConnectionError) as broker:
        response = client.post(
            f"/w/{subscription.config_id}",
            json={"Results": [{"Score": 90}]},
            headers={"Authorization": "Bearer " + decrypt_string(subscription.config.bearer_token)},
        )
    assert response.status_code == 202
    broker.assert_not_called()
    assert DeliveryOutbox.query.count() == 0
    assert CustomerWebhookDelivery.query.count() == 1
    assert subscription.config.last_seen_at is not None


def test_forwarding_switch_blocks_new_and_pending_deliveries(subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    subscription.config.forwarding_enabled = False
    db.session.commit()
    receive(subscription, {"Results": [{"Score": 91}]}, "disabled")
    assert CustomerWebhookDelivery.query.count() == 1
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
        post.assert_not_called()
    assert CustomerWebhookDelivery.query.one().status == "cancelled"


def test_connectwise_switch_stops_queued_work_and_stale_tickets(subscription):
    from hookwise.tasks import check_webhook_timeouts, handle_webhook_logic

    log = receive(subscription, {"Results": [{"Score": 90}]})
    subscription.config.connectwise_delivery_enabled = False
    subscription.config.timeout_alerts_enabled = True
    subscription.config.timeout_ticket_id = 123
    subscription.config.last_seen_at = datetime.now(timezone.utc) - timedelta(days=3)
    db.session.commit()
    with patch("hookwise.tasks.cw_client") as cw:
        check_webhook_timeouts.run()
        handle_webhook_logic(subscription.config_id, {"heartbeat": {"status": 0}}, "request-1", log_id=log.id)
        assert cw.mock_calls == []
    db.session.refresh(log)
    assert log.status == "skipped"
    assert subscription.config.timeout_ticket_id == 123


def test_endpoint_additional_delivery_controls_and_connector_list(client, subscription):
    config = subscription.config
    page = client.get(f"/endpoint/edit/{config.id}").get_data(as_text=True)
    assert "Additional Delivery" in page and 'id="tab-additional-delivery"' in page
    assert 'name="connectwise_delivery_enabled"' in page and 'name="forwarding_enabled"' in page
    assert "1 active" in page and "Kundenalarm" in page
    response = client.post(
        f"/endpoint/edit/{config.id}",
        data={
            "name": config.name,
            "bearer_auth_enabled": "true",
            "connectwise_delivery_enabled": "false",
            "forwarding_enabled": "false",
        },
    )
    assert response.status_code == 302
    assert config.connectwise_delivery_enabled is False and config.forwarding_enabled is False
    assert config.to_dict()["forwarding_enabled"] is False
    assert client.post(f"/endpoint/clone/{config.id}").status_code == 302
    cloned = WebhookConfig.query.filter_by(name=config.name + " (Copy)").one()
    assert cloned.connectwise_delivery_enabled is False and cloned.forwarding_enabled is False


def test_delivery_controls_survive_backup_restore(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    subscription.config.connectwise_delivery_enabled = False
    subscription.config.forwarding_enabled = False
    db.session.commit()
    document = parse_backup(export_backup())
    subscription.config.connectwise_delivery_enabled = True
    subscription.config.forwarding_enabled = True
    db.session.commit()
    restore_backup(document)
    assert subscription.config.connectwise_delivery_enabled is False
    assert subscription.config.forwarding_enabled is False


def test_clear_forwarding_history_permissions_and_pending_preservation(client, subscription):
    receive(subscription, {"Results": [{"Score": 90}, {"Score": 91}]})
    rows = CustomerWebhookDelivery.query.all()
    rows[0].status = "delivered"
    db.session.commit()
    path = f"/customer-webhooks/{subscription.id}/deliveries/clear"
    with client.session_transaction() as session:
        session["role"] = "viewer"
    assert client.post(path).status_code == 403
    with client.session_transaction() as session:
        session["role"] = "admin"
    assert client.post(path).status_code == 302
    assert CustomerWebhookDelivery.query.one().status == "pending"


def test_stage_is_safe_when_repeated_for_same_log(subscription):
    log = receive(subscription, {"Results": [{"Score": 90}]})
    stage_customer_deliveries(log, {"Results": [{"Score": 90}]})
    db.session.commit()
    assert CustomerWebhookDelivery.query.count() == 1


def test_crashed_final_attempt_is_not_sent_again(subscription):
    receive(subscription, {"Results": [{"Score": 90}]})
    row = CustomerWebhookDelivery.query.one()
    row.status = "sending"
    row.attempts = 5
    row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        dispatch_customer_deliveries()
        post.assert_not_called()
    assert row.status == "failed"


def test_configuration_backup_round_trip(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    raw = export_backup()
    assert b"customer-token" not in raw
    document = parse_backup(raw)
    original_id = subscription.id
    db.session.delete(subscription)
    db.session.commit()
    restore_backup(document)
    hook = db.session.get(CustomerWebhook, original_id)
    assert hook is not None
    assert hook.company_id == "123"
    assert hook.minimum_score == 70
    assert hook.comment == "Interne Notiz: Ansprechpartner Bereitschaft"
    assert decrypt_string(hook.target_url) == "https://alarm.example/hook?key=hidden"
    assert decrypt_string(hook.bearer_token) == "customer-token"

    # Backups from before the comment field was introduced remain readable.
    document["customer_webhooks"][0].pop("comment")
    document["customer_webhooks"][0].pop("additional_urls")
    restore_backup(document)
    assert hook.comment is None


def test_migration_matches_models_and_rolls_back(app):
    import importlib

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module("migrations.versions.c9a7b2e4d6f8_add_customer_webhooks")
    destination = importlib.import_module("migrations.versions.d7f3a9b2c5e1_bind_forwarding_destinations")
    targets = importlib.import_module("migrations.versions.f2a8c4d6e9b1_add_forwarding_targets")
    pagerduty = importlib.import_module("migrations.versions.a8c2d4e6f901_add_pagerduty_forwarding")
    severity = importlib.import_module("migrations.versions.b6d8e0f2a4c6_add_forwarding_severity_filter")
    status = importlib.import_module("migrations.versions.c7e9f1a3b5d7_add_forwarding_status_filter")
    with db.engine.begin() as connection:
        db.metadata.tables["customer_webhook_delivery"].drop(connection)
        db.metadata.tables["customer_webhook"].drop(connection)
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            destination.upgrade()
            targets.upgrade()
            pagerduty.upgrade()
            severity.upgrade()
            status.upgrade()
            assert compare_metadata(context, db.metadata) == []
            status.downgrade()
            severity.downgrade()
            pagerduty.downgrade()
            targets.downgrade()
            destination.downgrade()
            migration.downgrade()
            migration.upgrade()
            destination.upgrade()
            targets.upgrade()
            pagerduty.upgrade()
            severity.upgrade()
            status.upgrade()
            assert compare_metadata(context, db.metadata) == []


def test_target_migration_rollback_does_not_reroute_extra_copies(subscription):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from hookwise.services.customer_webhooks import set_destination

    set_destination(subscription, ["https://first.example", "https://second.example"], {})
    db.session.commit()
    receive(subscription, {"Results": [{"Score": 90}]})
    db.session.remove()
    migration = importlib.import_module("migrations.versions.f2a8c4d6e9b1_add_forwarding_targets")
    with db.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert connection.execute(text("SELECT COUNT(*) FROM customer_webhook_delivery")).scalar_one() == 1
        migration.upgrade()
        assert connection.execute(text("SELECT target_index FROM customer_webhook_delivery")).scalar_one() == 0
