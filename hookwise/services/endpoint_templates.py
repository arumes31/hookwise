"""Built-in endpoint presets for common monitoring systems."""

from typing import Any

ENDPOINT_TEMPLATES: dict[str, dict[str, Any]] = {
    "uptime-kuma": {
        "label": "Uptime Kuma",
        "trigger_field": "$.heartbeat.status",
        "open_value": "0",
        "close_value": "1",
        "ticket_prefix": "Uptime:",
        "description_template": "{{ msg }}",
    },
    "zabbix": {
        "label": "Zabbix",
        "trigger_field": "$.event_value",
        "open_value": "1",
        "close_value": "0",
        "ticket_prefix": "Zabbix:",
        "json_mapping": '{"summary":"$.event_name","description":"$.event_description"}',
    },
    "grafana": {
        "label": "Grafana",
        "trigger_field": "$.status",
        "open_value": "firing",
        "close_value": "resolved",
        "ticket_prefix": "Grafana:",
        "json_mapping": '{"summary":"$.title","description":"$.message"}',
    },
    "datadog": {
        "label": "Datadog",
        "trigger_field": "$.alert_transition",
        "open_value": "Triggered,Warn",
        "close_value": "Recovered",
        "ticket_prefix": "Datadog:",
        "json_mapping": '{"summary":"$.title","description":"$.body"}',
    },
    "cipp": {
        "label": "CIPP",
        "trigger_field": "$.Status",
        "open_value": "Failed,Alert",
        "close_value": "Resolved,Success",
        "ticket_prefix": "CIPP:",
        "description_template": "{{ cipp_results }}",
        "global_routing_enabled": True,
    },
}


def public_endpoint_templates() -> dict[str, dict[str, Any]]:
    """Return copy-safe template data for rendering into the endpoint editor."""
    return {key: dict(value) for key, value in ENDPOINT_TEMPLATES.items()}
