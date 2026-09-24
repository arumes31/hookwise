"""Transactional helpers for logical TenantMap groups."""

import json
import logging
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from redis.exceptions import RedisError
from sqlalchemy import or_

from ..extensions import db, redis_client
from ..models import GlobalMapping

MAX_TENANT_VALUES = 50
TENANT_MAPPING_REVISION_KEY = "hookwise:tenantmap:revision"
TENANT_MAPPING_UNDO_PREFIX = "hookwise:tenantmap:undo:"
TENANT_MAPPING_UNDO_TTL_SECONDS = 15

logger = logging.getLogger(__name__)


class TenantMappingValidationError(ValueError):
    """A mapping group contains missing, conflicting, or oversized values."""


class TenantMappingUndoUnavailable(RuntimeError):
    """The short-lived undo store could not be reached safely."""


@dataclass(frozen=True)
class TenantMappingGroup:
    """A logical mapping shown as one row while retaining flat match records."""

    id: str
    tenant_values: tuple[str, ...]
    company_id: str
    description: str | None


@dataclass(frozen=True)
class TenantMappingUndoSnapshot:
    """A deleted mapping that may be restored by its originating user."""

    original_mapping_id: str
    tenant_values: tuple[str, ...]
    company_id: str
    description: str | None


def normalize_tenant_match_value(value: object) -> str:
    """Canonicalize harmless tenant-value variations for deterministic matching.

    Domains are case-insensitive, may arrive with a trailing DNS dot, and are
    sometimes embedded in a URL or an email-like identifier. Parsing the host
    avoids unsafe substring matching such as ``trusted.example@evil.example``.
    Arbitrary names and tenant UUIDs are still supported through Unicode
    normalization and case folding.
    """
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if not normalized:
        return ""

    if "://" in normalized:
        try:
            parsed = urlsplit(normalized)
            if parsed.hostname:
                normalized = parsed.hostname
        except ValueError:
            # Malformed URL-like payloads remain inert strings and therefore do
            # not match a valid domain alias.
            return normalized.casefold()
    elif normalized.count("@") == 1 and not any(character.isspace() for character in normalized):
        local_part, domain = normalized.rsplit("@", 1)
        if local_part and domain and all(separator not in domain for separator in "/?#"):
            normalized = domain

    return normalized.rstrip(".").casefold()


def effective_group_id(mapping: GlobalMapping) -> str:
    """Return the persisted group ID or the row ID for legacy records."""
    return mapping.mapping_group_id or mapping.id


def group_mapping_rows(rows: Iterable[GlobalMapping]) -> list[TenantMappingGroup]:
    """Collapse flat matching rows into stable, alphabetically ordered groups."""
    grouped: dict[str, list[GlobalMapping]] = {}
    for row in rows:
        grouped.setdefault(effective_group_id(row), []).append(row)

    result: list[TenantMappingGroup] = []
    for group_id, group_rows in grouped.items():
        ordered = sorted(group_rows, key=lambda row: row.tenant_value.casefold())
        canonical = next((row for row in ordered if row.id == group_id), ordered[0])
        result.append(
            TenantMappingGroup(
                id=group_id,
                tenant_values=tuple(row.tenant_value for row in ordered),
                company_id=canonical.company_id,
                description=canonical.description,
            )
        )
    return sorted(result, key=lambda group: group.tenant_values[0].casefold())


def list_mapping_groups() -> list[TenantMappingGroup]:
    """Load all mappings and return one presentation record per logical group."""
    return group_mapping_rows(GlobalMapping.query.order_by(GlobalMapping.tenant_value).all())


def mapping_group_rows(mapping_id: str) -> list[GlobalMapping]:
    """Resolve either a row ID or group ID to every member of that group."""
    selected = db.session.get(GlobalMapping, mapping_id)
    group_id = effective_group_id(selected) if selected is not None else mapping_id
    rows = GlobalMapping.query.filter(
        or_(GlobalMapping.mapping_group_id == group_id, GlobalMapping.id == group_id)
    ).all()
    return sorted(rows, key=lambda row: row.tenant_value.casefold())


def normalize_mapping_input(
    raw_tenant_values: str | Sequence[str] | None,
    company_id: str | None,
    description: str | None,
) -> tuple[list[str], str, str | None]:
    """Normalize and validate aliases plus shared mapping metadata."""
    candidates = raw_tenant_values.splitlines() if isinstance(raw_tenant_values, str) else raw_tenant_values or []
    aliases: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        alias = normalize_tenant_match_value(candidate)
        if not alias or alias in seen:
            continue
        if len(alias) > 255:
            raise TenantMappingValidationError("Each tenant value must contain at most 255 characters.")
        aliases.append(alias)
        seen.add(alias)

    if not aliases:
        raise TenantMappingValidationError("At least one tenant value is required.")
    if len(aliases) > MAX_TENANT_VALUES:
        raise TenantMappingValidationError(f"A mapping can contain at most {MAX_TENANT_VALUES} tenant values.")

    normalized_company = (company_id or "").strip()
    if not normalized_company:
        raise TenantMappingValidationError("CW Company ID is required.")
    if len(normalized_company) > 50:
        raise TenantMappingValidationError("CW Company ID must contain at most 50 characters.")

    normalized_description = (description or "").strip() or None
    if normalized_description and len(normalized_description) > 255:
        raise TenantMappingValidationError("Description must contain at most 255 characters.")
    return aliases, normalized_company, normalized_description


def _ensure_aliases_available(aliases: Sequence[str], excluded_group_id: str | None = None) -> None:
    """Reject aliases already owned by a different logical mapping."""
    normalized_aliases = set(aliases)
    conflicts = [
        row for row in GlobalMapping.query.all() if normalize_tenant_match_value(row.tenant_value) in normalized_aliases
    ]
    external = [
        row.tenant_value
        for row in conflicts
        if excluded_group_id is None or effective_group_id(row) != excluded_group_id
    ]
    if external:
        shown = ", ".join(sorted(external, key=str.casefold)[:3])
        suffix = "" if len(external) <= 3 else f" and {len(external) - 3} more"
        raise TenantMappingValidationError(f"Tenant value already mapped: {shown}{suffix}.")


def _add_group_rows(
    group_id: str,
    aliases: Sequence[str],
    company_id: str,
    description: str | None,
) -> None:
    """Add the canonical row and its alias rows to the current transaction."""
    rows = [
        GlobalMapping(
            id=group_id if index == 0 else str(uuid.uuid4()),
            mapping_group_id=group_id,
            tenant_value=alias,
            company_id=company_id,
            description=description,
        )
        for index, alias in enumerate(aliases)
    ]
    db.session.add_all(rows)


def create_mapping_group(
    raw_tenant_values: str | Sequence[str] | None,
    company_id: str | None,
    description: str | None,
) -> TenantMappingGroup:
    """Stage a new logical mapping group in the current transaction."""
    aliases, normalized_company, normalized_description = normalize_mapping_input(
        raw_tenant_values, company_id, description
    )
    _ensure_aliases_available(aliases)
    group_id = str(uuid.uuid4())
    _add_group_rows(group_id, aliases, normalized_company, normalized_description)
    return TenantMappingGroup(group_id, tuple(aliases), normalized_company, normalized_description)


def replace_mapping_group(
    mapping_id: str,
    raw_tenant_values: str | Sequence[str] | None,
    company_id: str | None,
    description: str | None,
) -> TenantMappingGroup | None:
    """Atomically replace all aliases and shared metadata in an existing group."""
    current_rows = mapping_group_rows(mapping_id)
    if not current_rows:
        return None
    group_id = effective_group_id(current_rows[0])
    aliases, normalized_company, normalized_description = normalize_mapping_input(
        raw_tenant_values, company_id, description
    )
    _ensure_aliases_available(aliases, excluded_group_id=group_id)

    for row in current_rows:
        db.session.delete(row)
    db.session.flush()
    _add_group_rows(group_id, aliases, normalized_company, normalized_description)
    return TenantMappingGroup(group_id, tuple(aliases), normalized_company, normalized_description)


def delete_mapping_group(mapping_id: str) -> TenantMappingGroup | None:
    """Stage deletion of every alias in the selected logical mapping."""
    rows = mapping_group_rows(mapping_id)
    if not rows:
        return None
    group = group_mapping_rows(rows)[0]
    for row in rows:
        db.session.delete(row)
    return group


def mapping_alias_summary(aliases: Sequence[str]) -> str:
    """Return a bounded alias summary suitable for flashes and audit logs."""
    shown = ", ".join(aliases[:3])
    return shown if len(aliases) <= 3 else f"{shown} (+{len(aliases) - 3} more)"


def store_mapping_undo_snapshot(mapping: TenantMappingGroup, owner_id: str) -> str:
    """Persist a user-bound deletion snapshot and return its opaque token."""
    payload = json.dumps(
        {
            "version": 1,
            "owner_id": owner_id,
            "original_mapping_id": mapping.id,
            "tenant_values": mapping.tenant_values,
            "company_id": mapping.company_id,
            "description": mapping.description,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        for _ in range(3):
            token = secrets.token_urlsafe(24)
            if redis_client.set(
                f"{TENANT_MAPPING_UNDO_PREFIX}{token}",
                payload,
                ex=TENANT_MAPPING_UNDO_TTL_SECONDS,
                nx=True,
            ):
                return token
    except RedisError as error:
        raise TenantMappingUndoUnavailable("TenantMap undo storage is unavailable") from error
    raise TenantMappingUndoUnavailable("A unique TenantMap undo token could not be created")


def load_mapping_undo_snapshot(token: str, owner_id: str) -> TenantMappingUndoSnapshot | None:
    """Load a valid, unexpired deletion snapshot owned by the current user."""
    if not token or len(token) > 128:
        return None
    try:
        raw_payload = redis_client.get(f"{TENANT_MAPPING_UNDO_PREFIX}{token}")
    except RedisError as error:
        raise TenantMappingUndoUnavailable("TenantMap undo storage is unavailable") from error
    if raw_payload is None:
        return None

    try:
        payload = json.loads(raw_payload)
        tenant_values = payload["tenant_values"]
        description = payload.get("description")
        if (
            payload.get("version") != 1
            or payload.get("owner_id") != owner_id
            or not isinstance(payload.get("original_mapping_id"), str)
            or not isinstance(tenant_values, list)
            or not all(isinstance(value, str) for value in tenant_values)
            or not isinstance(payload.get("company_id"), str)
            or (description is not None and not isinstance(description, str))
        ):
            return None
        return TenantMappingUndoSnapshot(
            original_mapping_id=payload["original_mapping_id"],
            tenant_values=tuple(tenant_values),
            company_id=payload["company_id"],
            description=description,
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):  # fmt: skip
        logger.warning("TenantMap undo snapshot %s is invalid", token)
        return None


def discard_mapping_undo_snapshot(token: str) -> None:
    """Best-effort removal of a consumed or abandoned undo snapshot."""
    if not token or len(token) > 128:
        return
    try:
        redis_client.delete(f"{TENANT_MAPPING_UNDO_PREFIX}{token}")
    except RedisError:
        logger.warning("TenantMap undo snapshot %s could not be removed", token, exc_info=True)


def bump_mapping_cache_revision() -> None:
    """Invalidate worker-side mapping caches without failing a committed write."""
    try:
        redis_client.incr(TENANT_MAPPING_REVISION_KEY)
    except RedisError:
        logger.warning("TenantMap cache revision could not be incremented", exc_info=True)
