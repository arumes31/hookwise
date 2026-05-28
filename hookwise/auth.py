"""Authentication routes: login, logout, 2FA setup/disable."""

import base64
import io
from typing import Any, cast

import pyotp
import segno
from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .extensions import db, limiter
from .models import User
from .utils import auth_required, log_audit


def _bp() -> Any:
    """Lazy import to avoid circular dependency."""
    from .routes import main_bp

    return main_bp


# We need to register routes after main_bp is created.
# This module is imported at the bottom of routes.py, so main_bp already exists.


def _verify_credentials(username: str | None, password: str | None) -> User | None:
    """Verify user credentials and return the User object if successful."""
    if not username or not password:
        return None
    user = User.query.filter_by(username=username).first()
    if user and check_password_hash(cast(str, user.password_hash), cast(str, password)):
        return user
    return None


def _verify_otp(otp: str | None, secret: str | None) -> bool:
    """Verify the provided OTP against the secret."""
    if not otp or not secret:
        return False
    return pyotp.TOTP(cast(str, secret)).verify(cast(str, otp))


def _generate_qr_data(username: str, secret: str) -> str:
    """Generate a base64-encoded QR code for 2FA setup."""
    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name="HookWise")
    qr = segno.make(totp_uri)
    out = io.BytesIO()
    qr.save(out, kind="png", scale=5)
    return f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode()}"


def _register_login_routes(main_bp: Any) -> None:
    @main_bp.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per minute")
    def login() -> Any:
        # If we are already in the 2FA step (from previous credential check)
        pending_user_id = session.get("pending_user_id")

        if request.method == "POST":
            # Case 1: Submitting OTP (User is in pending state)
            if pending_user_id and "otp" in request.form:
                otp = request.form.get("otp")
                user = User.query.get(pending_user_id)

                if user and _verify_otp(otp, user.otp_secret):
                    # Success
                    session["user_id"] = user.id
                    session["username"] = user.username
                    session["role"] = user.role
                    session.pop("pending_user_id", None)
                    log_audit("login_2fa", None, f"User {user.username} logged in with 2FA")
                    return redirect(url_for("main.index"))

                flash("Invalid 2FA code", "danger")
                return render_template("login.html", step="2fa")

            # Case 2: Submitting Credentials or restarting flow
            # If attempting to login with new creds, clear old pending state
            if pending_user_id:
                session.pop("pending_user_id", None)

            username = request.form.get("username")
            password = request.form.get("password")

            user = _verify_credentials(username, password)
            if user:
                if user.is_2fa_enabled:
                    session["pending_user_id"] = user.id
                    return render_template("login.html", step="2fa")

                session.clear()
                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                log_audit("login", None, f"User {username} logged in")
                return redirect(url_for("main.index"))

            flash("Invalid username or password", "danger")

        # GET request - always reset pending state to ensure clean login flow
        if "pending_user_id" in session:
            session.pop("pending_user_id", None)

        return render_template("login.html")


def _register_2fa_routes(main_bp: Any) -> None:
    @main_bp.route("/settings/2fa/setup", methods=["GET", "POST"])
    @auth_required
    def setup_2fa() -> Any:
        user = User.query.get(session["user_id"])
        if user.is_2fa_enabled:
            flash("2FA is already enabled", "info")
            return redirect(url_for("main.settings"))

        if request.method == "POST":
            otp = request.form.get("otp")
            secret = session.get("pending_otp_secret")
            if _verify_otp(otp, secret):
                user.otp_secret = secret
                user.is_2fa_enabled = True
                db.session.commit()
                session.pop("pending_otp_secret")
                log_audit("2fa_enabled", None, f"User {user.username} enabled 2FA")
                flash("2FA has been enabled successfully!", "success")
                return redirect(url_for("main.settings"))
            flash("Invalid 2FA code", "danger")

        # GET: Generate secret and QR code
        secret = pyotp.random_base32()
        session["pending_otp_secret"] = secret
        qr_data = _generate_qr_data(user.username, secret)

        return render_template("setup_2fa.html", qr_data=qr_data, secret=secret)

    @main_bp.route("/settings/2fa/disable", methods=["POST"])
    @auth_required
    def disable_2fa() -> Any:
        user = User.query.get(session["user_id"])
        user.is_2fa_enabled = False
        user.otp_secret = None
        db.session.commit()
        log_audit("2fa_disabled", None, f"User {user.username} disabled 2FA")
        flash("2FA has been disabled.", "warning")
        return redirect(url_for("main.settings"))


def _register_logout_routes(main_bp: Any) -> None:
    @main_bp.route("/logout")
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
