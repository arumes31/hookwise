"""Configuration backup and restore routes."""

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from .extensions import db
from .services.backups import MAX_BACKUP_BYTES, BackupValidationError, export_backup, parse_backup, restore_backup
from .utils import auth_required, log_audit


def register_backup_routes(blueprint: Blueprint, handlers: Mapping[str, Callable[..., Any]]) -> None:
    @auth_required
    def backup_config() -> Response:
        payload = export_backup()
        return Response(
            payload,
            mimetype="application/vnd.hookwise.backup",
            headers={"Content-Disposition": "attachment;filename=hookwise-config.hwbackup"},
        )

    @auth_required
    def restore_config() -> Any:
        uploaded = request.files.get("backup_file")
        if uploaded is None:
            return jsonify({"status": "error", "message": "No file"}), 400
        try:
            count = restore_backup(parse_backup(uploaded.read(MAX_BACKUP_BYTES + 1)))
            log_audit("restore_config", None, f"Restored {count} endpoint configurations")
            return jsonify({"status": "success", "restored": count})
        except BackupValidationError as exc:
            db.session.rollback()
            current_app.logger.warning("Rejected configuration backup: %s", exc)
            return jsonify({"status": "error", "message": "Backup validation failed"}), 400
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to import configuration")
            return jsonify({"status": "error", "message": "Configuration import failed"}), 500

    blueprint.add_url_rule("/admin/backup", endpoint="backup_config", view_func=backup_config, methods=["GET"])
    blueprint.add_url_rule("/admin/restore", endpoint="restore_config", view_func=restore_config, methods=["POST"])
