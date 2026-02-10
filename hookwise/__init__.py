import os
import uuid
import secrets
from flask import Flask, g, request
from werkzeug.middleware.proxy_fix import ProxyFix
from .extensions import db, socketio

def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///hookwise.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Ensure DB dir exists
    db_url = app.config['SQLALCHEMY_DATABASE_URI']
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if '/' in db_path:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # Initialize extensions
    db.init_app(app)
    socketio.init_app(app)

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
