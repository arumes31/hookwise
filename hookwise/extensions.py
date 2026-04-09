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
    from flask import session

    return "user_id" in session


_socketio_message_queue = build_redis_uri(_redis_password, _redis_host, _redis_port, db=0)

# SocketIO Security: Read allowed origins from env with a safe default for development
_allowed_origins_raw = os.environ.get("SOCKETIO_ALLOWED_ORIGINS")
if _allowed_origins_raw:
    _allowed_origins = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]
else:
    # Safe defaults for local development
    _allowed_origins = ["http://localhost:5000", "http://127.0.0.1:5000"]

socketio = SocketIO(
    cors_allowed_origins=_allowed_origins,
    async_mode=os.environ.get("SOCKETIO_ASYNC_MODE"),
    message_queue=_socketio_message_queue,
)

redis_client: redis.Redis = redis.Redis(
    host=_redis_host,
    port=int(_redis_port),
    db=0,
    password=_redis_password,
)
