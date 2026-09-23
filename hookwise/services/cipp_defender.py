"""CIPP Defender incident correlation and per-incident ticket state."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..extensions import db
from ..models import CippDefenderIncidentState

DEFAULT_BASELINE_HOURS = 48
MAX_BASELINE_HOURS = 24 * 30
_INCIDENT_ID_FIELDS = ("IncidentId", "IncidentID", "incidentId", "incident_id")
_ALERT_ID_FIELDS = ("AlertId", "AlertID", "alertId", "alert_id")
_CREATED_FIELDS = ("CreatedAt", "CreatedDateTime", "Created", "FirstActivityDateTime")


@dataclass(frozen=True)
class DefenderIncidentChange:
    """One Defender incident and the durable state needed to route it."""

    incident_key: str
    display_id: str
    data: dict[str, Any]
    payload_hash: str
    alert_ids: tuple[str, ...]
    new_alert_ids: tuple[str, ...]
    actionable: bool
    ticket_id: int | None
    legacy_bundle: bool = False


@dataclass(frozen=True)
class DefenderIncidentDelta:
    tenant_key: str
    incidents: tuple[DefenderIncidentChange, ...]

    @property
    def actionable_count(self) -> int:
        return sum(incident.actionable for incident in self.incidents)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def baseline_hours() -> int:
    """Return the first-run history cutoff; it never limits ticket correlation."""
    raw_value = os.environ.get(
        "CIPP_DEFENDER_BASELINE_HOURS",
        os.environ.get("CIPP_DEFENDER_BUNDLE_HOURS", str(DEFAULT_BASELINE_HOURS)),
    )
    try:
        return max(1, min(int(raw_value), MAX_BASELINE_HOURS))
    except TypeError, ValueError:
        return DEFAULT_BASELINE_HOURS


def is_defender_incident_payload(data: dict[str, Any]) -> bool:
    task_info = data.get("TaskInfo")
    command = str(task_info.get("Command", "")) if isinstance(task_info, dict) else ""
    normalized = command.casefold()
    return "defender" in normalized and "incident" in normalized and isinstance(data.get("Results"), list)


def _tenant_key(data: dict[str, Any]) -> str:
    task_info = data.get("TaskInfo")
    task_info = task_info if isinstance(task_info, dict) else {}
    results = data.get("Results")
    first_result = next((item for item in results if isinstance(item, dict)), {}) if isinstance(results, list) else {}
    candidates = (
        data.get("TenantId"),
        data.get("tenantId"),
        task_info.get("TenantId"),
        task_info.get("tenantId"),
        data.get("Tenant"),
        data.get("tenant"),
        task_info.get("Tenant"),
        task_info.get("tenant"),
        first_result.get("TenantId"),
        first_result.get("Tenant"),
    )
    value = next(
        (str(candidate).strip() for candidate in candidates if candidate is not None and str(candidate).strip()),
        "unknown",
    )
    return value.casefold()[:255]


def _field_value(item: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _incident_key(item: dict[str, Any]) -> str:
    incident_id = _field_value(item, _INCIDENT_ID_FIELDS)
    if incident_id:
        return f"id:{incident_id}"[:255]

    for field in ("IncidentUrl", "incidentUrl", "Incident URL"):
        url = item.get(field)
        if url:
            match = re.search(r"/incident2?/([^/?#]+)", str(url), re.IGNORECASE)
            if match:
                return f"url:{match.group(1)}"[:255]

    stable_fallback = {
        "name": item.get("IncidentName") or item.get("Title") or item.get("Name"),
        "created": _field_value(item, _CREATED_FIELDS),
    }
    digest = hashlib.sha256(json.dumps(stable_fallback, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _display_id(incident_key: str) -> str:
    kind, _, value = incident_key.partition(":")
    if kind == "hash":
        return value[:12]
    return value or incident_key[:24]


def _collect_alert_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        alert_id = _field_value(value, _ALERT_ID_FIELDS)
        if alert_id:
            found.add(alert_id)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                found.update(_collect_alert_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_collect_alert_ids(nested))
    return found


def _canonicalize(value: Any) -> Any:
    """Normalize unordered provider collections before hashing a snapshot."""
    if isinstance(value, dict):
        return {key: _canonicalize(nested) for key, nested in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_canonicalize(nested) for nested in value]
        return sorted(normalized, key=lambda nested: json.dumps(nested, sort_keys=True, default=str))
    return value


def _payload_hash(items: list[dict[str, Any]]) -> str:
    canonical_items = sorted(
        json.dumps(_canonicalize(item), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        for item in items
    )
    return hashlib.sha256("\n".join(canonical_items).encode("utf-8")).hexdigest()


def _stored_alert_ids(row: CippDefenderIncidentState | None) -> set[str]:
    if row is None:
        return set()
    try:
        values = json.loads(row.seen_alert_ids or "[]")
    except TypeError, ValueError:
        return set()
    return {str(value) for value in values} if isinstance(values, list) else set()


def _parse_created_at(item: dict[str, Any]) -> datetime | None:
    raw_value = _field_value(item, _CREATED_FIELDS)
    if not raw_value:
        return None
    normalized = raw_value.strip().replace("Z", "+00:00")
    normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d\d:\d\d$)", r"\1", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prepare_defender_incident_delta(
    config_id: str,
    data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> DefenderIncidentDelta | None:
    """Split a CIPP payload into independently actionable Defender incidents."""
    if not is_defender_incident_payload(data):
        return None

    current_time = now or _utcnow()
    tenant = _tenant_key(data)
    raw_results = [item for item in data.get("Results", []) if isinstance(item, dict)]
    grouped_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in raw_results:
        grouped_items[_incident_key(item)].append(item)

    tenant_query = CippDefenderIncidentState.query.filter_by(config_id=config_id, tenant_key=tenant)
    is_initial_delivery = tenant_query.with_entities(CippDefenderIncidentState.id).first() is None
    incident_keys = list(grouped_items)
    existing_rows = (
        tenant_query.filter(CippDefenderIncidentState.incident_key.in_(incident_keys)).all() if incident_keys else []
    )
    existing_by_key = {row.incident_key: row for row in existing_rows}
    historical_cutoff = current_time - timedelta(hours=baseline_hours())
    incidents: list[DefenderIncidentChange] = []

    for incident_key, items in grouped_items.items():
        payload_hash = _payload_hash(items)
        existing = existing_by_key.get(incident_key)
        current_alert_ids = {alert_id for item in items for alert_id in _collect_alert_ids(item)}
        previous_alert_ids = _stored_alert_ids(existing)
        legacy_bundle = existing is not None and bool(existing.bundle_key)
        changed = (
            existing is None
            or legacy_bundle
            or existing.payload_hash != payload_hash
            or bool(current_alert_ids - previous_alert_ids)
        )

        # Seed old incidents on a brand-new installation without opening a backlog.
        # This cutoff is only for the first delivery and never expires a ticket association.
        created_values = [created for item in items if (created := _parse_created_at(item)) is not None]
        is_historical_baseline = (
            is_initial_delivery
            and existing is None
            and bool(created_values)
            and max(created_values) < historical_cutoff
        )
        incident_data = dict(data)
        incident_data["Results"] = items
        incidents.append(
            DefenderIncidentChange(
                incident_key=incident_key,
                display_id=_display_id(incident_key),
                data=incident_data,
                payload_hash=payload_hash,
                alert_ids=tuple(sorted(previous_alert_ids | current_alert_ids)),
                new_alert_ids=tuple(sorted(current_alert_ids - previous_alert_ids)),
                actionable=changed and not is_historical_baseline,
                ticket_id=None if legacy_bundle or existing is None else existing.ticket_id,
                legacy_bundle=legacy_bundle,
            )
        )

    return DefenderIncidentDelta(tenant_key=tenant, incidents=tuple(incidents))


def persist_defender_incident(
    config_id: str,
    tenant_key: str,
    incident: DefenderIncidentChange,
    *,
    ticket_id: int | None,
    now: datetime | None = None,
) -> None:
    """Persist one incident after its ConnectWise side effect has succeeded."""
    current_time = now or _utcnow()
    row = CippDefenderIncidentState.query.filter_by(
        config_id=config_id,
        tenant_key=tenant_key,
        incident_key=incident.incident_key,
    ).one_or_none()
    if row is None:
        row = CippDefenderIncidentState(
            config_id=config_id,
            tenant_key=tenant_key,
            incident_key=incident.incident_key,
            payload_hash=incident.payload_hash,
            seen_alert_ids=json.dumps(incident.alert_ids),
            first_seen_at=current_time,
            last_seen_at=current_time,
            last_changed_at=current_time,
        )
        db.session.add(row)
    else:
        row.last_seen_at = current_time

    if incident.actionable:
        row.payload_hash = incident.payload_hash
        row.seen_alert_ids = json.dumps(incident.alert_ids)
        row.ticket_id = ticket_id
        row.bundle_key = None
        row.last_changed_at = current_time


def defender_incident_summary(
    tenant_key: str,
    incident: DefenderIncidentChange,
    *,
    limit: int = 99,
) -> str:
    """Build a compact, readable title for one Defender incident."""
    results = incident.data.get("Results")
    first = next((item for item in results if isinstance(item, dict)), {}) if isinstance(results, list) else {}
    task_info = incident.data.get("TaskInfo")
    task_info = task_info if isinstance(task_info, dict) else {}
    candidates = (
        incident.data.get("Tenant"),
        incident.data.get("tenant"),
        incident.data.get("TenantName"),
        task_info.get("Tenant"),
        task_info.get("tenant"),
        task_info.get("TenantName"),
        first.get("Tenant"),
        first.get("TenantName"),
        tenant_key,
    )
    tenant = next(
        (re.sub(r"\s+", " ", str(candidate)).strip() for candidate in candidates if str(candidate or "").strip()),
        "unknown",
    )[:30]
    return f"CIPP Defender: {tenant} #{incident.display_id[:24]}"[:limit]
