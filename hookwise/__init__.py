import logging
import os
import secrets
import uuid
from typing import Any, cast

from flask import Flask, Response, g, jsonify, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import csrf, db, limiter, migrate
from .extensions import socketio as socketio

_logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Application factory for the HookWise application."""
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    _configure_app(app)
    _register_extensions(app)
    _register_request_handlers(app)
    _register_blueprints(app)
    _init_db_data(app)
    _register_error_handlers(app)
    _register_commands(app)

    return app


def _configure_app(app: Flask) -> None:
    """Configure the application with environment variables and defaults."""
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if os.environ.get("DEBUG_MODE", "false").lower() == "true":
            secret_key = secrets.token_hex(32)
            _logger.warning(
                "SECRET_KEY not set! Using a temporary key for development. Sessions will be invalidated on restart."
            )
        else:
            _logger.critical("SECRET_KEY must be set in production!")
            raise RuntimeError("SECRET_KEY env var is required")
    app.config["SECRET_KEY"] = secret_key
    # Tie CSRF token validity to the session lifetime instead of the Flask-WTF
    # default 1-hour cap. Long-lived pages (e.g. the endpoint editor) otherwise
    # accumulate a stale token and POSTs fail with a 400 CSRF error. The token is
    # still bound to the session secret, so CSRF protection is preserved.
    _csrf_ttl = os.environ.get("WTF_CSRF_TIME_LIMIT")
    app.config["WTF_CSRF_TIME_LIMIT"] = int(_csrf_ttl) if _csrf_ttl else None
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "postgresql://hookwise:hookwise_pass@postgres:5432/hookwise"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    if not db_url.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_size": 10,
            "max_overflow": 20,
            "pool_recycle": 3600,
            "pool_pre_ping": True,
        }
    else:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_recycle": 3600,
            "pool_pre_ping": True,
        }


def _register_extensions(app: Flask) -> None:
    """Initialize Flask extensions."""
    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    socketio.init_app(app)
    csrf.init_app(app)


def _register_request_handlers(app: Flask) -> None:
    """Register before and after request handlers and WSGI middleware."""

    @app.after_request
    def add_header(response: Response) -> Response:
        # Content Security Policy
        csp = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws: wss:;"
        )
        response.headers["Content-Security-Policy"] = csp
        if os.environ.get("ENABLE_HSTS", "true").lower() == "true":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"

        if "Cache-Control" not in response.headers:
            if request.path.startswith("/static/"):
                response.headers["Cache-Control"] = "public, max-age=31536000"
            else:
                # Disable caching for all protected routes to prevent "Back" button issues after logout
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
        return response

    @app.before_request
    def force_https() -> Any:
        if os.environ.get("FORCE_HTTPS") == "true":
            if not request.is_secure and request.headers.get("X-Forwarded-Proto", "http") != "https":
                url = request.url.replace("http://", "https://", 1)
                return redirect(url, code=301)

    # ProxyFix
    if os.environ.get("USE_PROXY") == "true":
        num_proxies = int(os.environ.get("PROXY_FIX_COUNT", 1))
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=num_proxies, x_proto=num_proxies, x_host=num_proxies, x_port=num_proxies
        )

    # Request ID middleware
    @app.before_request
    def add_request_id() -> None:
        g.request_id = str(uuid.uuid4())

    @app.before_request
    def check_maintenance() -> Any:
        from .tasks import redis_client

        # Allow /admin, /health*, and static files during maintenance
        if (
            request.path.startswith("/static/")
            or request.path.startswith("/admin")
            or request.path.startswith("/health")
        ):
            return

        mode = redis_client.get("hookwise_maintenance_mode")
        if mode and cast(bytes, mode).decode() == "true":
            if request.path.startswith("/w/"):
                return jsonify({"status": "error", "message": "Service under maintenance"}), 503
            return render_template("maintenance.html"), 503


def _register_blueprints(app: Flask) -> None:
    """Register application blueprints."""
    from .routes import main_bp

    app.register_blueprint(main_bp)


def _init_db_data(app: Flask) -> None:
    """Initialize database with default data (e.g., admin user)."""
    from .models import User

    with app.app_context():
        try:
            gui_password = os.environ.get("GUI_PASSWORD")
            if not gui_password:
                _logger.critical("GUI_PASSWORD must be set!")
                raise RuntimeError("GUI_PASSWORD env var is required")

            admin = User.query.filter_by(username="admin").first()
            if not admin:
                admin = User(username="admin", password_hash=generate_password_hash(gui_password), role="admin")
                db.session.add(admin)
                db.session.commit()
            elif not check_password_hash(admin.password_hash, gui_password):
                # A6: Sync password hash if GUI_PASSWORD env var changed
                admin.password_hash = generate_password_hash(gui_password)
                db.session.commit()
                _logger.info("Admin password hash updated to match GUI_PASSWORD.")
        except Exception:
            db.session.rollback()


def _register_error_handlers(app: Flask) -> None:
    """Register error handlers for common HTTP errors."""

    @app.errorhandler(404)
    def page_not_found(e: Any) -> Any:
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def internal_server_error(e: Any) -> Any:
        return render_template("500.html"), 500

    @app.errorhandler(400)
    def bad_request(e: Any) -> Any:
        if request.path.startswith("/w/") or request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "Bad Request or CSRF Error"}), 400
        return render_template("500.html"), 400

    @app.errorhandler(429)
    def rate_limit_error(e: Any) -> Any:
        return render_template("429.html"), 429


def _register_commands(app: Flask) -> None:
    """Register Flask CLI commands."""
    from .commands import clear_cw_cache_command

    app.cli.add_command(clear_cw_cache_command)
