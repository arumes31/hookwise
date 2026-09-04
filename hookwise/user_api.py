"""Nutzer-, Rollen- und Identitaetsverwaltung.

Die Aussperr-Regeln stehen hier und nicht in der Oberflaeche: Wer sich selbst
das letzte ``user:manage`` entzieht oder den letzten Administrator deaktiviert,
kommt ohne Datenbankzugriff nicht mehr zurueck in dieses Bild.
"""

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping

from flask import Blueprint, jsonify, render_template, request, session
from werkzeug.security import generate_password_hash

from .auth_entra import entra_aktiv
from .extensions import db
from .models import (
    EventAnnotation,
    RbacRole,
    RbacRolePermission,
    RbacUserRole,
    SavedHistorySearch,
    User,
    UserPreference,
)
from .rbac.catalog import ALL_PERMISSIONS, PERMISSION_GROUPS, is_assignable_start_role
from .rbac.resolver import bump_epoch, schema_bereit
from .utils import auth_required, log_audit

_logger = logging.getLogger(__name__)

ENTRA_AUTO_KEY = "hookwise_entra_auto_provision"
ENTRA_AUTO_ROLE_KEY = "hookwise_entra_auto_provision_role"
ENTRA_GROUP_KEY = "hookwise_entra_group_filter"


# --------------------------------------------------------------------------
# Einstellungen (Redis wie die uebrigen Systemeinstellungen -- kein Schema)
# --------------------------------------------------------------------------
def _redis() -> Any:
    from .extensions import redis_client

    return redis_client


def auto_provision_aktiv() -> bool:
    """Laufzeitschalter; die Umgebungsvariable setzt nur den Startwert."""
    try:
        wert = _redis().get(ENTRA_AUTO_KEY)
    except Exception:  # pragma: no cover - Redis nicht erreichbar
        wert = None
    if wert is None:
        return os.environ.get("ENTRA_AUTO_PROVISION", "false").strip().lower() == "true"
    text = wert.decode() if isinstance(wert, bytes) else str(wert)
    return text.strip().lower() == "true"


def auto_provision_rolle() -> str:
    try:
        wert = _redis().get(ENTRA_AUTO_ROLE_KEY)
    except Exception:  # pragma: no cover
        wert = None
    if wert is None:
        return os.environ.get("ENTRA_AUTO_PROVISION_ROLE", "viewer")
    return (wert.decode() if isinstance(wert, bytes) else str(wert)) or "viewer"


def entra_gruppenfilter() -> str:
    try:
        wert = _redis().get(ENTRA_GROUP_KEY)
    except Exception:  # pragma: no cover
        wert = None
    return (wert.decode() if isinstance(wert, bytes) else str(wert or "")).strip()


# --------------------------------------------------------------------------
# Invarianten
# --------------------------------------------------------------------------
def _rollen_mit(permission: str) -> List[str]:
    return [z.role_id for z in RbacRolePermission.query.filter_by(permission=permission)]


def _nutzer_mit(permission: str) -> List[str]:
    """Aktive Nutzer, die ein Recht ueber eine Rolle halten."""
    rollen = _rollen_mit(permission)
    if not rollen:
        return []
    zuweisungen = RbacUserRole.query.filter(RbacUserRole.role_id.in_(rollen)).all()
    ids = {z.user_id for z in zuweisungen}
    if not ids:
        return []
    return [u.id for u in User.query.filter(User.id.in_(ids)) if u.aktiv]


def _wuerde_aussperren(user_id: str, neue_rollen: List[str] | None = None, deaktivieren: bool = False) -> bool:
    """Bleibt nach der Aenderung noch jemand mit ``user:manage`` uebrig?"""
    halter = set(_nutzer_mit("user:manage"))
    if user_id not in halter:
        return False  # der Nutzer haelt es ohnehin nicht
    if deaktivieren:
        return len(halter) <= 1
    behaelt = False
    if neue_rollen:
        rechte = {z.permission for z in RbacRolePermission.query.filter(RbacRolePermission.role_id.in_(neue_rollen))}
        behaelt = "user:manage" in rechte
    return not behaelt and len(halter) <= 1


def register_user_routes(main_bp: Blueprint, handlers: Mapping[str, Callable[..., Any]] | None = None) -> None:
    """Registriert die Verwaltungs-Routen am Haupt-Blueprint."""

    # ---------------- Ansichten ------------------------------------------
    @main_bp.route("/settings/identity")
    @auth_required
    def identity_settings() -> Any:
        return render_template(
            "identity.html",
            schema_ok=schema_bereit(),
            entra_enabled=os.environ.get("ENTRA_ENABLED", "false").strip().lower() == "true",
            entra_tenant=os.environ.get("ENTRA_TENANT_ID", ""),
            entra_client=os.environ.get("ENTRA_CLIENT_ID", ""),
            entra_redirect=os.environ.get("ENTRA_REDIRECT_URL", ""),
            entra_secret_file=os.environ.get("ENTRA_CLIENT_SECRET_FILE", ""),
            entra_scopes=os.environ.get("ENTRA_SCOPES", "openid profile email"),
            entra_ready=entra_aktiv(),
            auto_provision=auto_provision_aktiv(),
            auto_role=auto_provision_rolle(),
            group_filter=entra_gruppenfilter(),
            permission_groups=PERMISSION_GROUPS,
        )

    # ---------------- Nutzer ---------------------------------------------
    @main_bp.route("/api/users")
    @auth_required
    def users_list() -> Any:
        nutzer = User.query.order_by(User.username).all()
        zuweisungen: Dict[str, List[str]] = {}
        if schema_bereit():
            rollen = {r.id: r.key for r in RbacRole.query.all()}
            for z in RbacUserRole.query.all():
                zuweisungen.setdefault(z.user_id, []).append(rollen.get(z.role_id, "?"))
        return jsonify(
            {
                "users": [dict(u.to_dict(), roles=sorted(zuweisungen.get(u.id, []))) for u in nutzer],
                "schema_ok": schema_bereit(),
            }
        )

    @main_bp.route("/api/users", methods=["POST"])
    @auth_required
    def user_create() -> Any:
        daten = request.get_json(silent=True) or {}
        username = (daten.get("username") or "").strip()
        quelle = (daten.get("auth_source") or "local").strip().lower()
        if not username:
            return jsonify({"status": "error", "message": "username is required"}), 400
        if quelle not in ("local", "entra"):
            return jsonify({"status": "error", "message": "auth_source must be local or entra"}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({"status": "error", "message": "username already exists"}), 409

        # Ein Entra-Konto ohne konfiguriertes Entra kann sich nie anmelden --
        # so ein Datensatz waere nur ein stiller Fehler auf Vorrat.
        if quelle == "entra" and not entra_aktiv():
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Entra ID is not configured. Set up the connection before creating Entra accounts.",
                    }
                ),
                409,
            )

        upn = (daten.get("upn") or "").strip() or None
        if quelle == "entra" and not upn:
            return jsonify({"status": "error", "message": "upn is required for Entra users"}), 400

        # Entra-Konten brauchen kein lokales Passwort; ein Zufallswert verhindert
        # trotzdem eine Anmeldung ueber das Formular.
        passwort = daten.get("password") or secrets.token_urlsafe(32)
        nutzer = User(
            username=username,
            password_hash=generate_password_hash(passwort),
            role=daten.get("role") or "user",
            auth_source=quelle,
            upn=upn,
            is_active=True,
        )
        db.session.add(nutzer)
        db.session.flush()
        _rollen_setzen(nutzer.id, daten.get("roles") or [])
        db.session.commit()
        log_audit("user_create", None, f"User {username} created ({quelle})")
        bump_epoch()
        return jsonify({"status": "success", "user": nutzer.to_dict()}), 201

    @main_bp.route("/api/users/<user_id>", methods=["PATCH"])
    @auth_required
    def user_update(user_id: str) -> Any:
        nutzer = User.query.get_or_404(user_id)
        daten = request.get_json(silent=True) or {}

        if "is_active" in daten:
            aktiv = bool(daten["is_active"])
            if not aktiv and _wuerde_aussperren(user_id, deaktivieren=True):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "This is the last account holding user:manage.",
                        }
                    ),
                    409,
                )
            nutzer.is_active = aktiv
        if "username" in daten:
            name = (daten.get("username") or "").strip()
            if not name:
                return jsonify({"status": "error", "message": "username must not be empty"}), 400
            belegt = User.query.filter(User.username == name, User.id != user_id).first()
            if belegt:
                return jsonify({"status": "error", "message": "username already exists"}), 409
            nutzer.username = name
        if "upn" in daten:
            # Nach der Bindung kommt der UPN von Microsoft und wird bei jeder
            # Anmeldung nachgezogen -- eine Handaenderung waere Selbstbetrug.
            if nutzer.entra_oid:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "This account is bound to Entra; clear the binding to edit the UPN.",
                        }
                    ),
                    409,
                )
            nutzer.upn = (daten.get("upn") or "").strip() or None
        db.session.commit()
        log_audit("user_update", None, f"User {nutzer.username} updated")
        bump_epoch()
        return jsonify({"status": "success", "user": nutzer.to_dict()})

    @main_bp.route("/api/users/<user_id>", methods=["DELETE"])
    @auth_required
    def user_delete(user_id: str) -> Any:
        nutzer = User.query.get_or_404(user_id)
        if session.get("user_id") == user_id:
            return jsonify({"status": "error", "message": "You cannot delete your own account."}), 409
        if _wuerde_aussperren(user_id, deaktivieren=True):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "This is the last account holding user:manage.",
                    }
                ),
                409,
            )
        name = nutzer.username
        # Abhaengige Zeilen explizit raeumen: der DB-Cascade greift in Postgres,
        # aber nicht in jeder Testumgebung.
        RbacUserRole.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        for modell in (UserPreference, SavedHistorySearch, EventAnnotation):
            modell.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        db.session.delete(nutzer)
        db.session.commit()
        log_audit("user_delete", None, f"User {name} deleted")
        bump_epoch()
        return jsonify({"status": "success"})

    @main_bp.route("/api/users/<user_id>/password", methods=["POST"])
    @auth_required
    def user_set_password(user_id: str) -> Any:
        nutzer = User.query.get_or_404(user_id)
        if nutzer.quelle == "entra":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Entra accounts sign in through Microsoft; there is no local password.",
                    }
                ),
                409,
            )
        passwort = (request.get_json(silent=True) or {}).get("password") or ""
        if len(passwort) < 8:
            return jsonify({"status": "error", "message": "Password needs at least 8 characters."}), 400
        nutzer.password_hash = generate_password_hash(passwort)
        db.session.commit()
        log_audit("user_password_reset", None, f"Password reset for {nutzer.username}")
        return jsonify({"status": "success"})

    @main_bp.route("/api/users/<user_id>/mfa", methods=["DELETE"])
    @auth_required
    def user_reset_mfa(user_id: str) -> Any:
        nutzer = User.query.get_or_404(user_id)
        if not nutzer.is_2fa_enabled and not nutzer.otp_secret:
            return jsonify({"status": "error", "message": "No MFA is set up for this account."}), 409
        nutzer.otp_secret = None
        nutzer.is_2fa_enabled = False
        db.session.commit()
        log_audit("user_mfa_reset", None, f"MFA reset for {nutzer.username}")
        return jsonify({"status": "success"})

    @main_bp.route("/api/users/<user_id>/roles", methods=["PUT"])
    @auth_required
    def user_roles_set(user_id: str) -> Any:
        if not schema_bereit():
            return jsonify({"status": "error", "message": "RBAC schema not ready"}), 503
        nutzer = User.query.get_or_404(user_id)
        gewuenscht = (request.get_json(silent=True) or {}).get("roles") or []
        rollen = RbacRole.query.filter(RbacRole.key.in_(gewuenscht)).all()
        if len(rollen) != len(set(gewuenscht)):
            return jsonify({"status": "error", "message": "unknown role"}), 400
        if _wuerde_aussperren(user_id, [r.id for r in rollen]):
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "This is the last account holding user:manage.",
                    }
                ),
                409,
            )
        _rollen_setzen(user_id, gewuenscht)
        db.session.commit()
        log_audit("role_grant", None, f"{nutzer.username} -> {', '.join(sorted(gewuenscht)) or 'none'}")
        bump_epoch()
        return jsonify({"status": "success", "roles": sorted(gewuenscht)})

    @main_bp.route("/api/users/<user_id>/entra-binding", methods=["DELETE"])
    @auth_required
    def user_reset_entra(user_id: str) -> Any:
        nutzer = User.query.get_or_404(user_id)
        nutzer.entra_tid = None
        nutzer.entra_oid = None
        db.session.commit()
        log_audit("entra_binding_reset", None, f"Entra binding reset for {nutzer.username}")
        return jsonify({"status": "success"})

    # ---------------- Rollen ---------------------------------------------
    @main_bp.route("/api/roles")
    @auth_required
    def roles_list() -> Any:
        if not schema_bereit():
            return jsonify({"roles": [], "schema_ok": False})
        rollen = RbacRole.query.order_by(RbacRole.is_builtin.desc(), RbacRole.key).all()
        zaehler: Dict[str, int] = {}
        for z in RbacUserRole.query.all():
            zaehler[z.role_id] = zaehler.get(z.role_id, 0) + 1
        return jsonify(
            {
                "roles": [dict(r.to_dict(), user_count=zaehler.get(r.id, 0)) for r in rollen],
                "schema_ok": True,
            }
        )

    @main_bp.route("/api/roles", methods=["POST"])
    @auth_required
    def role_create() -> Any:
        if not schema_bereit():
            return jsonify({"status": "error", "message": "RBAC schema not ready"}), 503
        daten = request.get_json(silent=True) or {}
        schluessel = (daten.get("key") or "").strip().lower()
        if not schluessel:
            return jsonify({"status": "error", "message": "key is required"}), 400
        if RbacRole.query.filter_by(key=schluessel).first():
            return jsonify({"status": "error", "message": "role already exists"}), 409
        unbekannt = set(daten.get("permissions") or []) - ALL_PERMISSIONS
        if unbekannt:
            return jsonify({"status": "error", "message": f"unknown permissions: {sorted(unbekannt)}"}), 400

        rolle = RbacRole(
            key=schluessel,
            name=(daten.get("name") or schluessel).strip(),
            description=(daten.get("description") or "").strip() or None,
            is_builtin=False,
        )
        db.session.add(rolle)
        db.session.flush()
        for recht in sorted(set(daten.get("permissions") or [])):
            db.session.add(RbacRolePermission(role_id=rolle.id, permission=recht))
        db.session.commit()
        log_audit("role_create", None, f"Role {schluessel} created")
        bump_epoch()
        return jsonify({"status": "success", "role": rolle.to_dict()}), 201

    @main_bp.route("/api/roles/<role_id>", methods=["PATCH"])
    @auth_required
    def role_update(role_id: str) -> Any:
        rolle = RbacRole.query.get_or_404(role_id)
        if rolle.is_builtin:
            return jsonify({"status": "error", "message": "built-in roles are read-only"}), 409
        daten = request.get_json(silent=True) or {}
        if "name" in daten:
            rolle.name = (daten.get("name") or rolle.key).strip()
        if "description" in daten:
            rolle.description = (daten.get("description") or "").strip() or None
        if "permissions" in daten:
            neu = set(daten.get("permissions") or [])
            unbekannt = neu - ALL_PERMISSIONS
            if unbekannt:
                return jsonify({"status": "error", "message": f"unknown permissions: {sorted(unbekannt)}"}), 400
            if "user:manage" not in neu:
                betroffen = [z.user_id for z in RbacUserRole.query.filter_by(role_id=role_id)]
                halter = set(_nutzer_mit("user:manage"))
                if halter and halter <= set(betroffen):
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "This role holds the last user:manage assignment.",
                            }
                        ),
                        409,
                    )
            RbacRolePermission.query.filter_by(role_id=role_id).delete(synchronize_session=False)
            for recht in sorted(neu):
                db.session.add(RbacRolePermission(role_id=role_id, permission=recht))
        db.session.commit()
        log_audit("role_update", None, f"Role {rolle.key} updated")
        bump_epoch()
        return jsonify({"status": "success", "role": rolle.to_dict()})

    @main_bp.route("/api/roles/<role_id>", methods=["DELETE"])
    @auth_required
    def role_delete(role_id: str) -> Any:
        rolle = RbacRole.query.get_or_404(role_id)
        if rolle.is_builtin:
            return jsonify({"status": "error", "message": "built-in roles cannot be deleted"}), 409
        zuweisungen = RbacUserRole.query.filter_by(role_id=role_id).count()
        if zuweisungen:
            return jsonify({"status": "error", "message": f"{zuweisungen} users still hold this role"}), 409
        RbacRolePermission.query.filter_by(role_id=role_id).delete(synchronize_session=False)
        db.session.delete(rolle)
        db.session.commit()
        log_audit("role_delete", None, f"Role {rolle.key} deleted")
        bump_epoch()
        return jsonify({"status": "success"})

    @main_bp.route("/api/permissions")
    @auth_required
    def permission_matrix() -> Any:
        return jsonify(
            {
                "groups": [
                    {"name": name, "permissions": [{"key": k, "text": t} for k, t in rechte]}
                    for name, rechte in PERMISSION_GROUPS
                ],
                "schema_ok": schema_bereit(),
            }
        )

    # ---------------- Entra-Einstellungen --------------------------------
    @main_bp.route("/api/entra/settings", methods=["POST"])
    @auth_required
    def entra_settings_update() -> Any:
        """Der Provisionierungs-Schalter. Redis wie die uebrigen Einstellungen,
        damit er ohne Neustart und ohne Schemaaenderung wirkt (ADR-002)."""
        daten = request.get_json(silent=True) or {}
        auto = bool(daten.get("auto_provision"))
        rolle_key = (daten.get("auto_role") or "viewer").strip().lower()

        if auto:
            rolle = RbacRole.query.filter_by(key=rolle_key).first() if schema_bereit() else None
            if rolle is None:
                return jsonify({"status": "error", "message": "unknown start role"}), 400
            rechte = frozenset(z.permission for z in RbacRolePermission.query.filter_by(role_id=rolle.id))
            if not is_assignable_start_role(rechte):
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Start role must not hold privileged permissions.",
                        }
                    ),
                    400,
                )

        try:
            _redis().set(ENTRA_AUTO_KEY, "true" if auto else "false")
            _redis().set(ENTRA_AUTO_ROLE_KEY, rolle_key)
            _redis().set(ENTRA_GROUP_KEY, (daten.get("group_filter") or "").strip())
        except Exception:  # pragma: no cover
            _logger.exception("Entra-Einstellungen konnten nicht gespeichert werden")
            return jsonify({"status": "error", "message": "settings store unavailable"}), 503

        log_audit(
            "entra_settings_update",
            None,
            f"auto_provision={'on' if auto else 'off'} role={rolle_key}",
        )
        return jsonify({"status": "success", "auto_provision": auto, "auto_role": rolle_key})


def _rollen_setzen(user_id: str, rollen_keys: List[str]) -> None:
    """Ersetzt die Zuweisungen eines Nutzers."""
    if not schema_bereit():
        return
    RbacUserRole.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    if not rollen_keys:
        return
    for rolle in RbacRole.query.filter(RbacRole.key.in_(rollen_keys)):
        db.session.add(
            RbacUserRole(
                user_id=user_id,
                role_id=rolle.id,
                granted_by=session.get("username", "system"),
                granted_at=datetime.now(timezone.utc),
            )
        )
