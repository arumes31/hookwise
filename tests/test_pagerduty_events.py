"""Events v2 payload compatibility, batch preservation and key recipient safety."""

import copy
import json

import pytest

from hookwise.services.pagerduty_events import build_pagerduty_event, validate_pagerduty_url


def envelope():
    return {
        "event": "hookwise.customer_alert",
        "id": "incident-1349",
        "timestamp": "2026-10-23T15:09:35.2066667Z",
        "source": {"id": "cipp-source", "name": "Cipp"},
        "customer": {"id": "CUSTOMER"},
        "score": None,
        "alert": {
            "tenantId": "customer-tenant",
            "Tenant": "customer.example",
            "Results": [
                {
                    "IncidentName": "EICAR malware was prevented",
                    "Severity": "informational",
                    "IncidentID": "1349",
                    "IncidentUrl": "https://security.microsoft.com/incident2/1349/overview?tid=customer-tenant",
                }
            ],
        },
    }


def test_cipp_event_has_required_events_v2_fields_and_preserves_details():
    body = envelope()
    original = copy.deepcopy(body)
    event = build_pagerduty_event(body, "customer-integration-key")
    assert event["routing_key"] == "customer-integration-key"
    assert event["event_action"] == "trigger"
    assert event["dedup_key"] == "hookwise:incident-1349"
    assert event["payload"]["summary"] == "EICAR malware was prevented"
    assert event["payload"]["source"] == "Hookwise / Cipp"
    assert event["payload"]["severity"] == "info"
    assert event["payload"]["timestamp"] == body["timestamp"]
    assert event["payload"]["custom_details"]["alert"] == body["alert"]
    assert event["links"][0]["href"] == body["alert"]["Results"][0]["IncidentUrl"]
    assert "customer-integration-key" not in json.dumps(event["payload"])
    assert body == original


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("high", "critical"), ("medium", "error"), ("low", "warning"), ("informational", "info"), ("UNKNOWN", "error")],
)
def test_defender_severity_mapping(severity, expected):
    body = envelope()
    body["alert"]["Results"][0]["Severity"] = severity
    assert build_pagerduty_event(body, "key")["payload"]["severity"] == expected


def test_batch_summary_counts_all_incidents_and_uses_highest_severity():
    body = envelope()
    body["alert"]["Results"].append({"IncidentName": "Urgent malware", "Severity": "high", "IncidentID": "1350"})
    event = build_pagerduty_event(body, "key")
    assert event["payload"]["summary"] == "EICAR malware was prevented (+1 more incidents)"
    assert event["payload"]["severity"] == "critical"
    assert len(event["payload"]["custom_details"]["alert"]["Results"]) == 2


def test_test_marker_remains_visible_and_cannot_deduplicate_a_production_event():
    body = envelope()
    production = build_pagerduty_event(body, "key")
    body["test"] = True
    event = build_pagerduty_event(body, "key")
    assert event["payload"]["summary"].startswith("[TEST] ")
    assert event["payload"]["custom_details"]["test"] is True
    assert event["dedup_key"] != production["dedup_key"]


def test_summary_and_dedup_limits_are_bounded_without_changing_retry_identity():
    body = envelope()
    body["id"] = "long-identity" * 300
    body["test"] = True
    body["alert"]["Results"][0]["IncidentName"] = "Very long title " * 200
    body["alert"]["Results"].append({"IncidentName": "Second incident"})
    event = build_pagerduty_event(body, "key")
    assert len(event["payload"]["summary"]) == 1024
    assert event["payload"]["summary"].endswith(" (+1 more incidents)")
    assert len(event["dedup_key"]) <= 255
    body["timestamp"] = "2026-10-23T16:00:00Z"
    assert build_pagerduty_event(body, "key")["dedup_key"] == event["dedup_key"]


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-02-30T12:00:00Z", "2026-10-23", "2026-10-23T15:09:35"])
def test_invalid_or_ambiguous_event_time_is_omitted(timestamp):
    body = envelope()
    body["timestamp"] = timestamp
    assert "timestamp" not in build_pagerduty_event(body, "key")["payload"]


def test_sender_headers_and_private_url_metadata_cannot_leak_from_old_envelopes():
    body = envelope()
    body["alert"]["TaskInfo"] = {
        "Parameters": {"Headers": {"x-ms-client-principal": "private-identity", "Authorization": "sender-token"}},
        "PostExecutionResults": "Sent webhook to https://hookwise.example/w/private-ingress-token",
    }
    body["alert"]["password"] = "private-password"
    event = build_pagerduty_event(body, "key")
    serialized = json.dumps(event)
    assert "private-identity" not in serialized
    assert "sender-token" not in serialized
    assert "private-ingress-token" not in serialized
    assert "private-password" not in serialized
    assert event["payload"]["custom_details"]["alert"]["Results"] == body["alert"]["Results"]


@pytest.mark.parametrize(
    "url", ["http://customer.example/alert", "https://user:password@customer.example/alert", "not-a-url"]
)
def test_only_safe_https_incident_links_are_added(url):
    body = envelope()
    body["alert"]["Results"][0]["IncidentUrl"] = url
    assert "links" not in build_pagerduty_event(body, "key")


def test_legacy_envelope_can_be_adapted_without_new_fields():
    body = {"request_id": "old-request", "source_endpoint_id": "Cipp", "alert": {"title": "Legacy alert"}}
    event = build_pagerduty_event(body, "key")
    assert event["dedup_key"] == "hookwise:old-request"
    assert event["payload"]["summary"] == "Legacy alert"
    assert event["payload"]["source"] == "Hookwise / Cipp"
    assert "timestamp" not in event["payload"]


@pytest.mark.parametrize(
    "url",
    [
        "https://events.pagerduty.com/v2/enqueue",
        "https://events.eu.pagerduty.com/v2/enqueue",
        "https://events.pagerduty.com:443/v2/enqueue",
    ],
)
def test_global_and_eu_events_endpoints_are_accepted(url):
    validate_pagerduty_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://events.pagerduty.com/v2/enqueue",
        "https://events.pagerduty.com.evil.example/v2/enqueue",
        "https://events.pagerduty.com@evil.example/v2/enqueue",
        "https://user:password@events.pagerduty.com/v2/enqueue",
        "https://events.pagerduty.com:8443/v2/enqueue",
        "https://events.pagerduty.com:invalid/v2/enqueue",
        "https://events.pagerduty.com/v2/enqueue/",
        "https://events.pagerduty.com/v2/enqueue?key=secret",
        "https://events.pagerduty.com/v2/enqueue?",
        "https://events.pagerduty.com/v2/enqueue#fragment",
        "https://events.pagerduty.com/v2/enqueue#",
        "https://events.pagerduty.com/v2/enqueue\n",
    ],
)
def test_integration_key_cannot_be_sent_to_other_destinations(url):
    with pytest.raises(ValueError):
        validate_pagerduty_url(url)


@pytest.mark.parametrize("key", ["", "key with spaces", "key\r\nInjected: header"])
def test_missing_or_invalid_routing_keys_are_rejected(key):
    with pytest.raises(ValueError):
        build_pagerduty_event(envelope(), key)


def test_oversized_events_fail_locally_without_truncating_incident_details():
    body = envelope()
    body["alert"]["Results"][0]["Summary"] = "€" * 180_000
    with pytest.raises(ValueError, match="512"):
        build_pagerduty_event(body, "key")
