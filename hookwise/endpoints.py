"""Endpoint CRUD routes: create, edit, toggle, clone, bulk operations."""

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from flask import Response, flash, jsonify, redirect, render_template, request, url_for

from .extensions import db
from .models import WebhookConfig
from .utils import auth_required, decrypt_string, encrypt_string, log_audit


def _register() -> None:
    from .routes import main_bp

    @main_bp.route("/endpoint/toggle-pin/<id>", methods=["POST"])
    @auth_required
    def toggle_pin(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        config.is_pinned = not config.is_pinned
        db.session.commit()
        action = "pin" if config.is_pinned else "unpin"
        log_audit(action, id, f"Endpoint {config.name} {action}ned")
        return jsonify({"status": "success", "is_pinned": config.is_pinned})

    @main_bp.route("/endpoint/reorder", methods=["POST"])
    @auth_required
    def reorder_endpoints() -> Any:
        order = request.json.get("order", [])
        for index, config_id in enumerate(order):
            config = WebhookConfig.query.get(config_id)
            if config:
                config.display_order = index
        db.session.commit()
        return jsonify({"status": "success"})

    @main_bp.route("/endpoint/new", methods=["GET", "POST"])
    @auth_required
    def new_endpoint() -> Any:
        if request.method == "POST":
            config = WebhookConfig(
                name=request.form.get("name"),
                customer_id_default=request.form.get("customer_id_default"),
                board=request.form.get("board"),
                status=request.form.get("status"),
                ticket_type=request.form.get("ticket_type"),
                subtype=request.form.get("subtype"),
                item=request.form.get("item"),
                priority=request.form.get("priority"),
                trigger_field=request.form.get("trigger_field") or "heartbeat.status",
                open_value=request.form.get("open_value") or "0",
                close_value=request.form.get("close_value") or "1",
                ticket_prefix=request.form.get("ticket_prefix"),
                description_template=request.form.get("description_template"),
                json_mapping=request.form.get("json_mapping"),
                routing_rules=request.form.get("routing_rules"),
                maintenance_windows=request.form.get("maintenance_windows"),
                trusted_ips=request.form.get("trusted_ips"),
                is_draft=request.form.get("is_draft") == "true",
                ai_rca_enabled=request.form.get("ai_rca_enabled") == "true",
                bearer_auth_enabled=request.form.get("bearer_auth_enabled") == "true",
                ai_prompt_template=request.form.get("ai_prompt_template"),
            )
            db.session.add(config)
            db.session.commit()
            log_audit("create", config.id, f"Endpoint {config.name} created")
            flash(f'Endpoint "{config.name}" {"saved as draft" if config.is_draft else "created successfully"}!')

            if request.form.get("create_another") == "true":
                return redirect(url_for("main.new_endpoint", confetti="true"))
            return redirect(url_for("main.index", confetti="true"))
        return render_template("form.html")

    @main_bp.route("/endpoint/edit/<id>", methods=["GET", "POST"])
    @auth_required
    def edit_endpoint(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        if request.method == "POST":
            config.name = request.form.get("name")
            config.customer_id_default = request.form.get("customer_id_default")
            config.board = request.form.get("board")
            config.status = request.form.get("status")
            config.ticket_type = request.form.get("ticket_type")
            config.subtype = request.form.get("subtype")
            config.item = request.form.get("item")
            config.priority = request.form.get("priority")
            config.trigger_field = request.form.get("trigger_field") or "heartbeat.status"
            config.open_value = request.form.get("open_value") or "0"
            config.close_value = request.form.get("close_value") or "1"
            config.ticket_prefix = request.form.get("ticket_prefix")
            config.description_template = request.form.get("description_template")
            config.json_mapping = request.form.get("json_mapping")
            config.routing_rules = request.form.get("routing_rules")
            config.maintenance_windows = request.form.get("maintenance_windows")
            config.trusted_ips = request.form.get("trusted_ips")
            config.is_draft = request.form.get("is_draft") == "true"
            config.ai_rca_enabled = request.form.get("ai_rca_enabled") == "true"
            config.bearer_auth_enabled = request.form.get("bearer_auth_enabled") == "true"
            config.ai_prompt_template = request.form.get("ai_prompt_template")

            db.session.commit()
            log_audit("update", config.id, f"Endpoint {config.name} updated")
            flash(f'Endpoint "{config.name}" updated successfully!')
            return redirect(url_for("main.index"))
        return render_template("form.html", config=config)

    @main_bp.route("/endpoint/toggle/<id>", methods=["POST"])
    @auth_required
    def toggle_endpoint(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        config.is_enabled = not config.is_enabled
        db.session.commit()
        action = "enable" if config.is_enabled else "disable"
        log_audit(action, id, f"Endpoint {config.name} {action}d")
        return jsonify({"status": "success", "is_enabled": config.is_enabled})

    @main_bp.route("/endpoint/rotate-token/<id>", methods=["POST"])
    @auth_required
    def rotate_token(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        new_token = secrets.token_urlsafe(32)
        config.bearer_token = encrypt_string(new_token)
        config.last_rotated_at = datetime.now(timezone.utc)
        db.session.commit()
        log_audit("rotate_token", id, f"Token for {config.name} rotated")

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({"status": "success", "token": new_token})

        flash(f'Token for "{config.name}" rotated successfully!')
        return redirect(url_for("main.index"))

    @main_bp.route("/endpoint/quick-update/<id>", methods=["POST"])
    @auth_required
    def quick_update_endpoint(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        field = request.json.get("field")
        value = request.json.get("value")

        if field in ["board", "priority"]:
            setattr(config, field, value)
            db.session.commit()
            log_audit("quick_update", id, f"Endpoint {config.name} {field} updated to {value}")
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "message": "Invalid field"}), 400

    @main_bp.route("/endpoint/clone/<id>", methods=["POST"])
    @auth_required
    def clone_endpoint(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        new_config = WebhookConfig(
            name=f"{config.name} (Copy)",
            customer_id_default=config.customer_id_default,
            board=config.board,
            status=config.status,
            ticket_type=config.ticket_type,
            subtype=config.subtype,
            item=config.item,
            priority=config.priority,
            trigger_field=config.trigger_field,
            open_value=config.open_value,
            close_value=config.close_value,
            ticket_prefix=config.ticket_prefix,
            description_template=config.description_template,
            json_mapping=config.json_mapping,
            routing_rules=config.routing_rules,
            maintenance_windows=config.maintenance_windows,
            trusted_ips=config.trusted_ips,
            ai_rca_enabled=config.ai_rca_enabled,
            bearer_auth_enabled=config.bearer_auth_enabled,
            ai_prompt_template=config.ai_prompt_template,
        )
        new_config.bearer_token = encrypt_string(secrets.token_urlsafe(32))

        db.session.add(new_config)
        db.session.commit()
        log_audit("clone", new_config.id, f"Endpoint {new_config.name} cloned from {config.id}")
        flash(f'Endpoint "{config.name}" cloned successfully!')
        return redirect(url_for("main.index"))

    @main_bp.route("/endpoint/token/<id>")
    @auth_required
    def get_endpoint_token(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        return jsonify({"token": decrypt_string(config.bearer_token)})

    @main_bp.route("/endpoint/delete/<id>", methods=["POST"])
    @auth_required
    def delete_endpoint(id: str) -> Any:
        config = WebhookConfig.query.get_or_404(id)
        name = config.name
        db.session.delete(config)
        db.session.commit()
        log_audit("delete", id, f"Endpoint {name} deleted")
        flash(f'Endpoint "{name}" deleted.')
        return redirect(url_for("main.index"))

    @main_bp.route("/endpoint/bulk/delete", methods=["POST"])
    @auth_required
    def bulk_delete_endpoints() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        log_audit("bulk_delete", None, f"Deleted endpoints: {', '.join(ids)}")
        return jsonify({"status": "success", "message": f"Deleted {len(ids)} endpoints"})

    @main_bp.route("/endpoint/bulk/pause", methods=["POST"])
    @auth_required
    def bulk_pause_endpoints() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).update({"is_enabled": False}, synchronize_session=False)
        db.session.commit()
        log_audit("bulk_pause", None, f"Paused endpoints: {', '.join(ids)}")
        return jsonify({"status": "success", "message": f"Paused {len(ids)} endpoints"})

    @main_bp.route("/endpoint/bulk/resume", methods=["POST"])
    @auth_required
    def bulk_resume_endpoints() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).update({"is_enabled": True}, synchronize_session=False)
        db.session.commit()
        log_audit("bulk_resume", None, f"Resumed endpoints: {', '.join(ids)}")
        return jsonify({"status": "success", "message": f"Resumed {len(ids)} endpoints"})

    @main_bp.route("/endpoint/bulk/export", methods=["POST"])
    @auth_required
    def bulk_export_endpoints() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        configs = WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).all()
        export_data = [c.to_dict() for c in configs]
        for c in export_data:
            c.pop("bearer_token", None)
            c.pop("id", None)
            c.pop("created_at", None)
            c.pop("last_seen_at", None)

        return Response(
            json.dumps(export_data, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment;filename=hookwise_export.json"},
        )


_register()
