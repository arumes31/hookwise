"""Authentication routes: login, logout, 2FA setup/disable."""

import base64
import io
from datetime import datetime, timezone
from typing import Any, cast

import pyotp
import segno
from flask import current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .extensions import db, limiter
from .models import User
from .utils import auth_required, decrypt_string, encrypt_string, log_audit


def _bp() -> Any:
    """Lazy import to avoid circular dependency."""
    from .routes import main_bp

    return main_bp


# We need to register routes after main_bp is created.
# This module is imported at the bottom of routes.py, so main_bp already exists.


def _register_login_routes(bp: Any) -> None:
    @bp.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute", methods=["POST"])
    def login() -> Any:
        # If we are already in the 2FA step (from previous credential check)
        pending_user_id = session.get("pending_user_id")

        if request.method == "POST":
            # Case 1: Submitting OTP (User is in pending state)
            if pending_user_id and "otp" in request.form:
                # Authenticator-Apps zeigen "123 456" -- Leerraum ist kein Fehler.
                otp = (request.form.get("otp") or "").strip().replace(" ", "")
                user = User.query.get(pending_user_id)
                secret_unavailable = bool(user and not user.otp_secret)

                try:
                    otp_secret = decrypt_string(cast(str, user.otp_secret)) if user and user.otp_secret else None
                except ValueError:
                    otp_secret = None
                    secret_unavailable = True
                    current_app.logger.error(
                        "Unable to decrypt the 2FA secret for user %s; verify ENCRYPTION_KEY",
                        user.id if user else "unknown",
                    )

                # valid_window=1 laesst den direkt vorherigen/naechsten Code zu --
                # die Toleranz fuer Tipp-Zeit und leichte Uhrenabweichung.
                if user and otp_secret and otp and pyotp.TOTP(otp_secret).verify(otp, valid_window=1):
                    # Success
                    session["user_id"] = user.id
                    session["username"] = user.username
                    session["role"] = user.role
                    anmeldung_abschliessen(user)
                    session.pop("pending_user_id", None)
                    log_audit("login_2fa", None, f"User {user.username} logged in with 2FA")
                    return redirect(url_for("main.index"))

                if user and secret_unavailable:
                    session.pop("pending_user_id", None)
                    log_audit("login_2fa_secret_error", None, f"Could not decrypt 2FA secret for user {user.id}")
                    flash("Two-factor authentication is unavailable. Contact an administrator.", "danger")
                    return render_template("login.html"), 503

                log_audit("login_2fa_failed", None, f"Failed 2FA attempt for pending user {pending_user_id}")
                flash("Invalid 2FA code", "danger")
                return render_template("login.html", step="2fa")

            # Case 2: Submitting Credentials or restarting flow
            # If attempting to login with new creds, clear old pending state
            if pending_user_id:
                session.pop("pending_user_id", None)

            username = request.form.get("username")
            password = request.form.get("password")

            user = User.query.filter_by(username=username).first()
            if user and check_password_hash(cast(str, user.password_hash), cast(str, password)):
                if user.is_2fa_enabled:
                    session["pending_user_id"] = user.id
                    return render_template("login.html", step="2fa")

                session.clear()
                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                anmeldung_abschliessen(user)
                log_audit("login", None, f"User {username} logged in")
                return redirect(url_for("main.index"))

            log_audit("login_failed", None, f"Failed login attempt for username '{username}'")
            flash("Invalid username or password", "danger")

        # GET request - always reset pending state to ensure clean login flow
        if "pending_user_id" in session:
            session.pop("pending_user_id", None)

        return render_template("login.html")


def _register_2fa_routes(bp: Any) -> None:
    @bp.route("/settings/2fa/setup", methods=["GET", "POST"])
    @auth_required
    def setup_2fa() -> Any:
        user = User.query.get(session["user_id"])
        if user.is_2fa_enabled:
            flash("2FA is already enabled", "info")
            return redirect(url_for("main.settings"))

        if request.method == "POST":
            otp = (request.form.get("otp") or "").strip().replace(" ", "")
            secret = session.get("pending_otp_secret")
            if secret and otp and pyotp.TOTP(cast(str, secret)).verify(otp, valid_window=1):
                user.otp_secret = encrypt_string(secret)
                user.is_2fa_enabled = True
                db.session.commit()
                session.pop("pending_otp_secret")
                log_audit("2fa_enabled", None, f"User {user.username} enabled 2FA")
                flash("2FA has been enabled successfully!", "success")
                return redirect(url_for("main.settings"))
            flash("Invalid 2FA code", "danger")

        # Das pending-Secret ueberlebt Fehlversuche und Seiten-Reloads: die
        # Authenticator-App haelt das zuerst gescannte Secret, also muss die
        # Seite dasselbe weiterzeigen. Vorher rotierte es bei jedem Rendern --
        # nach dem ersten Fehlversuch konnte das Setup nie mehr gelingen.
        secret = session.get("pending_otp_secret") or pyotp.random_base32()
        session["pending_otp_secret"] = secret
        totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="HookWise")

        qr = segno.make(totp_uri)
        out = io.BytesIO()
        qr.save(out, kind="png", scale=5)
        qr_data = f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode()}"

        return render_template("setup_2fa.html", qr_data=qr_data, secret=secret)

    @bp.route("/settings/2fa/disable", methods=["POST"])
    @auth_required
    def disable_2fa() -> Any:
        user = User.query.get(session["user_id"])
        user.is_2fa_enabled = False
        user.otp_secret = None
        db.session.commit()
        log_audit("2fa_disabled", None, f"User {user.username} disabled 2FA")
        flash("2FA has been disabled.", "warning")
        return redirect(url_for("main.settings"))


def _register_logout_routes(bp: Any) -> None:
    @bp.route("/logout")
    def logout() -> Any:
        username = session.get("username")
        session.clear()
        log_audit("logout", None, f"User {username} logged out")
        return redirect(url_for("main.login"))


def _register() -> None:
    from .routes import main_bp

    _register_login_routes(main_bp)
    _register_2fa_routes(main_bp)
    _register_logout_routes(main_bp)


_register()


def anmeldung_abschliessen(user: Any) -> None:
    """Nach erfolgreicher Anmeldung: Bestand nachziehen, Rechte aufloesen.

    Der Backfill laeuft verzoegert pro Nutzer statt als Big-Bang: Wer noch keine
    Rollenzuweisung hat, bekommt beim ersten Login nach dem Rollout genau eine,
    abgeleitet aus dem alten role-Wert.
    """
    from .rbac.resolver import schema_bereit, sitzung_setzen

    try:
        user.last_login_at = datetime.now(timezone.utc)
        if user.auth_source is None:
            user.auth_source = "local"
        if user.is_active is None:
            user.is_active = True
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()

    if schema_bereit():
        try:
            backfill_nutzer(user)
        except Exception:  # pragma: no cover
            db.session.rollback()
    sitzung_setzen(user)


def backfill_nutzer(user: Any) -> bool:
    """Legt fuer einen Nutzer ohne Zuweisung eine aus der Legacy-Rolle an."""
    from .models import RbacRole, RbacUserRole

    if RbacUserRole.query.filter_by(user_id=user.id).first() is not None:
        return False
    schluessel = (user.role or "user").strip().lower()
    ziel = {"user": "operator"}.get(schluessel, schluessel)
    rolle = RbacRole.query.filter_by(key=ziel).first() or RbacRole.query.filter_by(key="viewer").first()
    if rolle is None:
        return False
    db.session.add(RbacUserRole(user_id=user.id, role_id=rolle.id, granted_by="backfill"))
    db.session.commit()
    return True
