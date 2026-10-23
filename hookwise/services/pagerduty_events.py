"""Translate the saved Hookwise alert envelope into PagerDuty Events API v2."""

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ..utils import mask_secrets

_EVENTS_HOSTS = {"events.pagerduty.com", "events.eu.pagerduty.com"}
_SEVERITIES = {
    "critical": "critical",
    "high": "critical",
    "error": "error",
    "medium": "error",
    "warning": "warning",
    "low": "warning",
    "info": "info",
    "informational": "info",
}
_SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def validate_pagerduty_url(url: str) -> None:
    """Restrict routing-key recipients to the global and EU Events endpoints."""
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname in _EVENTS_HOSTS
            and parts.port in (None, 443)
            and parts.path == "/v2/enqueue"
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and "?" not in url
            and "#" not in url
            and not any(ord(char) < 33 or ord(char) == 127 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "PagerDuty benötigt https://events.pagerduty.com/v2/enqueue oder "
            "https://events.eu.pagerduty.com/v2/enqueue ohne Zugangsdaten, Query oder Fragment."
        )


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _details(value: Any) -> Any:
    """Also remove sender metadata when adapting an older queued envelope."""
    if isinstance(value, list):
        return [_details(item) for item in value]
    if not isinstance(value, dict):
        return value
    masked = mask_secrets(value)
    for key, item in masked.items():
        if key.lower().startswith("x-ms-client-principal") or key.lower() in (
            "headers",
            "cookie",
            "set-cookie",
            "postexecutionresults",
        ):
            masked[key] = "***"
        else:
            masked[key] = _details(item)
    return masked


def _incidents(alert: Any) -> list[dict[str, Any]]:
    if isinstance(alert, dict):
        results = alert.get("Results")
        if isinstance(results, list):
            incidents = [item for item in results if isinstance(item, dict)]
            if incidents:
                return incidents
        return [alert]
    if isinstance(alert, list):
        return [item for item in alert if isinstance(item, dict)]
    return []


def _summary(incidents: list[dict[str, Any]], test: bool) -> str:
    first = incidents[0] if incidents else {}
    title = next(
        (text for field in ("IncidentName", "title", "Title", "name", "Name") if (text := _text(first.get(field)))),
        "Hookwise customer alert",
    )
    prefix = "[TEST] " if test else ""
    suffix = f" (+{len(incidents) - 1} more incidents)" if len(incidents) > 1 else ""
    return prefix + title[: 1024 - len(prefix) - len(suffix)] + suffix


def _severity(incidents: list[dict[str, Any]]) -> str:
    severities = [
        mapped
        for incident in incidents
        if (value := _text(incident.get("Severity") or incident.get("severity")))
        and (mapped := _SEVERITIES.get(value.lower()))
    ]
    return max(severities, key=_SEVERITY_ORDER.__getitem__) if severities else "error"


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or "T" not in value or value.strip() != value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if timestamp.tzinfo is not None and timestamp.utcoffset() is not None else None


def _incident_link(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        # Accessing port also rejects malformed URLs that urllib otherwise accepts.
        _ = parts.port
    except ValueError:
        return None
    return value


def build_pagerduty_event(payload: dict[str, Any], routing_key: str) -> dict[str, Any]:
    """Build an Events v2 trigger without network calls or changing its input.

    The canonical envelope ID remains the deduplication identity across delivery
    retries. Legacy queued envelopes can fall back to their original request ID.
    """
    if not routing_key or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in routing_key):
        raise ValueError("Ein gültiger PagerDuty Integration Key ist erforderlich.")
    details = _details(payload)
    incidents = _incidents(details.get("alert"))
    test = payload.get("test") is True
    identity = _text(payload.get("id")) or _text(payload.get("request_id"))
    if identity is None:
        identity = hashlib.sha256(json.dumps(details, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    dedup_key = ("hookwise:test:" if test else "hookwise:") + identity
    if len(dedup_key) > 255 or any(ord(char) < 33 or ord(char) == 127 for char in dedup_key):
        dedup_key = ("hookwise:test:" if test else "hookwise:") + hashlib.sha256(identity.encode()).hexdigest()
    source = details.get("source")
    source_name = _text(source.get("name")) or _text(source.get("id")) if isinstance(source, dict) else _text(source)
    source_name = source_name or _text(details.get("source_endpoint_id"))
    event_payload: dict[str, Any] = {
        "summary": _summary(incidents, test),
        "source": f"Hookwise / {source_name}" if source_name else "Hookwise",
        "severity": _severity(incidents),
        "custom_details": details,
    }
    timestamp = _timestamp(payload.get("timestamp"))
    if timestamp:
        event_payload["timestamp"] = timestamp
    event: dict[str, Any] = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": dedup_key,
        "client": "Hookwise",
        "payload": event_payload,
    }
    links = []
    seen_links: set[str] = set()
    for incident in incidents:
        link = _incident_link(incident.get("IncidentUrl"))
        if link and link not in seen_links:
            seen_links.add(link)
            links.append({"href": link, "text": _text(incident.get("IncidentName")) or "Original alert"})
    if links:
        event["links"] = links
    if len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 512 * 1024:
        raise ValueError("Der PagerDuty Event überschreitet das Größenlimit von 512 KiB.")
    return event
