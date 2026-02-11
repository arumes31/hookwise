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
        if 'Cache-Control' not in response.headers:
            if request.path.startswith('/static/'):
                response.headers['Cache-Control'] = 'public, max-age=31536000'
        return response

    # ProxyFix
    if os.environ.get('USE_PROXY') == 'true':
        num_proxies = int(os.environ.get('PROXY_FIX_COUNT', 1))
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=num_proxies, x_proto=num_proxies, x_host=num_proxies, x_port=num_proxies)

    # Request ID middleware
    @app.before_request
    def add_request_id():
        g.request_id = str(uuid.uuid4())

    # Register blueprints
    from .routes import main_bp
    app.register_blueprint(main_bp)

    return app
