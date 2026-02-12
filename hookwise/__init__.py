import os
import secrets
import uuid

from flask import Flask, g, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import db, limiter, migrate, socketio


def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'postgresql://hookwise:hookwise_pass@postgres:5432/hookwise')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 3600,
    }

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    socketio.init_app(app)

    @app.after_request
    def add_header(response):
        # Content Security Policy
        csp = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws: wss:;"
        response.headers['Content-Security-Policy'] = csp
        
        if 'Cache-Control' not in response.headers:
            if request.path.startswith('/static/'):
                response.headers['Cache-Control'] = 'public, max-age=31536000'
        return response

    @app.before_request
    def force_https():
        if os.environ.get('FORCE_HTTPS') == 'true':
            if not request.is_secure and request.headers.get('X-Forwarded-Proto', 'http') != 'https':
                url = request.url.replace('http://', 'https://', 1)
                return redirect(url, code=301)

    # ProxyFix
    if os.environ.get('USE_PROXY') == 'true':
        num_proxies = int(os.environ.get('PROXY_FIX_COUNT', 1))
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=num_proxies, x_proto=num_proxies, x_host=num_proxies, x_port=num_proxies)

    # Request ID middleware
    @app.before_request
    def add_request_id():
        g.request_id = str(uuid.uuid4())

    @app.before_request
    def check_maintenance():
        from .tasks import redis_client
        from flask import jsonify
        # Allow /admin and static files during maintenance
        if request.path.startswith('/static/') or request.path.startswith('/admin') or request.path == '/health':
            return
        
        mode = redis_client.get('hookwise_maintenance_mode')
        if mode and mode.decode() == 'true':
            if request.path.startswith('/w/'):
                return jsonify({"status": "error", "message": "Service under maintenance"}), 503
            return render_template('maintenance.html'), 503

    # Register blueprints
    # Sub-modules (auth, endpoints, webhook, api) are imported at the bottom
    # of routes.py and register their routes directly on main_bp.
    from .routes import main_bp
    app.register_blueprint(main_bp)

    from .models import User
    from werkzeug.security import generate_password_hash
    with app.app_context():
        try:
            if not User.query.filter_by(username='admin').first():
                admin = User(
                    username='admin',
                    password_hash=generate_password_hash(os.environ.get('GUI_PASSWORD', 'admin')),
                    role='admin'
                )
                db.session.add(admin)
                db.session.commit()
        except Exception:
            db.session.rollback()

    from flask import render_template

    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def internal_server_error(e):
        return render_template('500.html'), 500

    return app
