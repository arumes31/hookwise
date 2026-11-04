"""Authorisation for the live activity Socket.IO connection."""

import logging
import threading
from typing import Any

from flask import current_app, request, session
from flask_socketio import join_room

from .extensions import LIVE_PAYLOAD_ROOM, LIVE_ROOM, socketio

_logger = logging.getLogger(__name__)


def authorize_live_connection() -> str | None:
    """Return the room this handshake may join, or ``None`` to refuse it.

    The engine.io middleware answers ``/socket.io/`` before Flask routing, so
    neither ``auth_required`` nor the RBAC guard ever sees these requests. The
    same checks are repeated here: trusted IPs, a live (non-revoked) session
    and ``dashboard:read``. Raw payloads additionally need ``history:read``.
    """
    from .rbac.resolver import has_permission
    from .user_sessions import ensure_user_session
    from .utils import client_ip_trusted

    if not client_ip_trusted(request.remote_addr):
        return None
    if "user_id" not in session or not ensure_user_session():
        return None
    if not has_permission("dashboard:read"):
        return None
    return LIVE_PAYLOAD_ROOM if has_permission("history:read") else LIVE_ROOM


#: Re-check interval for established live connections.
REVALIDATE_SECONDS = 30

# Process-local registry: a socket lives on exactly one worker process, so each
# process re-checks the connections it holds.
_connections: dict[str, dict[str, Any]] = {}
_connections_lock = threading.Lock()
_sweeper_started = False


def connection_room(user_id: Any, session_id: Any, *, rbac_mode: str, started_at: Any = None) -> str | None:
    """Room an established connection may stay in, or ``None`` to disconnect it.

    Unlike the handshake this runs without a request: it re-reads the account,
    the session registry and the account's permissions directly, so a revoked
    session, a disabled or deleted account, or a removed permission ends the
    subscription within one sweep interval.
    """
    from .models import User
    from .rbac.resolver import resolve_permissions
    from .user_sessions import session_revoked, session_superseded

    user = User.query.get(user_id) if user_id else None
    if user is None or not user.aktiv:
        return None
    if session_id and session_revoked(str(session_id)):
        return None
    if session_superseded(str(user_id), str(session_id) if session_id else None, started_at):
        return None
    if rbac_mode == "off":
        return LIVE_PAYLOAD_ROOM
    permissions = resolve_permissions(user)
    if not permissions or "dashboard:read" not in permissions:
        return None
    return LIVE_PAYLOAD_ROOM if "history:read" in permissions else LIVE_ROOM


def revalidate_connections(app: Any) -> None:
    """Disconnect or downgrade every registered socket whose access changed."""
    with _connections_lock:
        snapshot = dict(_connections)
    if not snapshot:
        return
    with app.app_context():
        from .extensions import db

        rbac_mode = app.config.get("RBAC_ENFORCE", "on")
        for sid, entry in snapshot.items():
            try:
                room = connection_room(
                    entry["user_id"], entry["session_id"], rbac_mode=rbac_mode, started_at=entry.get("started_at")
                )
            except Exception:
                _logger.exception("Live feed revalidation failed; disconnecting socket")
                room = None
            finally:
                db.session.remove()
            if room is None:
                with _connections_lock:
                    _connections.pop(sid, None)
                socketio.server.disconnect(sid, namespace="/")
            elif room != entry["room"]:
                socketio.server.leave_room(sid, entry["room"], namespace="/")
                socketio.server.enter_room(sid, room, namespace="/")
                with _connections_lock:
                    if sid in _connections:
                        _connections[sid]["room"] = room


def _sweep_forever(app: Any) -> None:
    while True:
        socketio.sleep(REVALIDATE_SECONDS)
        try:
            revalidate_connections(app)
        except Exception:  # pragma: no cover - the sweeper must keep running
            _logger.exception("Live feed revalidation sweep failed")


def _register_connection(room: str) -> None:
    global _sweeper_started
    with _connections_lock:
        _connections[request.sid] = {  # type: ignore[attr-defined]
            "user_id": session.get("user_id"),
            "session_id": session.get("session_id"),
            "started_at": session.get("session_started_at"),
            "room": room,
        }
        start = not _sweeper_started and not current_app.config.get("TESTING")
        if start:
            _sweeper_started = True
    if start:
        socketio.start_background_task(_sweep_forever, current_app._get_current_object())  # type: ignore[attr-defined]


def handle_connect(auth: Any = None) -> bool:
    """Socket.IO ``connect`` handler: join the permitted room or refuse."""
    try:
        room = authorize_live_connection()
    except Exception:
        _logger.exception("Live feed authorisation failed; refusing connection")
        room = None
    if room is None:
        return False
    join_room(room)
    _register_connection(room)
    return True


def handle_disconnect(*_args: Any) -> None:
    """Forget a closed socket."""
    with _connections_lock:
        _connections.pop(getattr(request, "sid", ""), None)
