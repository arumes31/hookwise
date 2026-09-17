import logging
from typing import Any

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db
from .routes import main_bp
from .services.tenant_mappings import (
    TenantMappingValidationError,
    bump_mapping_cache_revision,
    create_mapping_group,
    delete_mapping_group,
    list_mapping_groups,
    mapping_alias_summary,
    replace_mapping_group,
)
from .utils import auth_required, log_audit

logger = logging.getLogger(__name__)


@main_bp.route("/tenantmap")
@auth_required
def tenantmap() -> Any:
    """Render one administrative row per logical TenantMap group."""
    mappings = list_mapping_groups()
    return render_template("tenantmap.html", mappings=mappings)


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
    """Delete a logical mapping and every flat alias it contains."""
    try:
        mapping = delete_mapping_group(mapping_id)
        if mapping is None:
            flash("Global mapping not found.")
            return redirect(url_for("main.tenantmap"))
        db.session.commit()
        bump_mapping_cache_revision()
        aliases = mapping_alias_summary(mapping.tenant_values)
        log_audit("delete_mapping", config_id=mapping.id, details=f"Deleted global mapping for: {aliases}")
        flash(f"Mapping with {len(mapping.tenant_values)} tenant value(s) deleted.")
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Failed to delete TenantMap group %s", mapping_id)
        flash("The mapping could not be deleted. Try again.")

    return redirect(url_for("main.tenantmap"))
