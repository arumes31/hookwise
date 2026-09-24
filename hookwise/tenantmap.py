import logging
import time
from typing import Any

from flask import flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .routes import main_bp
from .services.tenant_mappings import (
    TENANT_MAPPING_UNDO_TTL_SECONDS,
    TenantMappingUndoUnavailable,
    TenantMappingValidationError,
    bump_mapping_cache_revision,
    create_mapping_group,
    delete_mapping_group,
    discard_mapping_undo_snapshot,
    list_mapping_groups,
    load_mapping_undo_snapshot,
    mapping_alias_summary,
    replace_mapping_group,
    store_mapping_undo_snapshot,
)
from .utils import auth_required, log_audit

logger = logging.getLogger(__name__)

_UNDO_TOKEN_SESSION_KEY = "tenantmap_undo_token"
_UNDO_EXPIRY_SESSION_KEY = "tenantmap_undo_expires_at"


def _undo_owner_id() -> str:
    """Return a stable identity that binds undo data to this authenticated user."""
    user_id = session.get("user_id")
    if user_id is not None:
        return f"user:{user_id}"
    username = session.get("username")
    if username:
        return f"username:{username}"
    authorization = request.authorization
    return f"basic:{authorization.username}" if authorization and authorization.username else "unknown"


def _clear_undo_session() -> None:
    """Remove the browser pointer to the most recent deletion snapshot."""
    session.pop(_UNDO_TOKEN_SESSION_KEY, None)
    session.pop(_UNDO_EXPIRY_SESSION_KEY, None)


def _active_undo_token() -> str | None:
    """Return the unexpired undo token stored in this browser session."""
    token = session.get(_UNDO_TOKEN_SESSION_KEY)
    expires_at = session.get(_UNDO_EXPIRY_SESSION_KEY)
    if isinstance(token, str) and isinstance(expires_at, (int, float)) and expires_at > time.time():
        return token
    _clear_undo_session()
    return None


@main_bp.route("/tenantmap")
@auth_required
def tenantmap() -> Any:
    """Render one administrative row per logical TenantMap group."""
    mappings = list_mapping_groups()
    return render_template("tenantmap.html", mappings=mappings, undo_available=_active_undo_token() is not None)


@main_bp.route("/tenantmap/add", methods=["POST"])
@auth_required
def add_mapping() -> Any:
    """Create a logical mapping containing one or more tenant aliases."""
    tenant_values = request.form.get("tenant_values") or request.form.get("tenant_value")
    company_id = request.form.get("company_id")
    description = request.form.get("description")

    try:
        mapping = create_mapping_group(tenant_values, company_id, description)
        db.session.commit()
        bump_mapping_cache_revision()
        aliases = mapping_alias_summary(mapping.tenant_values)
        log_audit(
            "create_mapping", config_id=mapping.id, details=f"Added global mapping: {aliases} -> {mapping.company_id}"
        )
        flash(f"Mapping with {len(mapping.tenant_values)} tenant value(s) added successfully.")
    except TenantMappingValidationError as error:
        db.session.rollback()
        flash(str(error))
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to add TenantMap group")
        flash("The mapping could not be added. Check for duplicate tenant values and try again.")

    return redirect(url_for("main.tenantmap"))


@main_bp.route("/tenantmap/edit/<mapping_id>", methods=["POST"])
@auth_required
def edit_mapping(mapping_id: str) -> Any:
    """Replace the aliases and shared metadata of one logical mapping."""
    tenant_values = request.form.get("tenant_values") or request.form.get("tenant_value")
    company_id = request.form.get("company_id")
    description = request.form.get("description")

    try:
        mapping = replace_mapping_group(mapping_id, tenant_values, company_id, description)
        if mapping is None:
            flash("Global mapping not found.")
            return redirect(url_for("main.tenantmap"))
        db.session.commit()
        bump_mapping_cache_revision()
        aliases = mapping_alias_summary(mapping.tenant_values)
        log_audit(
            "update_mapping",
            config_id=mapping.id,
            details=f"Updated global mapping: {aliases} -> {mapping.company_id}",
        )
        flash(f"Mapping with {len(mapping.tenant_values)} tenant value(s) updated successfully.")
    except TenantMappingValidationError as error:
        db.session.rollback()
        flash(str(error))
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to update TenantMap group %s", mapping_id)
        flash("The mapping could not be updated. Check for duplicate tenant values and try again.")

    return redirect(url_for("main.tenantmap"))


@main_bp.route("/tenantmap/delete/<mapping_id>", methods=["POST"])
@auth_required
def delete_mapping(mapping_id: str) -> Any:
    """Delete a logical mapping after securing a short-lived undo snapshot."""
    undo_token: str | None = None
    try:
        mapping = delete_mapping_group(mapping_id)
        if mapping is None:
            flash("Global mapping not found.")
            return redirect(url_for("main.tenantmap"))
        undo_token = store_mapping_undo_snapshot(mapping, _undo_owner_id())
        aliases = mapping_alias_summary(mapping.tenant_values)
        log_audit(
            "delete_mapping",
            config_id=mapping.id,
            details=f"Deleted global mapping for: {aliases}",
            commit=False,
        )
        db.session.commit()
        bump_mapping_cache_revision()
        session[_UNDO_TOKEN_SESSION_KEY] = undo_token
        session[_UNDO_EXPIRY_SESSION_KEY] = time.time() + TENANT_MAPPING_UNDO_TTL_SECONDS
    except TenantMappingUndoUnavailable:
        db.session.rollback()
        logger.warning("TenantMap deletion stopped because undo storage is unavailable", exc_info=True)
        flash("The mapping was not deleted because a safe undo snapshot could not be created. Try again.")
    except SQLAlchemyError:
        db.session.rollback()
        if undo_token:
            discard_mapping_undo_snapshot(undo_token)
        logger.exception("Failed to delete TenantMap group %s", mapping_id)
        flash("The mapping could not be deleted. Try again.")

    return redirect(url_for("main.tenantmap"))


@main_bp.route("/tenantmap/delete/undo", methods=["POST"])
@auth_required
def undo_delete_mapping() -> Any:
    """Restore the most recently deleted mapping while its snapshot is valid."""
    token = _active_undo_token()
    if token is None:
        flash("The undo period has expired. The mapping was not restored.")
        return redirect(url_for("main.tenantmap"))

    try:
        snapshot = load_mapping_undo_snapshot(token, _undo_owner_id())
        if snapshot is None:
            _clear_undo_session()
            flash("The undo period has expired. The mapping was not restored.")
            return redirect(url_for("main.tenantmap"))
        mapping = create_mapping_group(snapshot.tenant_values, snapshot.company_id, snapshot.description)
        aliases = mapping_alias_summary(mapping.tenant_values)
        log_audit(
            "restore_mapping",
            config_id=mapping.id,
            details=f"Restored deleted global mapping for: {aliases}",
            commit=False,
        )
        db.session.commit()
        bump_mapping_cache_revision()
        discard_mapping_undo_snapshot(token)
        _clear_undo_session()
        flash(f"Mapping with {len(mapping.tenant_values)} tenant value(s) restored.")
    except TenantMappingValidationError as error:
        db.session.rollback()
        flash(f"The mapping could not be restored: {error}")
    except TenantMappingUndoUnavailable:
        db.session.rollback()
        logger.warning("TenantMap undo failed because undo storage is unavailable", exc_info=True)
        flash("Undo is temporarily unavailable. Try again before the undo period expires.")
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to restore deleted TenantMap group")
        flash("The mapping could not be restored. Try again before the undo period expires.")

    return redirect(url_for("main.tenantmap"))
