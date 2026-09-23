"""Authentication routes: login, logout, 2FA setup/disable."""

import base64
import hashlib
import io
from datetime import datetime, timezone
from typing import Any, cast

import pyotp
import segno
from flask import current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .auth_entra import entra_aktiv
from .extensions import db, limiter
from .models import User
from .utils import auth_required, decrypt_string, encrypt_string, log_audit


def _bp() -> Any:
    """Lazy import to avoid circular dependency."""
    from .routes import main_bp

    return main_bp


def _otp_verbrauchen(user_id: str, otp: str) -> bool | None:
    """Redeem a one-time code, distinguishing replay from store failure.

    ``valid_window=1`` accepts a code for a step before and after the current
    one, so verification alone leaves it usable for up to ninety seconds. A
    code observed in that window -- in a screen recording, a proxy, a support
    session -- would otherwise open a second sign-in. The marker is keyed by a
    digest so the code itself never becomes a Redis key.

    ``True`` means redeemed, ``False`` means already used, and ``None`` means
    the replay store was unavailable. An unreachable store refuses the sign-in.
    That is deliberate: a guard that
    waves everything through when the store is down is no guard, and Redis
    carries the settings, the cache and the session registry anyway, so a
    console that cannot reach it is of little use. The cost is real though --
    while Redis is down, no account with 2FA can sign in.
    """
    from .extensions import redis_client

    abdruck = hashlib.sha256(f"{user_id}:{otp}".encode()).hexdigest()
    try:
        return bool(redis_client.set(f"hookwise:otp_used:{abdruck}", "1", nx=True, ex=180))
    except Exception:
        current_app.logger.exception("OTP replay guard unavailable; refusing the sign-in")
        return None


def _pending_secret_lesen() -> str | None:
    """Read the half-finished TOTP seed back out of the session.

    Flask signs the session cookie but does not encrypt it, so the seed is kept
    encrypted there. A value that will not decrypt -- a plaintext entry from an
    older release, or a rotated ``ENCRYPTION_KEY`` -- is dropped so the setup
    starts over with a fresh QR code instead of failing every code the user
    types.
    """
    gespeichert = session.get("pending_otp_secret")
    if not gespeichert:
        return None
    try:
        return decrypt_string(cast(str, gespeichert))
    except ValueError:
        session.pop("pending_otp_secret", None)
        return None


# We need to register routes after main_bp is created.
# This module is imported at the bottom of routes.py, so main_bp already exists.


def _register_login_routes(bp: Any) -> None:
    """Register local credential and two-factor sign-in routes."""

    @bp.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute", methods=["POST"])
    def login() -> Any:
        """Authenticate a local user and complete an optional TOTP step."""

        # If we are already in the 2FA step (from previous credential check)
        pending_user_id = session.get("pending_user_id")
        entra_ready = entra_aktiv()

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
                # Zwischen Passwort- und Code-Eingabe kann das Konto gesperrt
                # worden sein -- der zweite Schritt prueft deshalb erneut.
                if user and not user.aktiv:
                    session.pop("pending_user_id", None)
                    log_audit("login_denied", None, f"Disabled account {user.username} attempted 2FA")
                    flash("Invalid username or password", "danger")
                    return render_template("login.html", entra_ready=entra_ready)

                if user and otp_secret and otp and pyotp.TOTP(otp_secret).verify(otp, valid_window=1):
                    otp_status = _otp_verbrauchen(str(user.id), otp)
                    if otp_status is None:
                        log_audit(
                            "login_2fa_unavailable",
                            None,
                            f"2FA replay guard unavailable for user {user.username}",
                        )
                        flash("Two-factor authentication is temporarily unavailable. Please try again.", "danger")
                        return render_template("login.html", step="2fa", entra_ready=entra_ready), 503
                    if otp_status is False:
                        log_audit("login_2fa_replay", None, f"Reused 2FA code for user {user.username}")
                        flash("That one-time code has already been used", "danger")
                        return render_template("login.html", step="2fa", entra_ready=entra_ready)

                    # Success
                    session["user_id"] = user.id
                    session["username"] = user.username
                    session["role"] = user.role
                    anmeldung_abschliessen(user)
                    from .user_sessions import start_user_session

                    start_user_session(user)
                    session.pop("pending_user_id", None)
                    log_audit("login_2fa", None, f"User {user.username} logged in with 2FA")
                    return redirect(url_for("main.index"))

                if user and secret_unavailable:
                    session.pop("pending_user_id", None)
                    log_audit("login_2fa_secret_error", None, f"Could not decrypt 2FA secret for user {user.id}")
                    flash("Two-factor authentication is unavailable. Contact an administrator.", "danger")
                    return render_template("login.html", entra_ready=entra_ready), 503

                log_audit("login_2fa_failed", None, f"Failed 2FA attempt for pending user {pending_user_id}")
                flash("Invalid 2FA code", "danger")
                return render_template("login.html", step="2fa", entra_ready=entra_ready)

            # Case 2: Submitting Credentials or restarting flow
            # If attempting to login with new creds, clear old pending state
            if pending_user_id:
                session.pop("pending_user_id", None)

            username = request.form.get("username")
            password = request.form.get("password")

            user = User.query.filter_by(username=username).first()
            if (
                user
                and user.quelle == "local"
                and check_password_hash(cast(str, user.password_hash), cast(str, password))
            ):
                # Ein deaktiviertes Konto authentifiziert sich nicht -- weder in
                # den 2FA-Schritt hinein noch in eine Sitzung. Die Meldung
                # bleibt die allgemeine, um kein Konto zu bestaetigen.
                if not user.aktiv:
                    log_audit("login_denied", None, f"Disabled account {username} attempted sign-in")
                    flash("Invalid username or password", "danger")
                    return render_template("login.html", entra_ready=entra_ready)

                if user.is_2fa_enabled:
                    session["pending_user_id"] = user.id
                    return render_template("login.html", step="2fa")

                session.clear()
                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                anmeldung_abschliessen(user)
                from .user_sessions import start_user_session

                start_user_session(user)
                log_audit("login", None, f"User {username} logged in")
                return redirect(url_for("main.index"))

            log_audit("login_failed", None, f"Failed login attempt for username '{username}'")
            flash("Invalid username or password", "danger")

        # GET request - always reset pending state to ensure clean login flow
        if "pending_user_id" in session:
            session.pop("pending_user_id", None)

        return render_template("login.html", entra_ready=entra_ready)


def _register_2fa_routes(bp: Any) -> None:
    """Register local-account two-factor setup and removal routes."""

    @bp.route("/settings/2fa/setup", methods=["GET", "POST"])
    @auth_required
    def setup_2fa() -> Any:
        """Enroll a local account in TOTP after verifying its first code."""

        user = User.query.get(session["user_id"])
        if user.quelle != "local":
            flash("Two-factor authentication for this account is managed in Microsoft 365.", "info")
            return redirect(url_for("main.account_settings"))
        if user.is_2fa_enabled:
            flash("2FA is already enabled", "info")
            return redirect(url_for("main.account_settings"))

        if request.method == "POST":
            otp = (request.form.get("otp") or "").strip().replace(" ", "")
            secret = _pending_secret_lesen()
            if secret and otp and pyotp.TOTP(secret).verify(otp, valid_window=1):
                user.otp_secret = encrypt_string(secret)
                user.is_2fa_enabled = True
                db.session.commit()
                session.pop("pending_otp_secret", None)
                log_audit("2fa_enabled", None, f"User {user.username} enabled 2FA")
                flash("2FA has been enabled successfully!", "success")
                return redirect(url_for("main.account_settings"))
            flash("Invalid 2FA code", "danger")

        # Das pending-Secret ueberlebt Fehlversuche und Seiten-Reloads: die
        # Authenticator-App haelt das zuerst gescannte Secret, also muss die
        # Seite dasselbe weiterzeigen. Vorher rotierte es bei jedem Rendern --
        # nach dem ersten Fehlversuch konnte das Setup nie mehr gelingen.
        secret = _pending_secret_lesen() or pyotp.random_base32()
        session["pending_otp_secret"] = encrypt_string(secret)
        totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="HookWise")

        qr = segno.make(totp_uri)
        out = io.BytesIO()
        qr.save(out, kind="png", scale=5)
        qr_data = f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode()}"

        return render_template("setup_2fa.html", qr_data=qr_data, secret=secret)

    @bp.route("/settings/2fa/disable", methods=["POST"])
    @limiter.limit("5 per minute")
    @auth_required
    def disable_2fa() -> Any:
        """Disable TOTP for a local account after password confirmation."""

        user = User.query.get(session["user_id"])
        if user.quelle != "local":
            flash("Two-factor authentication for this account is managed in Microsoft 365.", "info")
            return redirect(url_for("main.account_settings"))

        # Den zweiten Faktor abzuschalten wiegt so schwer wie ein Passwortwechsel
        # und verlangt denselben Nachweis. Vorher genuegte die blosse Sitzung --
        # wer eine uebernahm, konnte den Schutz in einem Request entfernen.
        bestaetigung = request.form.get("current_password") or ""
        if not check_password_hash(cast(str, user.password_hash), bestaetigung):
            log_audit("2fa_disable_denied", None, f"Failed confirmation disabling 2FA for {user.username}")
            flash("Enter your current password to disable two-factor authentication.", "danger")
            return redirect(url_for("main.account_settings"))

        user.is_2fa_enabled = False
        user.otp_secret = None
        db.session.commit()
        log_audit("2fa_disabled", None, f"User {user.username} disabled 2FA")
        flash("2FA has been disabled.", "warning")
        return redirect(url_for("main.account_settings"))


def _register_account_routes(bp: Any) -> None:
    @bp.route("/settings/account")
    @auth_required
    def account_settings() -> Any:
        from .user_sessions import list_user_sessions

        user = User.query.get_or_404(session["user_id"])
        return render_template(
            "account_settings.html",
            user=user,
            active_sessions=list_user_sessions(str(user.id)),
        )

    @bp.route("/settings/account/password", methods=["POST"])
    @limiter.limit("5 per minute")
    @auth_required
    def change_own_password() -> Any:
        user = User.query.get_or_404(session["user_id"])
        if user.quelle != "local":
            flash("Password changes for this account are managed in Microsoft 365.", "info")
            return redirect(url_for("main.account_settings"))

        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirmation = request.form.get("confirm_password") or ""
        if not check_password_hash(cast(str, user.password_hash), current_password):
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("main.account_settings"))
        if len(new_password) < 8:
            flash("New password must contain at least 8 characters.", "danger")
            return redirect(url_for("main.account_settings"))
        if new_password != confirmation:
            flash("New password and confirmation do not match.", "danger")
            return redirect(url_for("main.account_settings"))
        if check_password_hash(cast(str, user.password_hash), new_password):
            flash("Choose a password different from your current password.", "danger")
            return redirect(url_for("main.account_settings"))

        from .user_sessions import SessionRevocationError, revoke_other_sessions

        try:
            revoked = revoke_other_sessions(str(user.id), str(session.get("session_id") or ""))
        except SessionRevocationError:
            flash(
                "Password was not changed because other sessions could not be signed out. Try again.",
                "danger",
            )
            return redirect(url_for("main.account_settings"))

        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        log_audit("user_password_change", None, f"User {user.username} changed their password")
        flash(
            f"Password changed. {revoked} other session{'s' if revoked != 1 else ''} signed out.",
            "success",
        )
        return redirect(url_for("main.account_settings"))

    @bp.route("/settings/account/sessions/<session_id>/revoke", methods=["POST"])
    @auth_required
    def revoke_own_session(session_id: str) -> Any:
        from .user_sessions import revoke_user_session

        user_id = str(session["user_id"])
        if session_id == str(session.get("session_id") or ""):
            flash("Use Sign out to end the session on this device.", "info")
        elif revoke_user_session(user_id, session_id):
            log_audit("user_session_revoke", None, f"User {session.get('username')} revoked another session")
            flash("Session signed out.", "success")
        else:
            flash("That session has already ended or could not be signed out.", "warning")
        return redirect(url_for("main.account_settings"))

    @bp.route("/settings/account/sessions/revoke-others", methods=["POST"])
    @auth_required
    def revoke_other_own_sessions() -> Any:
        from .user_sessions import SessionRevocationError, revoke_other_sessions

        try:
            count = revoke_other_sessions(str(session["user_id"]), str(session.get("session_id") or ""))
        except SessionRevocationError:
            flash("Other sessions could not be signed out. Try again.", "danger")
            return redirect(url_for("main.account_settings"))
        log_audit("user_sessions_revoke", None, f"User {session.get('username')} revoked {count} other sessions")
        flash(f"Signed out {count} other session{'s' if count != 1 else ''}.", "success")
        return redirect(url_for("main.account_settings"))


def _register_logout_routes(bp: Any) -> None:
    @bp.route("/logout")
    def logout() -> Any:
        username = session.get("username")
        from .user_sessions import revoke_current_session

        revoke_current_session()
        session.clear()
        log_audit("logout", None, f"User {username} logged out")
        return redirect(url_for("main.login"))


def _register() -> None:
    from .routes import main_bp

    _register_login_routes(main_bp)
    _register_2fa_routes(main_bp)
    _register_account_routes(main_bp)
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
