from typing import Any

from flask import flash, redirect, render_template, request, url_for

from .extensions import db
from .models import GlobalMapping
from .routes import main_bp
from .utils import auth_required, log_audit


@main_bp.route("/routing")
@auth_required
def routing() -> Any:
    mappings = GlobalMapping.query.order_by(GlobalMapping.tenant_value).all()
    return render_template("routing.html", mappings=mappings)


@main_bp.route("/routing/add", methods=["POST"])
@auth_required
def add_mapping() -> Any:
    tenant_value = request.form.get("tenant_value")
    company_id = request.form.get("company_id")
    description = request.form.get("description")

    if not tenant_value or not company_id:
        flash("Tenant Value and Company ID are required.")
        return redirect(url_for("main.routing"))

    mapping = GlobalMapping(
        tenant_value=tenant_value.strip(),
        company_id=company_id.strip(),
        description=description.strip() if description else None,
    )

    try:
        db.session.add(mapping)
        db.session.commit()
        log_audit("create_mapping", f"Added global mapping: {tenant_value} -> {company_id}")
        flash(f"Mapping for {tenant_value} added successfully.")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding mapping: {str(e)}")

    return redirect(url_for("main.routing"))


@main_bp.route("/routing/delete/<id>", methods=["POST"])
@auth_required
def delete_mapping(id: str) -> Any:
    mapping = GlobalMapping.query.get(id)
    if mapping:
        tenant = mapping.tenant_value
        db.session.delete(mapping)
        db.session.commit()
        log_audit("delete_mapping", f"Deleted global mapping for: {tenant}")
        flash(f"Mapping for {tenant} deleted.")
    return redirect(url_for("main.routing"))
