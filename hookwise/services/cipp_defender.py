"""CIPP Defender incident correlation and bounded ticket bundling."""

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

DEFAULT_BUNDLE_HOURS = 48
MAX_BUNDLE_HOURS = 24 * 30
_INCIDENT_ID_FIELDS = ("IncidentId", "IncidentID", "incidentId", "incident_id")
_ALERT_ID_FIELDS = ("AlertId", "AlertID", "alertId", "alert_id")
_CREATED_FIELDS = ("CreatedAt", "CreatedDateTime", "Created", "FirstActivityDateTime")


@dataclass(frozen=True)
class DefenderIncidentSnapshot:
    incident_key: str
    payload_hash: str
    alert_ids: tuple[str, ...]
    actionable: bool


@dataclass(frozen=True)
class DefenderIncidentDelta:
    data: dict[str, Any]
    tenant_key: str
    bundle_key: str
    snapshots: tuple[DefenderIncidentSnapshot, ...]

    @property
    def actionable_count(self) -> int:
        return sum(snapshot.actionable for snapshot in self.snapshots)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def bundle_hours() -> int:
    raw_value = os.environ.get("CIPP_DEFENDER_BUNDLE_HOURS", str(DEFAULT_BUNDLE_HOURS))
    try:
        return max(1, min(int(raw_value), MAX_BUNDLE_HOURS))
    except TypeError, ValueError:
        return DEFAULT_BUNDLE_HOURS


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


def _payload_hash(items: list[dict[str, Any]]) -> str:
    canonical_items = sorted(
        (json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str) for item in items)
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


def _bundle_key(now: datetime, hours: int) -> str:
    window_seconds = hours * 3600
    window_start = datetime.fromtimestamp((int(now.timestamp()) // window_seconds) * window_seconds, timezone.utc)
    return window_start.strftime("%Y%m%d-%H%MZ")


def prepare_defender_incident_delta(
    config_id: str,
    data: dict[str, Any],
    *,
    now: datetime | None = None,
) -> DefenderIncidentDelta | None:
    """Return only new or changed incidents, while retaining a durable baseline plan."""
    if not is_defender_incident_payload(data):
        return None

    current_time = now or _utcnow()
    hours = bundle_hours()
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
    historical_cutoff = current_time - timedelta(hours=hours)
    actionable_items: list[dict[str, Any]] = []
    snapshots: list[DefenderIncidentSnapshot] = []

    for incident_key, items in grouped_items.items():
        payload_hash = _payload_hash(items)
        existing = existing_by_key.get(incident_key)
        current_alert_ids = {alert_id for item in items for alert_id in _collect_alert_ids(item)}
        previous_alert_ids = _stored_alert_ids(existing)
        alert_ids = tuple(sorted(previous_alert_ids | current_alert_ids))
        changed = (
            existing is None or existing.payload_hash != payload_hash or bool(current_alert_ids - previous_alert_ids)
        )

        # On first deployment, seed old incidents without opening dozens of historical tickets.
        # Missing/unparseable timestamps remain actionable so security data cannot be silently lost.
        created_values = [created for item in items if (created := _parse_created_at(item)) is not None]
        is_historical_baseline = (
            is_initial_delivery
            and existing is None
            and bool(created_values)
            and max(created_values) < historical_cutoff
        )
        actionable = changed and not is_historical_baseline
        if actionable:
            actionable_items.extend(items)
        snapshots.append(
            DefenderIncidentSnapshot(
                incident_key=incident_key,
                payload_hash=payload_hash,
                alert_ids=alert_ids,
                actionable=actionable,
            )
        )

    filtered_data = dict(data)
    filtered_data["Results"] = actionable_items
    return DefenderIncidentDelta(
        data=filtered_data,
        tenant_key=tenant,
        bundle_key=_bundle_key(current_time, hours),
        snapshots=tuple(snapshots),
    )


def persist_defender_incident_delta(
    config_id: str,
    delta: DefenderIncidentDelta,
    *,
    ticket_id: int | None,
    now: datetime | None = None,
) -> None:
    """Apply a prepared delta only after its ConnectWise side effect succeeded."""
    current_time = now or _utcnow()
    keys = [snapshot.incident_key for snapshot in delta.snapshots]
    existing_rows = (
        CippDefenderIncidentState.query.filter(
            CippDefenderIncidentState.config_id == config_id,
            CippDefenderIncidentState.tenant_key == delta.tenant_key,
            CippDefenderIncidentState.incident_key.in_(keys),
        ).all()
        if keys
        else []
    )
    existing_by_key = {row.incident_key: row for row in existing_rows}

    for snapshot in delta.snapshots:
        row = existing_by_key.get(snapshot.incident_key)
        if row is None:
            row = CippDefenderIncidentState(
                config_id=config_id,
                tenant_key=delta.tenant_key,
                incident_key=snapshot.incident_key,
                payload_hash=snapshot.payload_hash,
                seen_alert_ids=json.dumps(snapshot.alert_ids),
                first_seen_at=current_time,
                last_seen_at=current_time,
                last_changed_at=current_time,
            )
            db.session.add(row)
        else:
            row.last_seen_at = current_time

        if snapshot.actionable:
            row.payload_hash = snapshot.payload_hash
            row.seen_alert_ids = json.dumps(snapshot.alert_ids)
            row.ticket_id = ticket_id
            row.bundle_key = delta.bundle_key
            row.last_changed_at = current_time


def defender_bundle_summary(summary: str, bundle_key: str, *, limit: int = 99) -> str:
    marker = f"[Defender {bundle_key}]"
    available = max(0, limit - len(marker) - 1)
    base = summary[:available].rstrip()
    return f"{base} {marker}".strip()
