"""Authorisation for the live activity Socket.IO connection."""

import logging
from typing import Any

from flask import request, session
from flask_socketio import join_room

from .extensions import LIVE_PAYLOAD_ROOM, LIVE_ROOM

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
    return True
