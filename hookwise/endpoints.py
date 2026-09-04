"""Endpoint CRUD routes: create, edit, toggle, clone, bulk operations."""

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from flask import Response, flash, jsonify, redirect, render_template, request, session, url_for

from .extensions import db
from .models import EndpointTag, WebhookConfig, WebhookLog
from .services.endpoint_templates import public_endpoint_templates
from .utils import auth_required, decrypt_string, encrypt_string, log_audit

_TAG_NAME = re.compile(r"^[\w .:/-]{1,32}$")


def _operator_denied() -> Any:
    if session.get("role") not in {"admin", "operator"}:
        return jsonify({"error": "An operator role is required for this action."}), 403
    return None


def _set_tags(config: WebhookConfig, raw: str | None) -> None:
    names = list(dict.fromkeys(part.strip() for part in (raw or "").split(",") if part.strip()))
    if len(names) > 12 or any(not _TAG_NAME.fullmatch(name) for name in names):
        raise ValueError("Use at most 12 tags; each tag may contain 1-32 letters, numbers, spaces, . : / or -.")
    existing = {tag.name: tag for tag in EndpointTag.query.filter(EndpointTag.name.in_(names)).all()} if names else {}
    config.tags = [existing.get(name) or EndpointTag(name=name) for name in names]


def _get_int_form_value(key: str, default: int = 24, min_val: int = 1, max_val: int = 168) -> int:
    """Safely parse an integer from form data with bounds checking."""
    val = request.form.get(key)
    if not val or not val.strip():
        return default
    try:
        parsed = int(val)
        return max(min_val, min(max_val, parsed))
    except ValueError, TypeError:
        return default


def _authentication_error(config: WebhookConfig) -> str | None:
    if config.bearer_auth_enabled or config.hmac_secret or config.allow_unauthenticated:
        return None
    return "Enable bearer authentication, configure HMAC, or explicitly allow an unauthenticated endpoint."


def _register_crud_routes(main_bp: Any) -> None:
    @main_bp.route("/endpoint/toggle-pin/<config_id>", methods=["POST"])
    @auth_required
    def toggle_pin(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        config.is_pinned = not config.is_pinned
        db.session.commit()
        action = "pin" if config.is_pinned else "unpin"
        log_audit(action, config_id, f"Endpoint {config.name} {action}ned")
        return jsonify({"status": "success", "is_pinned": config.is_pinned})

    @main_bp.route("/endpoint/reorder", methods=["POST"])
    @auth_required
    def reorder_endpoints() -> Any:
        order = request.json.get("order", [])
        if not order:
            return jsonify({"status": "success"})

        # Validation: check for duplicates in the payload
        if len(set(order)) != len(order):
            return jsonify({"status": "error", "message": "Duplicate IDs in order"}), 400

        # Bulk fetch all relevant configs
        configs = WebhookConfig.query.filter(WebhookConfig.id.in_(order)).all()
        config_map = {c.id: c for c in configs}

        # Validation: check for unknown IDs
        if len(configs) != len(order):
            return jsonify({"status": "error", "message": "One or more unknown IDs in order"}), 400

        for index, config_id in enumerate(order):
            config = config_map[config_id]
            config.display_order = index

        db.session.commit()
        return jsonify({"status": "success"})

    @main_bp.route("/endpoint/new", methods=["GET", "POST"])
    @auth_required
    def new_endpoint() -> Any:
        if request.method == "POST":
            bearer_token = secrets.token_urlsafe(32)
            config = WebhookConfig(
                name=request.form.get("name"),
                bearer_token=encrypt_string(bearer_token),
                bearer_token_last4=bearer_token[-4:],
                customer_id_default=request.form.get("customer_id_default"),
                board=request.form.get("board"),
                status=request.form.get("status"),
                close_status=request.form.get("close_status"),
                ticket_type=request.form.get("ticket_type"),
                subtype=request.form.get("subtype"),
                item=request.form.get("item"),
                priority=request.form.get("priority"),
                trigger_field=request.form.get("trigger_field") or "heartbeat.status",
                open_value=request.form.get("open_value") or "0",
                close_value=request.form.get("close_value") or "1",
                ticket_prefix=request.form.get("ticket_prefix"),
                description_template=request.form.get("description_template"),
                summary_remove_strings=request.form.get("summary_remove_strings"),
                json_mapping=request.form.get("json_mapping"),
                routing_rules=request.form.get("routing_rules"),
                maintenance_windows=request.form.get("maintenance_windows"),
                trusted_ips=request.form.get("trusted_ips"),
                hmac_secret=request.form.get("hmac_secret") or None,
                is_draft=request.form.get("is_draft") == "true",
                ai_rca_enabled=request.form.get("ai_rca_enabled") == "true",
                bearer_auth_enabled=request.form.get("bearer_auth_enabled") == "true",
                allow_unauthenticated=request.form.get("allow_unauthenticated") == "true",
                global_routing_enabled=request.form.get("global_routing_enabled") == "true",
                auto_link_configuration_enabled=request.form.get("auto_link_configuration_enabled") == "true",
                ai_prompt_template=request.form.get("ai_prompt_template"),
                timeout_alerts_enabled=request.form.get("timeout_alerts_enabled") == "true",
                timeout_hours=_get_int_form_value("timeout_hours", 24),
                notify_failure_threshold=_get_int_form_value("notify_failure_threshold", 0, min_val=0, max_val=1000)
                or None,
                notify_window_minutes=max(5, _get_int_form_value("notify_window_minutes", 60, max_val=1440)),
                rate_limit_per_minute=_get_int_form_value("rate_limit_per_minute", 60, 1, 10000),
                retry_enabled=request.form.get("retry_enabled", "true") == "true",
                retry_max_attempts=_get_int_form_value("retry_max_attempts", 5, 0, 20),
                retry_base_delay_seconds=_get_int_form_value("retry_base_delay_seconds", 1, 1, 3600),
                retry_max_delay_seconds=_get_int_form_value("retry_max_delay_seconds", 300, 1, 86400),
            )
            db.session.add(config)
            if auth_error := _authentication_error(config):
                db.session.rollback()
                return (
                    render_template(
                        "form.html",
                        base_url=request.url_root.rstrip("/"),
                        form_error=auth_error,
                        endpoint_templates=public_endpoint_templates(),
                    ),
                    400,
                )
            try:
                _set_tags(config, request.form.get("tags"))
            except ValueError as exc:
                db.session.rollback()
                return (
                    render_template(
                        "form.html",
                        base_url=request.url_root.rstrip("/"),
                        form_error=str(exc),
                        endpoint_templates=public_endpoint_templates(),
                    ),
                    400,
                )
            db.session.commit()
            log_audit("create", config.id, f"Endpoint {config.name} created")
            flash(f'Endpoint "{config.name}" {"saved as draft" if config.is_draft else "created successfully"}!')

            if request.form.get("create_another") == "true":
                return redirect(url_for("main.new_endpoint", confetti="true"))
            return redirect(url_for("main.webhooks", confetti="true"))
        return render_template(
            "form.html", base_url=request.url_root.rstrip("/"), endpoint_templates=public_endpoint_templates()
        )

    @main_bp.route("/endpoint/edit/<config_id>", methods=["GET", "POST"])
    @auth_required
    def edit_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        if request.method == "POST":
            config.name = request.form.get("name")
            config.customer_id_default = request.form.get("customer_id_default")
            config.board = request.form.get("board")
            config.status = request.form.get("status")
            config.close_status = request.form.get("close_status")
            config.ticket_type = request.form.get("ticket_type")
            config.subtype = request.form.get("subtype")
            config.item = request.form.get("item")
            config.priority = request.form.get("priority")
            config.trigger_field = request.form.get("trigger_field") or "heartbeat.status"
            config.open_value = request.form.get("open_value") or "0"
            config.close_value = request.form.get("close_value") or "1"
            config.ticket_prefix = request.form.get("ticket_prefix")
            config.description_template = request.form.get("description_template")
            config.summary_remove_strings = request.form.get("summary_remove_strings")
            config.json_mapping = request.form.get("json_mapping")
            config.routing_rules = request.form.get("routing_rules")
            config.maintenance_windows = request.form.get("maintenance_windows")
            config.trusted_ips = request.form.get("trusted_ips")
            if request.form.get("hmac_secret"):
                config.hmac_secret = request.form["hmac_secret"]
            config.is_draft = request.form.get("is_draft") == "true"
            config.ai_rca_enabled = request.form.get("ai_rca_enabled") == "true"
            config.bearer_auth_enabled = request.form.get("bearer_auth_enabled") == "true"
            config.allow_unauthenticated = request.form.get("allow_unauthenticated") == "true"
            config.global_routing_enabled = request.form.get("global_routing_enabled") == "true"
            config.auto_link_configuration_enabled = request.form.get("auto_link_configuration_enabled") == "true"
            config.ai_prompt_template = request.form.get("ai_prompt_template")
            config.timeout_alerts_enabled = request.form.get("timeout_alerts_enabled") == "true"
            config.timeout_hours = _get_int_form_value("timeout_hours", 24)
            config.notify_failure_threshold = (
                _get_int_form_value("notify_failure_threshold", 0, min_val=0, max_val=1000) or None
            )
            config.notify_window_minutes = max(5, _get_int_form_value("notify_window_minutes", 60, max_val=1440))
            config.rate_limit_per_minute = _get_int_form_value("rate_limit_per_minute", 60, 1, 10000)
            config.retry_enabled = request.form.get("retry_enabled", "true") == "true"
            config.retry_max_attempts = _get_int_form_value("retry_max_attempts", 5, 0, 20)
            config.retry_base_delay_seconds = _get_int_form_value("retry_base_delay_seconds", 1, 1, 3600)
            config.retry_max_delay_seconds = _get_int_form_value("retry_max_delay_seconds", 300, 1, 86400)
            if config.retry_max_delay_seconds < config.retry_base_delay_seconds:
                config.retry_max_delay_seconds = config.retry_base_delay_seconds

            if auth_error := _authentication_error(config):
                db.session.rollback()
                return (
                    render_template(
                        "form.html",
                        config=config,
                        base_url=request.url_root.rstrip("/"),
                        form_error=auth_error,
                        endpoint_templates=public_endpoint_templates(),
                    ),
                    400,
                )

            try:
                _set_tags(config, request.form.get("tags"))
            except ValueError as exc:
                db.session.rollback()
                return (
                    render_template(
                        "form.html",
                        config=config,
                        base_url=request.url_root.rstrip("/"),
                        form_error=str(exc),
                        endpoint_templates=public_endpoint_templates(),
                    ),
                    400,
                )

            db.session.commit()
            log_audit("update", config.id, f"Endpoint {config.name} updated")
            flash(f'Endpoint "{config.name}" updated successfully!')
            return redirect(url_for("main.webhooks"))
        return render_template(
            "form.html",
            config=config,
            base_url=request.url_root.rstrip("/"),
            endpoint_templates=public_endpoint_templates(),
        )

    @main_bp.route("/endpoint/toggle/<config_id>", methods=["POST"])
    @auth_required
    def toggle_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        if config.archived_at is not None:
            # Archiviert impliziert pausiert; reaktivieren geht nur ueber
            # Wiederherstellen -- sonst ingestiert ein unsichtbarer Endpoint.
            return jsonify({"status": "error", "message": "Endpoint is archived"}), 400
        config.is_enabled = not config.is_enabled
        db.session.commit()
        action = "enable" if config.is_enabled else "disable"
        log_audit(action, config_id, f"Endpoint {config.name} {action}d")
        return jsonify({"status": "success", "is_enabled": config.is_enabled})

    @main_bp.route("/endpoint/rotate-token/<config_id>", methods=["POST"])
    @auth_required
    def rotate_token(config_id: str) -> Any:
        if denied := _operator_denied():
            return denied
        config = WebhookConfig.query.get_or_404(config_id)
        new_token = secrets.token_urlsafe(32)
        config.bearer_token = encrypt_string(new_token)
        config.bearer_token_last4 = new_token[-4:]
        config.last_rotated_at = datetime.now(timezone.utc)
        db.session.commit()
        log_audit("rotate_token", config_id, f"Token for {config.name} rotated")

        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            response = jsonify({"status": "success", "token": new_token})
            response.headers["Cache-Control"] = "no-store"
            return response

        flash(f'Token for "{config.name}" rotated successfully!')
        return redirect(url_for("main.webhooks"))

    @main_bp.route("/endpoint/quick-update/<config_id>", methods=["POST"])
    @auth_required
    def quick_update_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        field = request.json.get("field")
        value = request.json.get("value")

        # Nr. 15 (Inline-Umbenennen): name gehoert in die Whitelist; ein leerer
        # oder ueberlanger Name wird abgewiesen wie ein unbekanntes Feld.
        if field == "name":
            value = (value or "").strip()
            if not value or len(value) > 100:
                return jsonify({"status": "error", "message": "Invalid name"}), 400
        if field in ["name", "board", "priority", "close_status", "status"]:
            alter_wert = getattr(config, field)
            setattr(config, field, value)
            db.session.commit()
            log_audit(
                "quick_update", config_id, f"Endpoint {config.name}: {field} changed from {alter_wert!r} to {value!r}"
            )
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "message": "Invalid field"}), 400

    @main_bp.route("/endpoint/export/<config_id>")
    @auth_required
    def export_endpoint(config_id: str) -> Any:
        """Nr. 13: einen Endpoint als JSON-Datei exportieren.

        Nutzt die CONFIG_FIELDS-Whitelist der Backups, aber OHNE Geheimnisse:
        Backups sind verschluesselt, eine einzelne JSON-Datei waere es nicht.
        bearer_token und hmac_secret bleiben deshalb draussen; der Import
        erzeugt frische Werte.
        """
        from .services.backups import CONFIG_FIELDS

        config = WebhookConfig.query.get_or_404(config_id)
        record = config.to_dict(include_token=False)
        daten = {
            key: value
            for key, value in record.items()
            if key in CONFIG_FIELDS and key not in {"bearer_token", "hmac_secret"}
        }
        daten["tags"] = [tag.name for tag in config.tags]
        dokument = {"format": "hookwise-endpoint", "version": 1, "endpoint": daten}
        antwort = jsonify(dokument)
        dateiname = "".join(c if c.isalnum() or c in "-_" else "-" for c in config.name.lower())
        antwort.headers["Content-Disposition"] = f'attachment; filename="{dateiname or "endpoint"}.hookwise.json"'
        return antwort

    @main_bp.route("/endpoint/import", methods=["POST"])
    @auth_required
    def import_endpoint() -> Any:
        """Nr. 13: Endpoint aus einer Export-Datei anlegen.

        Erzeugt einen NEUEN Endpoint (frische ID, frischer Token), pausiert --
        wer importiert, prueft und aktiviert selbst. Es werden ausschliesslich
        Felder der CONFIG_FIELDS-Whitelist uebernommen; Geheimnisse aus der
        Datei werden ignoriert.
        """
        from .services.backups import CONFIG_FIELDS

        daten = request.get_json(silent=True) or {}
        if daten.get("format") != "hookwise-endpoint" or "endpoint" not in daten:
            return jsonify({"status": "error", "message": "Not a HookWise endpoint file."}), 400
        felder = daten.get("endpoint")
        if not isinstance(felder, dict):
            return jsonify({"status": "error", "message": "Not a HookWise endpoint file."}), 400
        name = str(felder.get("name") or "").strip()
        if not name or len(name) > 100:
            return jsonify({"status": "error", "message": "Invalid name in the file."}), 400
        # Zustand und Geheimnisse kommen nie aus der Datei; jeder uebrige Wert
        # wird gegen die Spaltendefinition geprueft -- eine manipulierte Datei
        # darf weder 500er ausloesen noch Falschtypen einschleusen.
        gesperrt = {"bearer_token", "hmac_secret", "name", "is_enabled", "is_draft", "is_pinned", "tags", "archived_at"}
        spalten = WebhookConfig.__table__.columns  # type: ignore[attr-defined]
        config = WebhookConfig(name=name, is_enabled=False)
        for key, value in felder.items():
            if key not in CONFIG_FIELDS or key in gesperrt:
                continue
            spalte = spalten.get(key)
            if spalte is None:
                continue
            typ = spalte.type
            if isinstance(typ, db.Boolean):
                if not isinstance(value, bool):
                    return jsonify({"status": "error", "message": f"Invalid value for {key}."}), 400
            elif isinstance(typ, db.Integer):
                if isinstance(value, bool) or not isinstance(value, int):
                    return jsonify({"status": "error", "message": f"Invalid value for {key}."}), 400
            elif isinstance(typ, (db.String, db.Text)):
                if value is not None and not isinstance(value, str):
                    return jsonify({"status": "error", "message": f"Invalid value for {key}."}), 400
                laenge = getattr(typ, "length", None)
                if value is not None and laenge and len(value) > laenge:
                    return jsonify({"status": "error", "message": f"Value too long for {key}."}), 400
            else:
                continue
            setattr(config, key, value)
        db.session.add(config)
        db.session.flush()
        # Wie new_endpoint/clone_endpoint: Last4-Hinweis fuer die Token-Suche
        # mitschreiben (der Spalten-Default hat frisch verschluesselt erzeugt).
        config.bearer_token_last4 = decrypt_string(config.bearer_token)[-4:]
        db.session.commit()
        log_audit("import", config.id, f"Endpoint {config.name} imported from JSON")
        return jsonify({"status": "success", "id": config.id, "name": config.name})

    @main_bp.route("/endpoint/archive/<config_id>", methods=["POST"])
    @auth_required
    def archive_endpoint(config_id: str) -> Any:
        """Nr. 12: Archivieren statt Loeschen.

        Setzt archived_at und pausiert den Endpoint -- die bestehenden
        is_enabled-Filter halten ihn damit aus Ingest, Tasks und
        Timeout-Monitor heraus. History und Konfiguration bleiben erhalten;
        der Vorgang ist ueber /endpoint/restore umkehrbar.
        """
        config = WebhookConfig.query.get_or_404(config_id)
        config.archived_at = datetime.now(timezone.utc)
        config.is_enabled = False
        db.session.commit()
        log_audit("archive", config_id, f"Endpoint {config.name} archived")
        flash(f'Endpoint "{config.name}" archived. You can restore it anytime.')
        return redirect(url_for("main.webhooks"))

    @main_bp.route("/endpoint/restore/<config_id>", methods=["POST"])
    @auth_required
    def restore_endpoint(config_id: str) -> Any:
        """Nr. 12: Wiederherstellen aus dem Archiv.

        Der Endpoint bleibt bewusst pausiert -- wer wiederherstellt, soll die
        Konfiguration pruefen und selbst aktivieren, statt dass ein alter
        Endpoint unbemerkt wieder Tickets erzeugt.
        """
        config = WebhookConfig.query.get_or_404(config_id)
        config.archived_at = None
        db.session.commit()
        log_audit("restore", config_id, f"Endpoint {config.name} restored from archive")
        flash(f'Endpoint "{config.name}" restored (paused).')
        return redirect(url_for("main.webhooks"))

    @main_bp.route("/endpoint/clone/<config_id>", methods=["POST"])
    @auth_required
    def clone_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        new_config = WebhookConfig(
            name=f"{config.name} (Copy)",
            customer_id_default=config.customer_id_default,
            board=config.board,
            status=config.status,
            close_status=config.close_status,
            ticket_type=config.ticket_type,
            subtype=config.subtype,
            item=config.item,
            priority=config.priority,
            trigger_field=config.trigger_field,
            open_value=config.open_value,
            close_value=config.close_value,
            ticket_prefix=config.ticket_prefix,
            description_template=config.description_template,
            summary_remove_strings=config.summary_remove_strings,
            json_mapping=config.json_mapping,
            routing_rules=config.routing_rules,
            maintenance_windows=config.maintenance_windows,
            trusted_ips=config.trusted_ips,
            hmac_secret=config.hmac_secret,
            ai_rca_enabled=config.ai_rca_enabled,
            bearer_auth_enabled=config.bearer_auth_enabled,
            allow_unauthenticated=config.allow_unauthenticated,
            global_routing_enabled=config.global_routing_enabled,
            auto_link_configuration_enabled=False,
            ai_prompt_template=config.ai_prompt_template,
            timeout_alerts_enabled=config.timeout_alerts_enabled,
            timeout_hours=config.timeout_hours,
            rate_limit_per_minute=config.rate_limit_per_minute,
            retry_enabled=config.retry_enabled,
            retry_max_attempts=config.retry_max_attempts,
            retry_base_delay_seconds=config.retry_base_delay_seconds,
            retry_max_delay_seconds=config.retry_max_delay_seconds,
        )
        new_token = secrets.token_urlsafe(32)
        new_config.bearer_token = encrypt_string(new_token)
        new_config.bearer_token_last4 = new_token[-4:]
        new_config.tags = list(config.tags)

        db.session.add(new_config)
        db.session.commit()
        log_audit("clone", new_config.id, f"Endpoint {new_config.name} cloned from {config.id}")
        flash(f'Endpoint "{config.name}" cloned successfully!')
        return redirect(url_for("main.webhooks"))

    @main_bp.route("/endpoint/token/<config_id>")
    @auth_required
    def get_endpoint_token(config_id: str) -> Any:
        if denied := _operator_denied():
            return denied
        config = WebhookConfig.query.get_or_404(config_id)
        token = decrypt_string(config.bearer_token)
        if not config.bearer_token_last4:
            config.bearer_token_last4 = token[-4:]
            db.session.commit()
        response = jsonify({"token": token})
        response.headers["Cache-Control"] = "no-store"
        return response

    @main_bp.route("/endpoint/delete/<config_id>", methods=["POST"])
    @auth_required
    def delete_endpoint(config_id: str) -> Any:
        config = WebhookConfig.query.get_or_404(config_id)
        name = config.name
        WebhookLog.query.filter_by(config_id=config_id).delete(synchronize_session=False)
        db.session.delete(config)
        db.session.commit()
        log_audit("delete", config_id, f"Endpoint {name} deleted")
        flash(f'Endpoint "{name}" deleted.')
        return redirect(url_for("main.webhooks"))


def _register_bulk_routes(main_bp: Any) -> None:
    @main_bp.route("/endpoint/bulk/delete", methods=["POST"])
    @auth_required
    def bulk_delete_endpoints() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        WebhookLog.query.filter(WebhookLog.config_id.in_(ids)).delete(synchronize_session=False)
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
        WebhookConfig.query.filter(WebhookConfig.id.in_(ids), WebhookConfig.archived_at.is_(None)).update(
            {"is_enabled": False}, synchronize_session=False
        )
        db.session.commit()
        log_audit("bulk_pause", None, f"Paused endpoints: {', '.join(ids)}")
        return jsonify({"status": "success", "message": f"Paused {len(ids)} endpoints"})

    @main_bp.route("/endpoint/bulk/update", methods=["POST"])
    @auth_required
    def bulk_update_endpoints() -> Any:
        """Nr. 11: Board oder Prioritaet fuer eine Auswahl setzen.

        Gleiche Form wie die uebrigen Bulk-Routen; die Feld-Whitelist ist die
        der Quick-Update-Route (ohne name -- Massen-Umbenennen ergibt keinen
        Sinn und waere ein Fussschuss).
        """
        ids = request.json.get("ids", [])
        field = request.json.get("field")
        value = request.json.get("value")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        if field not in ["board", "priority", "close_status", "status"]:
            return jsonify({"status": "error", "message": "Invalid field"}), 400
        if value is not None and (not isinstance(value, str) or len(value) > 100):
            return jsonify({"status": "error", "message": "Invalid value"}), 400
        betroffen = WebhookConfig.query.filter(WebhookConfig.id.in_(ids)).update(
            {field: value}, synchronize_session=False
        )
        db.session.commit()
        log_audit("bulk_update", None, f"Set {field}={value!r} on {betroffen} endpoints")
        return jsonify({"status": "success", "message": f"Updated {betroffen} endpoints"})

    @main_bp.route("/endpoint/bulk/resume", methods=["POST"])
    @auth_required
    def bulk_resume_endpoints() -> Any:
        ids = request.json.get("ids", [])
        if not ids:
            return jsonify({"status": "error", "message": "No IDs provided"}), 400
        WebhookConfig.query.filter(WebhookConfig.id.in_(ids), WebhookConfig.archived_at.is_(None)).update(
            {"is_enabled": True}, synchronize_session=False
        )
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


def _register() -> None:
    from .routes import main_bp

    _register_crud_routes(main_bp)
    _register_bulk_routes(main_bp)


_register()
