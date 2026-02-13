"""Authentication routes: login, logout, 2FA setup/disable."""

import base64
import io
from typing import Any, cast

import pyotp
import segno
from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .extensions import db
from .models import User
from .utils import auth_required, log_audit


def _bp() -> Any:
    """Lazy import to avoid circular dependency."""
    from .routes import main_bp

    return main_bp


# We need to register routes after main_bp is created.
# This module is imported at the bottom of routes.py, so main_bp already exists.


def _register() -> None:
    from .routes import main_bp

    @main_bp.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")

            user = User.query.filter_by(username=username).first()
            if user and check_password_hash(cast(str, user.password_hash), cast(str, password)):
                if user.is_2fa_enabled:
                    session["pending_user_id"] = user.id
                    return redirect(url_for("main.login_2fa"))

                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                log_audit("login", None, f"User {username} logged in")
                return redirect(url_for("main.index"))

            flash("Invalid username or password", "danger")
        return render_template("login.html")

    @main_bp.route("/login/2fa", methods=["GET", "POST"])
    def login_2fa() -> Any:
        if "pending_user_id" not in session:
            return redirect(url_for("main.login"))

        if request.method == "POST":
            otp = request.form.get("otp")
            user = User.query.get(session["pending_user_id"])

            if user and pyotp.TOTP(cast(str, user.otp_secret)).verify(cast(str, otp)):
                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                session.pop("pending_user_id")
                log_audit("login_2fa", None, f"User {user.username} logged in with 2FA")
                return redirect(url_for("main.index"))

            flash("Invalid 2FA code", "danger")

        return render_template("login_2fa.html")

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
            if secret and pyotp.TOTP(cast(str, secret)).verify(cast(str, otp)):
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
        totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="HookWise")

        qr = segno.make(totp_uri)
        out = io.BytesIO()
        qr.save(out, kind="png", scale=5)
        qr_data = f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode()}"

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

    @main_bp.route("/logout")
    def logout() -> Any:
        username = session.get("username")
        session.clear()
        log_audit("logout", None, f"User {username} logged out")
        return redirect(url_for("main.login"))


_register()
