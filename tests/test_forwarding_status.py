"""Only explicitly open incidents may pass the optional on-call status gate."""

import copy
import importlib
import json
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from hookwise.extensions import db
from hookwise.models import CustomerWebhook, CustomerWebhookDelivery, WebhookLog
from hookwise.services import customer_webhooks as forwarding

from .test_forwarding_severity import app as app
from .test_forwarding_severity import client as client
from .test_forwarding_severity import configure, form_data, get_test_form, incident, payload, stage
from .test_forwarding_severity import subscription as subscription

STATUS_PATH = "$.Results[*].Status"


def require_open(hook, path=STATUS_PATH):
    forwarding.set_status_filter(hook, path)
    db.session.commit()


@pytest.mark.parametrize("status", ["active", " Active ", "InProgress", "inprogress", "awaitingAction"])
@pytest.mark.parametrize("severity", ["high", "informational"])
def test_high_and_explicitly_open_must_both_pass(subscription, status, severity):
    configure(subscription)
    require_open(subscription)
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident(severity, Status=status)))
    assert result.eligible is (severity == "high")
    assert result.code == ("ready" if severity == "high" else "severity_below_threshold")
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize("status", ["resolved", " Resolved ", "redirected", "Redirected"])
def test_high_resolved_or_merged_incidents_do_not_stage(subscription, status):
    configure(subscription)
    require_open(subscription)
    data = payload(incident("high", Status=status))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert not result.eligible and result.code == "status_closed"
    stage(subscription, data)
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize("status", [None, True, 1, 0, "", "unknown", "unknownFutureValue", "new", [], {}])
def test_unknown_status_is_never_assumed_open(subscription, status):
    configure(subscription)
    require_open(subscription)
    data = payload(incident("high", Status=status))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert not result.eligible and result.code == "status_invalid"
    assert not result.matches
    stage(subscription, data)
    assert CustomerWebhookDelivery.query.count() == 0


def test_original_cipp_projection_missing_status_is_blocked_even_for_high(subscription):
    configure(subscription)
    require_open(subscription)
    data = payload(incident("high"))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert not result.eligible and result.code == "status_missing"
    assert "CIPP" in result.message
    assert "Status" in result.message
    stage(subscription, data)
    assert CustomerWebhookDelivery.query.count() == 0


def test_status_filter_off_preserves_existing_high_filter_for_legacy_cipp(subscription):
    configure(subscription)
    assert subscription.status_path == ""
    data = payload(incident("high"), incident("high", "resolved", Status="Resolved"))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible and result.matches is not None and len(result.matches) == 2


def test_mixed_batch_sends_only_high_open_incidents_to_pagerduty(subscription):
    forwarding.set_destination(subscription, "https://events.eu.pagerduty.com/v2/enqueue", {})
    forwarding.set_payload_format(subscription, "pagerduty", "a" * 32)
    configure(subscription)
    require_open(subscription)
    data = payload(
        incident("high", "high-resolved", Status="Resolved"),
        incident("informational", "quiet-eicar", Status="Active"),
        incident("high", "high-missing"),
        incident("high", "high-unknown", Status="unknown"),
        incident("high", "open-high", Status="InProgress", password="private-test-secret"),
    )
    original = copy.deepcopy(data)
    stage(subscription, data)
    row = CustomerWebhookDelivery.query.one()
    envelope = json.loads(row.payload)
    assert envelope["incident_status"] == "inprogress"
    assert envelope["severity"] == "high"
    assert envelope["alert"]["IncidentID"] == "open-high"
    assert "private-test-secret" not in row.payload
    for blocked in ("high-resolved", "quiet-eicar", "high-missing", "high-unknown"):
        assert blocked not in row.payload
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as post:
        assert forwarding.dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
    wire = json.loads(post.call_args.args[1])
    assert wire["event_action"] == "trigger" and wire["payload"]["severity"] == "critical"
    assert wire["payload"]["custom_details"]["incident_status"] == "inprogress"
    assert wire["payload"]["custom_details"]["alert"]["IncidentID"] == "open-high"
    assert data == original


@pytest.mark.parametrize("score_enabled", [False, True])
def test_open_low_cannot_qualify_a_different_resolved_high(subscription, score_enabled):
    configure(subscription)
    require_open(subscription)
    if score_enabled:
        subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
        db.session.commit()
    data = payload(
        incident("high", "closed-high", Status="Resolved", Score=99),
        incident("low", "open-low", Status="Active", Score=99),
    )
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert not result.eligible and result.code == "filters_no_joint_match"
    stage(subscription, data)
    assert CustomerWebhookDelivery.query.count() == 0


def test_all_three_filters_must_match_the_same_incident(subscription):
    configure(subscription)
    require_open(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    data = payload(
        incident("high", "closed-high-score", Status="Resolved", Score=99),
        incident("high", "open-low-score", Status="Active", Score=20),
        incident("low", "open-high-score", Status="Active", Score=99),
        incident("high", "all-pass", Status="Active", Score=70),
    )
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible and result.matches is not None
    assert [(score, alert["IncidentID"]) for score, alert in result.matches] == [(70, "all-pass")]


def test_status_and_severity_on_different_nested_objects_do_not_jointly_match(subscription):
    configure(subscription)
    require_open(subscription, "$.Results[*].Details.Status")
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident("high", Details={"Status": "Active"})))
    assert not result.eligible and result.code == "filters_no_joint_match"


def test_status_only_filter_isolates_open_objects_without_broadening_batch(subscription):
    require_open(subscription)
    data = payload(incident("low", "open", Status="Active"), incident("high", "closed", Status="Resolved"))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible and result.matches is not None
    assert [alert["IncidentID"] for _, alert in result.matches] == ["open"]


def test_custom_status_field_is_canonical_in_outbound_envelope(subscription):
    configure(subscription)
    require_open(subscription, "$.Results[*].Lifecycle")
    stage(subscription, payload(incident("high", Lifecycle=" Active ", Status="Resolved")))
    envelope = json.loads(CustomerWebhookDelivery.query.one().payload)
    assert envelope["incident_status"] == "active"
    assert envelope["alert"]["Lifecycle"] == " Active "


@pytest.mark.parametrize("path", ["$..Status", "$.Results[*]", "$." + "a" * 201, None, 42])
def test_invalid_status_path_does_not_mutate_saved_policy(subscription, path):
    require_open(subscription)
    generation = subscription.destination_generation
    with pytest.raises(ValueError):
        forwarding.set_status_filter(subscription, path)
    assert subscription.status_path == STATUS_PATH
    assert subscription.destination_generation == generation


def test_corrupt_saved_path_fails_closed_with_diagnosis(subscription):
    subscription.status_path = "$..Status"
    db.session.commit()
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident(Status="Active")))
    assert not result.eligible and result.code == "invalid_status_filter"


@pytest.mark.parametrize("change", ["enable", "path", "disable"])
@pytest.mark.parametrize("queue_status", ["pending", "sending"])
def test_status_policy_changes_cancel_queued_payload_and_generation(subscription, change, queue_status):
    configure(subscription)
    if change != "enable":
        require_open(subscription)
    stage(subscription, payload(incident(Status="Active")))
    row = CustomerWebhookDelivery.query.one()
    row.status = queue_status
    db.session.commit()
    generation = subscription.destination_generation
    path = "" if change == "disable" else ("$.Results[0].Status" if change == "path" else STATUS_PATH)
    require_open(subscription, path)
    assert subscription.destination_generation > generation
    assert row.status == "cancelled" and row.payload == "{}"
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        forwarding.dispatch_customer_deliveries()
    post.assert_not_called()


def test_unchanged_status_policy_keeps_pending_delivery(subscription):
    require_open(subscription)
    stage(subscription, payload(incident(Status="Active")))
    row = CustomerWebhookDelivery.query.one()
    generation = subscription.destination_generation
    require_open(subscription)
    assert subscription.destination_generation == generation
    assert row.status == "pending" and row.payload != "{}"


@pytest.mark.parametrize("change", ["enable", "path", "disable"])
def test_old_confirmation_cannot_send_after_status_policy_change(client, subscription, change):
    configure(subscription)
    if change != "enable":
        require_open(subscription)
    form = get_test_form(client, subscription)
    require_open(subscription, "" if change == "disable" else "$.Results[0].Status")
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={
            **form,
            "payload": json.dumps(payload(incident(Status="Active"))),
            "action": "send",
            "confirm_send": "on",
        },
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize("status,code", [("Resolved", "status_closed"), (None, "status_invalid")])
def test_dry_run_has_same_status_gate_without_delivery(client, subscription, status, code):
    configure(subscription)
    require_open(subscription)
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        response = client.post(
            f"/customer-webhooks/{subscription.id}/test",
            data={
                **get_test_form(client, subscription),
                "payload": json.dumps(payload(incident(Status=status))),
                "action": "preview",
            },
        )
    assert response.status_code == 200
    assert code in response.get_data(as_text=True)
    post.assert_not_called()
    assert CustomerWebhookDelivery.query.count() == WebhookLog.query.count() == 0


def test_gui_saves_and_explicitly_disables_optional_status_filter(client, subscription):
    response = client.post("/customer-webhooks/new", data=form_data(subscription, status_path=STATUS_PATH))
    assert response.status_code == 302
    hook = CustomerWebhook.query.filter_by(name="Severity rule").one()
    assert hook.status_path == STATUS_PATH
    response = client.post(
        f"/customer-webhooks/{hook.id}/edit",
        data=form_data(hook, status_filter_present="1"),
    )
    assert response.status_code == 302 and hook.status_path == ""


def test_legacy_form_post_does_not_silently_disable_existing_status_filter(client, subscription):
    require_open(subscription)
    response = client.post(f"/customer-webhooks/{subscription.id}/edit", data=form_data(subscription))
    assert response.status_code == 302 and subscription.status_path == STATUS_PATH


def test_invalid_gui_status_path_keeps_saved_configuration(client, subscription):
    require_open(subscription)
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(subscription, status_path="$..Status"),
    )
    assert response.status_code == 400 and subscription.status_path == STATUS_PATH


class PayloadTextarea(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_payload = False
        self.content = ""

    def handle_starttag(self, tag, attrs):
        if tag == "textarea" and dict(attrs).get("name") == "payload":
            self.in_payload = True

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.in_payload = False

    def handle_data(self, data):
        if self.in_payload:
            self.content += data


def test_synthetic_sample_supplies_status_but_real_history_is_never_enriched(client, subscription):
    require_open(subscription)
    sample = PayloadTextarea()
    sample.feed(client.get(f"/customer-webhooks/{subscription.id}/test").get_data(as_text=True))
    assert json.loads(unescape(sample.content))["Results"][0]["Status"] == "Active"
    log = WebhookLog(
        config_id=subscription.config_id, request_id="real-without-status", payload=json.dumps(payload(incident()))
    )
    db.session.add(log)
    db.session.commit()
    history = PayloadTextarea()
    history.feed(client.get(f"/customer-webhooks/{subscription.id}/test?log_id={log.id}").get_data(as_text=True))
    assert "Status" not in json.loads(unescape(history.content))["Results"][0]


def test_backup_round_trip_restores_status_policy(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    configure(subscription)
    require_open(subscription)
    hook_id = subscription.id
    document = parse_backup(export_backup())
    assert document["customer_webhooks"][0]["status_path"] == STATUS_PATH
    db.session.delete(subscription)
    db.session.commit()
    restore_backup(document)
    restored = db.session.get(CustomerWebhook, hook_id)
    assert restored is not None
    assert restored.status_path == STATUS_PATH
    assert restored.severity_path == "$.Results[*].Severity"


def test_legacy_backup_without_status_filter_remains_compatible(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    require_open(subscription)
    document = parse_backup(export_backup())
    document["customer_webhooks"][0].pop("status_path")
    restore_backup(document)
    assert subscription.status_path == ""


def test_restored_status_policy_cancels_old_pending_payload(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    stage(subscription, payload(incident(Status="Resolved")))
    row = CustomerWebhookDelivery.query.one()
    generation = subscription.destination_generation
    document = parse_backup(export_backup())
    document["customer_webhooks"][0]["status_path"] = STATUS_PATH
    restore_backup(document)
    assert subscription.status_path == STATUS_PATH
    assert subscription.destination_generation > generation
    assert row.status == "cancelled" and row.payload == "{}"


@pytest.mark.parametrize("queue_status", ["pending", "sending"])
def test_status_migration_rollback_does_not_broaden_alarm_policy(subscription, queue_status):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    require_open(subscription)
    stage(subscription, payload(incident(Status="Active")))
    row = CustomerWebhookDelivery.query.one()
    row.status = queue_status
    legacy = CustomerWebhook(
        name="Legacy hook",
        config_id=subscription.config_id,
        company_id=subscription.company_id,
        target_url="https://legacy.example/notify",
        score_path="",
        minimum_score=0,
    )
    db.session.add(legacy)
    db.session.flush()
    db.session.add(CustomerWebhookDelivery(webhook_id=legacy.id, event_key="legacy", payload='{"alert":"keep"}'))
    db.session.commit()
    hook_id, legacy_id, delivery_id = subscription.id, legacy.id, row.id
    db.session.remove()
    migration_file = next(Path("migrations/versions").glob("*_add_forwarding_status_filter.py"))
    migration = importlib.import_module("migrations.versions." + migration_file.stem)
    with db.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        enabled = {
            record[0]: record[1] for record in connection.execute(text("SELECT id, is_enabled FROM customer_webhook"))
        }
        assert not enabled[hook_id] and enabled[legacy_id]
        deliveries = {
            record["id"]: record
            for record in connection.execute(
                text("SELECT id, status, payload FROM customer_webhook_delivery")
            ).mappings()
        }
        assert deliveries[delivery_id]["status"] == "cancelled" and deliveries[delivery_id]["payload"] == "{}"
        assert any(record["status"] == "pending" and "keep" in record["payload"] for record in deliveries.values())
        migration.upgrade()
        assert all(record[0] == "" for record in connection.execute(text("SELECT status_path FROM customer_webhook")))
