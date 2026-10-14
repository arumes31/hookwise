import os
import urllib.parse

import redis
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect


def build_redis_uri(password, host, port, db=0):
    """Securely build a Redis URI with URL-encoded password."""
    quoted_password = urllib.parse.quote(password, safe="") if password else None
    if quoted_password:
        return f"redis://:{quoted_password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


_redis_password = os.environ.get("REDIS_PASSWORD")
_redis_host = os.environ.get("REDIS_HOST", "localhost")
_redis_port = os.environ.get("REDIS_PORT", 6379)
_limiter_storage = os.environ.get("LIMITER_STORAGE_URI")
if not _limiter_storage:
    _limiter_storage = build_redis_uri(_redis_password, _redis_host, _redis_port, db=0)


db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address, storage_uri=_limiter_storage, default_limits=["2000 per day", "500 per hour"]
)


@limiter.request_filter
def header_whitelist():
    """Exempt normal signed-in traffic, but never sensitive account actions."""
    from flask import request, session

    sensitive_endpoints = {
        "main.change_own_password",
        "main.disable_2fa",
    }
    return "user_id" in session and request.endpoint not in sensitive_endpoints


_socketio_message_queue = (
    None
    if os.environ.get("TESTING", "").lower() == "true"
    else build_redis_uri(_redis_password, _redis_host, _redis_port, db=0)
)


def socketio_allowed_origins(raw: str | None) -> str | list[str] | None:
    """Origins allowed to open the live Socket.IO connection.

    Unset or empty means the page's own origin only: python-engineio's default,
    which also honours X-Forwarded-Proto/-Host behind a reverse proxy. A fixed
    localhost list used to be the default and rejected every real deployment.
    """
    value = (raw or "").strip()
    if value == "*":
        return "*"
    origins = [origin.strip() for origin in value.split(",") if origin.strip()]
    return origins or None


socketio = SocketIO(
    cors_allowed_origins=socketio_allowed_origins(os.environ.get("SOCKETIO_ALLOWED_ORIGINS")),
    async_mode=os.environ.get("SOCKETIO_ASYNC_MODE"),
    message_queue=_socketio_message_queue,
)

redis_client: redis.Redis = redis.Redis(
    host=_redis_host,
    port=int(_redis_port),
    db=0,
    password=_redis_password,
)
