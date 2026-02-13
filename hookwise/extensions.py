import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from typing import cast

_redis_password = os.environ.get("REDIS_PASSWORD")
_redis_host = os.environ.get("REDIS_HOST", "localhost")
_redis_port = os.environ.get("REDIS_PORT", 6379)
_limiter_storage = (
    f"redis://:{_redis_password}@{_redis_host}:{_redis_port}/0"
    if _redis_password
    else f"redis://{_redis_host}:{_redis_port}/0"
)


db = SQLAlchemy()
migrate = Migrate()
limiter = Limiter(
    key_func=get_remote_address, storage_uri=_limiter_storage, default_limits=["2000 per day", "500 per hour"]
)
socketio = SocketIO(cors_allowed_origins="*", async_mode=os.environ.get("SOCKETIO_ASYNC_MODE"))
