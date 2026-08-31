from typing import Any

from flask import flash, redirect, render_template, request, url_for

from .extensions import db
from .models import CidMapping
from .routes import main_bp
from .utils import auth_required, log_audit


@main_bp.route("/cidmap")
@auth_required
def cidmap() -> Any:
    mappings = CidMapping.query.order_by(CidMapping.last_seen_at.desc()).all()
    return render_template("cidmap.html", mappings=mappings)


@main_bp.route("/cidmap/add", methods=["POST"])
@auth_required
def add_cid_mapping() -> Any:
    cid = (request.form.get("cid") or "").strip()
    customer = (request.form.get("customer_name") or "").strip()
    company = (request.form.get("company_id") or "").strip()
    if not cid or not company or len(cid) > 100 or len(customer) > 255 or len(company) > 50:
        flash("A valid CID and ConnectWise Company ID are required.")
        return redirect(url_for("main.cidmap"))
    try:
        row = CidMapping(cid=cid, customer_name=customer or None, company_id=company)
        db.session.add(row)
        db.session.commit()
        log_audit("create_cid_mapping", config_id=row.id, details=f"Mapped CID {cid} to {company}")
        flash(f"CID {cid} mapped successfully.")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error adding CID mapping: {exc}")
    return redirect(url_for("main.cidmap"))


@main_bp.route("/cidmap/edit/<mapping_id>", methods=["POST"])
@auth_required
def edit_cid_mapping(mapping_id: str) -> Any:
    row = db.session.get(CidMapping, mapping_id)
    if row is None:
        flash("CID mapping not found.")
        return redirect(url_for("main.cidmap"))
    company = (request.form.get("company_id") or "").strip()
    if not company or len(company) > 50:
        flash("A valid ConnectWise Company ID is required.")
        return redirect(url_for("main.cidmap"))
    row.company_id = company
    db.session.commit()
    log_audit("update_cid_mapping", config_id=row.id, details=f"Mapped CID {row.cid} to {company}")
    flash(f"CID {row.cid} mapped successfully.")
    return redirect(url_for("main.cidmap"))


@main_bp.route("/cidmap/delete/<mapping_id>", methods=["POST"])
@auth_required
def delete_cid_mapping(mapping_id: str) -> Any:
    row = db.session.get(CidMapping, mapping_id)
    if row is None:
        flash("CID mapping not found.")
        return redirect(url_for("main.cidmap"))
    cid = row.cid
    db.session.delete(row)
    db.session.commit()
    log_audit("delete_cid_mapping", config_id=mapping_id, details=f"Deleted CID mapping {cid}")
    flash(f"CID {cid} deleted.")
    return redirect(url_for("main.cidmap"))
