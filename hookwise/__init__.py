import logging
import os
import secrets
import uuid
from typing import Any, cast

from flask import Flask, Response, g, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import db, limiter, migrate
from .extensions import socketio as socketio

_logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        secret_key = secrets.token_hex(32)
        _logger.critical(
            "SECRET_KEY not set! Sessions will be invalidated on restart. Set SECRET_KEY in your environment."
        )
    app.config["SECRET_KEY"] = secret_key
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
        }
    else:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_recycle": 3600,
        }

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    socketio.init_app(app)

    @app.after_request
    def add_header(response: Response) -> Response:
        # Content Security Policy
        csp = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self' ws: wss:;"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"

        if "Cache-Control" not in response.headers:
            if request.path.startswith("/static/"):
                response.headers["Cache-Control"] = "public, max-age=31536000"
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
        from flask import jsonify

        from .tasks import redis_client

        # Allow /admin and static files during maintenance
        if request.path.startswith("/static/") or request.path.startswith("/admin") or request.path == "/health":
            return

        mode = redis_client.get("hookwise_maintenance_mode")
        if mode and cast(bytes, mode).decode() == "true":
            if request.path.startswith("/w/"):
                return jsonify({"status": "error", "message": "Service under maintenance"}), 503
            return render_template("maintenance.html"), 503

    # Register blueprints
    # Sub-modules (auth, endpoints, webhook, api) are imported at the bottom
    # of routes.py and register their routes directly on main_bp.
    from .routes import main_bp

    app.register_blueprint(main_bp)

    from flask_wtf.csrf import CSRFProtect
    from werkzeug.security import check_password_hash, generate_password_hash

    from .models import User

    CSRFProtect(app)

    with app.app_context():
        try:
            gui_password = os.environ.get("GUI_PASSWORD")
            if not gui_password:
                if os.environ.get("DEBUG_MODE", "false").lower() == "true":
                    gui_password = "admin"
                    _logger.warning("GUI_PASSWORD not set, using default 'admin' for development.")
                else:
                    _logger.critical("GUI_PASSWORD must be set in production!")
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

    @app.errorhandler(404)
    def page_not_found(e: Any) -> Any:
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def internal_server_error(e: Any) -> Any:
        return render_template("500.html"), 500
    
    @app.errorhandler(400)
    def bad_request(e: Any) -> Any:
        return render_template("500.html"), 400

    return app
