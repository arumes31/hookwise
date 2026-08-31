"""Versioned, authenticated configuration backup and restore service."""

import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import InvalidToken

from ..extensions import db
from ..models import CidMapping, EndpointTag, GlobalMapping, User, UserPreference, WebhookConfig
from ..utils import get_fernet

BACKUP_FORMAT = "hookwise-config"
BACKUP_VERSION = 2
MAX_BACKUP_BYTES = 5 * 1024 * 1024
MAX_CONFIGS = 10_000
CONFIG_FIELDS = {
    "name",
    "customer_id_default",
    "board",
    "status",
    "close_status",
    "ticket_type",
    "subtype",
    "item",
    "priority",
    "trigger_field",
    "open_value",
    "close_value",
    "ticket_prefix",
    "description_template",
    "summary_remove_strings",
    "json_mapping",
    "routing_rules",
    "maintenance_windows",
    "trusted_ips",
    "bearer_token",
    "hmac_secret",
    "bearer_auth_enabled",
    "allow_unauthenticated",
    "rate_limit_per_minute",
    "retry_enabled",
    "retry_max_attempts",
    "retry_base_delay_seconds",
    "retry_max_delay_seconds",
    "is_enabled",
    "is_pinned",
    "is_draft",
    "display_order",
    "ai_rca_enabled",
    "ai_prompt_template",
    "global_routing_enabled",
    "timeout_alerts_enabled",
    "timeout_hours",
}
INT_BOUNDS = {
    "rate_limit_per_minute": (1, 10_000),
    "retry_max_attempts": (0, 20),
    "retry_base_delay_seconds": (1, 3_600),
    "retry_max_delay_seconds": (1, 86_400),
    "timeout_hours": (0, 8_760),
    "display_order": (0, 1_000_000),
}


class BackupValidationError(ValueError):
    """The uploaded backup is unauthenticated, unsupported, or malformed."""


def _config_record(config: WebhookConfig) -> dict[str, Any]:
    record = config.to_dict(include_token=True)
    record.update(
        {
            "is_draft": config.is_draft,
            "display_order": config.display_order,
            "tags": [tag.name for tag in config.tags],
        }
    )
    return {"id": config.id, **{key: value for key, value in record.items() if key in CONFIG_FIELDS or key == "tags"}}


def export_backup() -> bytes:
    """Return an encrypted and authenticated portable backup token."""
    document = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configs": [_config_record(config) for config in WebhookConfig.query.order_by(WebhookConfig.id).all()],
        "global_mappings": [mapping.to_dict() for mapping in GlobalMapping.query.order_by(GlobalMapping.id).all()],
        "cid_mappings": [mapping.to_dict() for mapping in CidMapping.query.order_by(CidMapping.cid).all()],
        "preferences": [
            {
                "username": preference.user.username,
                "dashboard_layout": preference.dashboard_layout,
                "hidden_kpis": preference.hidden_kpis,
                "dashboard_compact_mode": preference.dashboard_compact_mode,
                "dashboard_refresh_interval": preference.dashboard_refresh_interval,
                "timezone": preference.timezone,
                "activity_buffer_size": preference.activity_buffer_size,
                "browser_notifications_enabled": preference.browser_notifications_enabled,
                "sound_notifications_enabled": preference.sound_notifications_enabled,
            }
            for preference in UserPreference.query.join(User).order_by(User.username).all()
        ],
    }
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    return get_fernet().encrypt(raw)


def parse_backup(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_BACKUP_BYTES:
        raise BackupValidationError("Backup is empty or exceeds 5 MiB")
    try:
        if raw.lstrip().startswith((b"[", b"{")):
            decoded = json.loads(raw)
            document = {"format": "legacy", "version": 1, "configs": decoded} if isinstance(decoded, list) else decoded
        else:
            document = json.loads(get_fernet().decrypt(raw))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupValidationError("Backup authentication or JSON validation failed") from exc
    if not isinstance(document, dict):
        raise BackupValidationError("Backup root must be an object")
    version = document.get("version")
    if version not in {1, BACKUP_VERSION}:
        raise BackupValidationError("Unsupported backup version")
    configs = document.get("configs")
    if not isinstance(configs, list) or len(configs) > MAX_CONFIGS:
        raise BackupValidationError("Backup contains an invalid configuration list")
    return document


def _bounded(field: str, value: Any) -> int:
    minimum, maximum = INT_BOUNDS[field]
    if isinstance(value, bool):
        raise BackupValidationError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BackupValidationError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise BackupValidationError(f"{field} is outside supported bounds")
    return parsed


def restore_backup(document: dict[str, Any]) -> int:
    """Validate the complete document, then restore it in one transaction."""
    configs = document["configs"]
    ids: list[str] = []
    for record in configs:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise BackupValidationError("Every configuration requires a non-empty string id")
        if len(record["id"]) > 64 or set(record) - (CONFIG_FIELDS | {"id", "tags"}):
            raise BackupValidationError("Configuration contains unsupported fields")
        if not isinstance(record.get("name"), str) or not 1 <= len(record["name"]) <= 100:
            raise BackupValidationError("Every configuration requires a valid name")
        ids.append(record["id"])
    if len(ids) != len(set(ids)):
        raise BackupValidationError("Backup contains duplicate configuration ids")

    existing = {row.id: row for row in WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).all()} if ids else {}
    for record in configs:
        config = existing.get(record["id"])
        if config is None:
            config = WebhookConfig(id=record["id"], name=record["name"])
            db.session.add(config)
        for field in CONFIG_FIELDS:
            if field not in record:
                continue
            value = _bounded(field, record[field]) if field in INT_BOUNDS else record[field]
            if isinstance(value, str) and len(value) > 100_000:
                raise BackupValidationError(f"{field} exceeds the maximum length")
            setattr(config, field, value)
        tags = record.get("tags", [])
        if (
            not isinstance(tags, list)
            or len(tags) > 12
            or any(not isinstance(tag, str) or len(tag) > 64 for tag in tags)
        ):
            raise BackupValidationError("Configuration tags are invalid")
        if tags:
            known = {row.name: row for row in EndpointTag.query.filter(EndpointTag.name.in_(tags)).all()}
            config.tags = [known.get(name) or EndpointTag(name=name) for name in tags]
        base_delay = int(config.retry_base_delay_seconds or 1)
        max_delay = int(config.retry_max_delay_seconds or 300)
        if max_delay < base_delay:
            raise BackupValidationError("retry_max_delay_seconds must be at least retry_base_delay_seconds")
        bearer_enabled = config.bearer_auth_enabled is not False
        if not bearer_enabled and not config.hmac_secret and not config.allow_unauthenticated:
            raise BackupValidationError(f"Endpoint {config.name} has no approved authentication mode")

    mappings = document.get("global_mappings", [])
    if not isinstance(mappings, list) or len(mappings) > MAX_CONFIGS:
        raise BackupValidationError("Global mappings are invalid")
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise BackupValidationError("Global mapping must be an object")
        tenant = mapping.get("tenant_value")
        company = mapping.get("company_id")
        if not isinstance(tenant, str) or not tenant or len(tenant) > 255:
            raise BackupValidationError("Global mapping tenant is invalid")
        if not isinstance(company, str) or not company or len(company) > 50:
            raise BackupValidationError("Global mapping company is invalid")
        row = GlobalMapping.query.filter_by(tenant_value=tenant).first() or GlobalMapping(tenant_value=tenant)
        row.company_id = company
        row.description = str(mapping.get("description") or "")[:255] or None
        db.session.add(row)

    cid_mappings = document.get("cid_mappings", [])
    if not isinstance(cid_mappings, list) or len(cid_mappings) > MAX_CONFIGS:
        raise BackupValidationError("CID mappings are invalid")
    for mapping in cid_mappings:
        if not isinstance(mapping, dict):
            raise BackupValidationError("CID mapping must be an object")
        cid = mapping.get("cid")
        company = mapping.get("company_id")
        customer = mapping.get("customer_name")
        if not isinstance(cid, str) or not cid or len(cid) > 100:
            raise BackupValidationError("CID mapping CID is invalid")
        if company is not None and (not isinstance(company, str) or not company or len(company) > 50):
            raise BackupValidationError("CID mapping company is invalid")
        if customer is not None and (not isinstance(customer, str) or len(customer) > 255):
            raise BackupValidationError("CID mapping customer is invalid")
        row = CidMapping.query.filter_by(cid=cid).first() or CidMapping(cid=cid)
        row.company_id = company
        row.customer_name = customer
        db.session.add(row)

    preferences = document.get("preferences", [])
    if not isinstance(preferences, list) or len(preferences) > MAX_CONFIGS:
        raise BackupValidationError("User preferences are invalid")
    for record in preferences:
        if not isinstance(record, dict) or not isinstance(record.get("username"), str):
            raise BackupValidationError("User preference owner is invalid")
        user = User.query.filter_by(username=record["username"]).first()
        if user is None:
            continue
        preference = UserPreference.query.filter_by(user_id=user.id).first() or UserPreference(user_id=user.id)
        for field in (
            "dashboard_layout",
            "hidden_kpis",
            "dashboard_compact_mode",
            "dashboard_refresh_interval",
            "timezone",
            "activity_buffer_size",
            "browser_notifications_enabled",
            "sound_notifications_enabled",
        ):
            if field in record:
                setattr(preference, field, record[field])
        db.session.add(preference)
    db.session.commit()
    return len(configs)
