"""Severity gating isolates qualifying CIPP incidents before any customer alarm."""

import copy
import json
from html.parser import HTMLParser
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import CustomerWebhook, CustomerWebhookDelivery, WebhookConfig, WebhookLog
from hookwise.services import customer_webhooks as forwarding

SEVERITIES = ("informational", "low", "medium", "high", "critical")
SEVERITY_PATH = "$.Results[*].Severity"


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user_id="severity-test-admin", username="test", role="admin")
    return client


@pytest.fixture
def subscription(app):
    config = WebhookConfig(name="Cipp test source", customer_id_default="TEST-CUSTOMER", is_enabled=True)
    db.session.add(config)
    db.session.flush()
    hook = CustomerWebhook(
        name="Before rule",
        config_id=config.id,
        company_id="TEST-CUSTOMER",
        target_url="https://alarm.example/notify",
        score_path="",
        minimum_score=0,
    )
    db.session.add(hook)
    db.session.commit()
    return hook


def incident(severity="high", incident_id="test-high", **extra):
    return {
        "IncidentName": "Test incident " + incident_id,
        "IncidentID": incident_id,
        "Severity": severity,
        "Tenant": "customer.example",
        **extra,
    }


def payload(*incidents):
    return {
        "Tenant": "customer.example",
        "TaskInfo": {"Tenant": "AllTenants", "Command": "Get-CIPPAlertDefenderIncidents"},
        "Results": list(incidents),
    }


def configure(hook, minimum="high", path=SEVERITY_PATH):
    forwarding.set_severity_filter(hook, path, minimum)
    db.session.commit()


def stage(hook, data):
    log = WebhookLog(config_id=hook.config_id, request_id="severity-request", payload=json.dumps(data))
    db.session.add(log)
    db.session.flush()
    forwarding.stage_customer_deliveries(log, data)
    db.session.commit()
    return log


@pytest.mark.parametrize("minimum", SEVERITIES)
@pytest.mark.parametrize("incoming", SEVERITIES)
def test_every_severity_threshold_is_inclusive_and_ordered(subscription, minimum, incoming):
    configure(subscription, minimum)
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident(incoming)))
    expected = SEVERITIES.index(incoming) >= SEVERITIES.index(minimum)
    assert result.eligible is expected
    assert result.code == ("ready" if expected else "severity_below_threshold")
    if expected:
        assert result.matches is not None and len(result.matches) == 1
        assert result.matches[0][0] is None
    else:
        assert not result.matches
    assert CustomerWebhookDelivery.query.count() == 0


@pytest.mark.parametrize(
    ("incoming", "minimum"),
    [(" HIGH ", "high"), ("CrItIcAl", "critical"), (" INFO ", "informational"), ("Informational", "informational")],
)
def test_incoming_severity_allows_case_whitespace_and_info_alias(subscription, incoming, minimum):
    configure(subscription, minimum)
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident(incoming)))
    assert result.eligible is True and result.code == "ready"


@pytest.mark.parametrize("incoming", [None, True, 100, 0, "", "unknown", "urgent", "90", [], {}])
def test_missing_or_unrecognized_severity_is_not_promoted_by_numeric_score(subscription, incoming):
    configure(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident(incoming, Score=99)))
    assert result.eligible is False and result.code == "severity_invalid"
    assert not result.matches


def test_absent_severity_field_has_its_own_diagnosis(subscription):
    configure(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    result = forwarding.evaluate_customer_webhook(
        subscription, payload({"IncidentID": "missing-severity", "Score": 99})
    )
    assert result.eligible is False and result.code == "severity_missing"
    assert result.message
    assert not result.matches


@pytest.mark.parametrize("change", ["path", "minimum"])
def test_corrupt_saved_severity_filter_is_blocked_with_diagnosis(subscription, change):
    configure(subscription)
    if change == "path":
        subscription.severity_path = "$..Severity"
    else:
        subscription.minimum_severity = "unexpected"
    db.session.commit()
    result = forwarding.evaluate_customer_webhook(subscription, payload(incident()))
    assert result.eligible is False and result.code == "invalid_severity_filter"
    assert not result.matches


def test_mixed_batch_exposes_only_qualifying_sanitized_incidents(subscription):
    configure(subscription)
    data = payload(
        incident("informational", "noise"),
        incident("high", "qualified", password="private-password"),
        incident("unknown", "unknown"),
        incident("critical", "critical"),
        {"IncidentID": "missing"},
    )
    original = copy.deepcopy(data)
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is True and result.matches is not None
    assert {alert["IncidentID"] for _, alert in result.matches} == {"qualified", "critical"}
    assert all(score is None and "Results" not in alert for score, alert in result.matches)
    assert "private-password" not in json.dumps(result.matches)
    assert data == original


def test_unconfigured_severity_filter_preserves_legacy_whole_batch_forwarding(subscription):
    assert subscription.severity_path == ""
    assert subscription.minimum_severity == "high"
    data = payload(incident("informational", "noise"), {"IncidentID": "no-severity"})
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is True and result.matches == [(None, data)]


def test_disabled_severity_filter_preserves_existing_numeric_score_behavior(subscription):
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    data = payload(incident("informational", "qualified-score", Score=90), incident("high", "low-score", Score=20))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is True and result.matches is not None
    assert [(score, alert["IncidentID"]) for score, alert in result.matches] == [(90, "qualified-score")]


def test_score_and_severity_must_pass_on_the_same_incident(subscription):
    configure(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    data = payload(
        incident("high", "joint", Score=70),
        incident("critical", "score-too-low", Score=69),
        incident("low", "severity-too-low", Score=99),
    )
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is True and result.matches is not None
    assert [(score, alert["IncidentID"]) for score, alert in result.matches] == [(70, "joint")]


def test_score_from_one_incident_cannot_qualify_severity_from_another(subscription):
    configure(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    data = payload(incident("low", "score-only", Score=99), incident("high", "severity-only", Score=20))
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is False and result.code == "filters_no_joint_match"
    assert not result.matches
    stage(subscription, data)
    assert CustomerWebhookDelivery.query.count() == 0


def test_different_parent_objects_cannot_be_combined_even_within_one_incident(subscription):
    configure(subscription, path="$.Results[*].Details.Severity")
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    data = payload({"IncidentID": "different-contexts", "Score": 99, "Details": {"Severity": "high"}})
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is False and result.code == "filters_no_joint_match"
    assert not result.matches


def test_equal_incident_objects_are_independently_jointly_matched(subscription):
    configure(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    alert = incident("high", "equal-objects", Score=80)
    data = payload(copy.deepcopy(alert), copy.deepcopy(alert))
    assert data["Results"][0] is not data["Results"][1]
    result = forwarding.evaluate_customer_webhook(subscription, data)
    assert result.eligible is True and result.matches is not None
    assert len(result.matches) == 2
    assert all(score == 80 and match["IncidentID"] == "equal-objects" for score, match in result.matches)


def test_actual_pagerduty_dispatch_contains_only_eligible_incident(subscription):
    forwarding.set_destination(subscription, "https://events.eu.pagerduty.com/v2/enqueue", {})
    forwarding.set_payload_format(subscription, "pagerduty", "a" * 32)
    configure(subscription)
    stage(subscription, payload(incident("informational", "quiet-noise"), incident("high", "urgent-alert")))
    row = CustomerWebhookDelivery.query.one()
    envelope = json.loads(row.payload)
    assert envelope["alert"]["IncidentID"] == "urgent-alert"
    assert envelope["severity"] == "high" and envelope["minimum_severity"] == "high"
    assert "quiet-noise" not in row.payload
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as post:
        assert forwarding.dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
    post.assert_called_once()
    url, raw, headers = post.call_args.args
    event = json.loads(raw)
    assert url == "https://events.eu.pagerduty.com/v2/enqueue"
    assert event["payload"]["severity"] == "critical"
    assert event["payload"]["custom_details"]["alert"]["IncidentID"] == "urgent-alert"
    assert "quiet-noise" not in raw.decode()
    assert "Authorization" not in headers


def test_custom_severity_field_is_canonical_in_envelope_and_pagerduty(subscription):
    forwarding.set_destination(subscription, "https://events.eu.pagerduty.com/v2/enqueue", {})
    forwarding.set_payload_format(subscription, "pagerduty", "a" * 32)
    configure(subscription, path="$.Results[*].Priority")
    data = payload(
        {
            "IncidentID": "custom-priority",
            "IncidentName": "Custom priority alarm",
            "Priority": " HIGH ",
            "Severity": "low",
        }
    )
    stage(subscription, data)
    envelope = json.loads(CustomerWebhookDelivery.query.one().payload)
    assert envelope["severity"] == "high" and envelope["minimum_severity"] == "high"
    assert envelope["alert"]["Priority"] == " HIGH "
    with patch("hookwise.services.customer_webhooks.post_webhook", return_value=202) as post:
        assert forwarding.dispatch_customer_deliveries() == {"delivered": 1, "failed": 0}
    assert json.loads(post.call_args.args[1])["payload"]["severity"] == "critical"


class FormInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name"):
            self.values[fields["name"]] = fields.get("value", "")


def get_test_form(client, hook):
    response = client.get(f"/customer-webhooks/{hook.id}/test")
    assert response.status_code == 200
    parser = FormInputs()
    parser.feed(response.get_data(as_text=True))
    return parser.values


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (payload(incident("informational")), "severity_below_threshold"),
        (payload(incident("unknown")), "severity_invalid"),
        (payload({"IncidentID": "missing"}), "severity_missing"),
    ],
)
def test_preview_reports_severity_diagnosis_without_writes_or_network(client, subscription, data, code):
    configure(subscription)
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        response = client.post(
            f"/customer-webhooks/{subscription.id}/test",
            data={**get_test_form(client, subscription), "payload": json.dumps(data), "action": "preview"},
        )
    assert response.status_code == 200
    assert code in response.get_data(as_text=True)
    post.assert_not_called()
    assert CustomerWebhookDelivery.query.count() == WebhookLog.query.count() == 0


def form_data(hook, **overrides):
    return {
        "name": "Severity rule",
        "config_id": hook.config_id,
        "company_id": hook.company_id,
        "target_url": "https://alarm.example/notify",
        "score_path": "",
        "minimum_score": "",
        "is_enabled": "on",
        "severity_path": SEVERITY_PATH,
        "minimum_severity": "high",
        **overrides,
    }


def test_gui_create_and_edit_persist_severity_filter(client, subscription):
    assert client.post("/customer-webhooks/new", data=form_data(subscription)).status_code == 302
    hook = CustomerWebhook.query.filter_by(name="Severity rule").one()
    assert hook.severity_path == SEVERITY_PATH and hook.minimum_severity == "high"
    edit = client.get(f"/customer-webhooks/{hook.id}/edit").get_data(as_text=True)
    assert 'name="severity_path"' in edit and 'name="minimum_severity"' in edit
    response = client.post(f"/customer-webhooks/{hook.id}/edit", data=form_data(hook, minimum_severity="critical"))
    assert response.status_code == 302
    assert hook.minimum_severity == "critical"


@pytest.mark.parametrize(
    "changes",
    [
        {"severity_path": "$..Severity"},
        {"severity_path": "$.Results[*]"},
        {"severity_path": "$." + "a" * 201},
        {"minimum_severity": "urgent"},
        {"minimum_severity": "99"},
    ],
)
def test_gui_rejects_invalid_filter_without_changing_saved_rule(client, subscription, changes):
    configure(subscription)
    response = client.post(f"/customer-webhooks/{subscription.id}/edit", data=form_data(subscription, **changes))
    assert response.status_code == 400
    assert subscription.severity_path == SEVERITY_PATH and subscription.minimum_severity == "high"


def test_old_post_without_severity_fields_preserves_active_filter(client, subscription):
    configure(subscription)
    data = form_data(subscription)
    data.pop("severity_path")
    data.pop("minimum_severity")
    assert client.post(f"/customer-webhooks/{subscription.id}/edit", data=data).status_code == 302
    assert subscription.severity_path == SEVERITY_PATH and subscription.minimum_severity == "high"


def test_gui_marker_can_disable_filter_when_disabled_inputs_are_absent(client, subscription):
    configure(subscription)
    data = form_data(subscription)
    data.pop("severity_path")
    data.pop("minimum_severity")
    data["severity_filter_present"] = "1"
    assert client.post(f"/customer-webhooks/{subscription.id}/edit", data=data).status_code == 302
    assert subscription.severity_path == ""


def test_new_form_supplies_cipp_path_and_high_as_initial_severity_values(client, subscription):
    page = client.get("/customer-webhooks/new").get_data(as_text=True)
    assert 'value="$.Results[*].Severity"' in page
    assert '<option value="high" selected>' in page
    assert 'name="severity_filter_present" value="1"' in page


def test_form_error_keeps_submitted_severity_settings_without_saving(client, subscription):
    configure(subscription)
    response = client.post(
        f"/customer-webhooks/{subscription.id}/edit",
        data=form_data(
            subscription,
            score_path="$..Score",
            minimum_score="70",
            severity_path="$.Results[*].Urgency",
            minimum_severity="critical",
        ),
    )
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert 'value="$.Results[*].Urgency"' in page and '<option value="critical" selected>' in page
    assert subscription.severity_path == SEVERITY_PATH and subscription.minimum_severity == "high"


def test_overview_and_test_page_explain_joint_filters_and_disabled_severity(client, subscription):
    configure(subscription)
    subscription.score_path, subscription.minimum_score = "$.Results[*].Score", 70
    db.session.commit()
    for path in ("/customer-webhooks", f"/customer-webhooks/{subscription.id}/test"):
        page = client.get(path).get_data(as_text=True)
        assert "Beide Bedingungen" in page and "selben Alarmobjekt" in page
        assert SEVERITY_PATH in page and "$.Results[*].Score" in page
    configure(subscription, path="")
    for path in ("/customer-webhooks", f"/customer-webhooks/{subscription.id}/test"):
        assert "Alle Schweregrade" in client.get(path).get_data(as_text=True)


@pytest.mark.parametrize("change", ["enable", "minimum", "path", "disable"])
def test_filter_change_cancels_old_pending_deliveries_and_invalidates_generation(subscription, change):
    if change != "enable":
        configure(subscription)
    stage(subscription, payload(incident("critical")))
    row = CustomerWebhookDelivery.query.one()
    generation = subscription.destination_generation
    path, minimum = SEVERITY_PATH, "high"
    if change == "minimum":
        minimum = "critical"
    elif change == "path":
        path = "$.Results[0].Severity"
    elif change == "disable":
        path = ""
    configure(subscription, minimum, path)
    assert subscription.destination_generation > generation
    assert row.status == "cancelled" and row.payload == "{}"
    with patch("hookwise.services.customer_webhooks.post_webhook") as post:
        forwarding.dispatch_customer_deliveries()
    post.assert_not_called()


def test_unchanged_filter_preserves_pending_generation_and_delivery(subscription):
    configure(subscription)
    stage(subscription, payload(incident("critical")))
    row = CustomerWebhookDelivery.query.one()
    generation = subscription.destination_generation
    configure(subscription)
    assert subscription.destination_generation == generation
    assert row.status == "pending" and row.payload != "{}"


def test_invalid_setter_does_not_invalidate_existing_pending_delivery(subscription):
    configure(subscription)
    stage(subscription, payload(incident("critical")))
    generation = subscription.destination_generation
    with pytest.raises(ValueError):
        forwarding.set_severity_filter(subscription, "$..Severity", "high")
    assert subscription.destination_generation == generation
    assert CustomerWebhookDelivery.query.one().status == "pending"


def test_old_test_confirmation_cannot_send_after_severity_rule_change(client, subscription):
    configure(subscription)
    form = get_test_form(client, subscription)
    configure(subscription, "critical")
    response = client.post(
        f"/customer-webhooks/{subscription.id}/test",
        data={**form, "payload": json.dumps(payload(incident("critical"))), "action": "send", "confirm_send": "on"},
    )
    assert response.status_code == 400
    assert CustomerWebhookDelivery.query.count() == 0


def test_severity_filter_backup_round_trip(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    configure(subscription, "critical")
    original_id = subscription.id
    document = parse_backup(export_backup())
    record = document["customer_webhooks"][0]
    assert record["severity_path"] == SEVERITY_PATH and record["minimum_severity"] == "critical"
    db.session.delete(subscription)
    db.session.commit()
    restore_backup(document)
    restored = db.session.get(CustomerWebhook, original_id)
    assert restored is not None
    assert restored.severity_path == SEVERITY_PATH and restored.minimum_severity == "critical"


def test_legacy_backup_without_severity_fields_restores_disabled_filter(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    configure(subscription, "critical")
    document = parse_backup(export_backup())
    record = document["customer_webhooks"][0]
    record.pop("severity_path")
    record.pop("minimum_severity")
    restore_backup(document)
    assert subscription.severity_path == "" and subscription.minimum_severity == "high"


def test_restoring_changed_severity_policy_cancels_pending_generation(subscription):
    from hookwise.services.backups import export_backup, parse_backup, restore_backup

    configure(subscription)
    stage(subscription, payload(incident("critical")))
    row = CustomerWebhookDelivery.query.one()
    generation = subscription.destination_generation
    document = parse_backup(export_backup())
    document["customer_webhooks"][0]["minimum_severity"] = "critical"
    restore_backup(document)
    assert subscription.minimum_severity == "critical"
    assert subscription.destination_generation > generation
    assert row.status == "cancelled" and row.payload == "{}"


@pytest.mark.parametrize("status", ["pending", "sending"])
def test_migration_rollback_disables_filtered_hooks_without_broadening_old_alarm_policy(subscription, status):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    configure(subscription)
    stage(subscription, payload(incident("critical")))
    filtered = CustomerWebhookDelivery.query.one()
    filtered.status = status
    legacy = CustomerWebhook(
        name="Unfiltered existing hook",
        config_id=subscription.config_id,
        company_id=subscription.company_id,
        target_url="https://legacy.example/notify",
        score_path="",
        minimum_score=0,
    )
    db.session.add(legacy)
    db.session.flush()
    legacy_delivery = CustomerWebhookDelivery(
        webhook_id=legacy.id,
        event_key="rollback-unfiltered",
        destination_generation=legacy.destination_generation,
        payload='{"alert":{"IncidentID":"unfiltered"}}',
    )
    db.session.add(legacy_delivery)
    db.session.commit()
    filtered_id, unfiltered_id = subscription.id, legacy.id
    filtered_delivery_id, unfiltered_delivery_id = filtered.id, legacy_delivery.id
    db.session.remove()
    migration = importlib.import_module("migrations.versions.b6d8e0f2a4c6_add_forwarding_severity_filter")
    with db.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        enabled = {row[0]: row[1] for row in connection.execute(text("SELECT id, is_enabled FROM customer_webhook"))}
        assert not enabled[filtered_id] and enabled[unfiltered_id]
        deliveries = {
            row["id"]: row
            for row in connection.execute(text("SELECT id, status, payload FROM customer_webhook_delivery")).mappings()
        }
        assert deliveries[filtered_delivery_id]["status"] == "cancelled"
        assert deliveries[filtered_delivery_id]["payload"] == "{}"
        assert deliveries[unfiltered_delivery_id]["status"] == "pending"
        assert "unfiltered" in deliveries[unfiltered_delivery_id]["payload"]
        migration.upgrade()
        filters = connection.execute(text("SELECT severity_path, minimum_severity FROM customer_webhook")).all()
        assert all(path == "" and minimum == "high" for path, minimum in filters)
