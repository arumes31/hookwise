"""Server-side registry for user-visible and revocable browser sessions."""

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from flask import current_app, request, session

_SESSION_PREFIX = "hookwise:user-session:"
_USER_SET_PREFIX = "hookwise:user-sessions:"
_REVOKED_PREFIX = "hookwise:revoked-session:"
_SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
_TOUCH_INTERVAL_SECONDS = 60


class SessionRevocationError(RuntimeError):
    """Raised when the session registry cannot prove a requested revocation."""


def _redis() -> Any:
    """Resolve the shared client lazily so tests can replace it safely."""
    from .extensions import redis_client

    return redis_client


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _session_key(session_id: str) -> str:
    return f"{_SESSION_PREFIX}{session_id}"


def _user_set_key(user_id: str) -> str:
    return f"{_USER_SET_PREFIX}{user_id}"


def _revoked_key(session_id: str) -> str:
    return f"{_REVOKED_PREFIX}{session_id}"


def _request_metadata() -> dict[str, str]:
    """Return bounded, display-only metadata for the current browser."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    ip_address = forwarded or request.remote_addr or "Unknown"
    user_agent = (request.user_agent.string or "Unknown browser")[:300]
    return {"ip_address": ip_address[:64], "user_agent": user_agent}


def _write(record: dict[str, Any]) -> None:
    client = _redis()
    session_id = str(record["id"])
    user_id = str(record["user_id"])
    payload = json.dumps(record, separators=(",", ":"))
    pipeline = client.pipeline()
    pipeline.setex(_session_key(session_id), _SESSION_TTL_SECONDS, payload)
    pipeline.sadd(_user_set_key(user_id), session_id)
    pipeline.expire(_user_set_key(user_id), _SESSION_TTL_SECONDS)
    pipeline.execute()


def start_user_session(user: Any) -> str:
    """Register a newly authenticated browser session and return its ID."""
    session_id = secrets.token_urlsafe(32)
    timestamp = _now().isoformat()
    record: dict[str, Any] = {
        "id": session_id,
        "user_id": str(user.id),
        "username": str(user.username),
        "auth_source": str(getattr(user, "quelle", None) or getattr(user, "auth_source", "local") or "local"),
        "created_at": timestamp,
        "last_seen_at": timestamp,
        **_request_metadata(),
    }
    session["session_id"] = session_id
    session["auth_source"] = record["auth_source"]
    try:
        client = _redis()
        client.delete(_revoked_key(session_id))
        _write(record)
    except Exception:
        current_app.logger.exception("Unable to register the authenticated user session")
    return session_id


def ensure_user_session() -> bool:
    """Validate revocation state, backfill legacy sessions, and refresh activity."""
    user_id = session.get("user_id")
    if not user_id:
        return False

    session_id = session.get("session_id")
    if not session_id:
        from .models import User

        user = User.query.get(user_id)
        if user is not None:
            start_user_session(user)
        return True

    try:
        client = _redis()
        if client.get(_revoked_key(str(session_id))):
            return False

        raw = client.get(_session_key(str(session_id)))
        if raw is None:
            from .models import User

            user = User.query.get(user_id)
            if user is not None:
                start_user_session(user)
            return True

        record = json.loads(_decode(raw))
        if str(record.get("user_id")) != str(user_id):
            return False

        last_seen = datetime.fromisoformat(str(record["last_seen_at"]))
        if (_now() - last_seen).total_seconds() >= _TOUCH_INTERVAL_SECONDS:
            record["last_seen_at"] = _now().isoformat()
            record.update(_request_metadata())
            _write(record)
    except Exception:
        # Redis backs revocation and presentation, but an outage must not turn
        # into a global authentication outage for already signed-in users.
        current_app.logger.exception("Unable to validate the user session registry")
    return True


def list_user_sessions(user_id: str) -> list[dict[str, Any]]:
    """List live sessions for one owner and remove stale set members."""
    records: list[dict[str, Any]] = []
    current_id = str(session.get("session_id") or "")
    try:
        client = _redis()
        members = client.smembers(_user_set_key(user_id)) or set()
        for raw_id in members:
            session_id = _decode(raw_id)
            raw = client.get(_session_key(session_id))
            if raw is None:
                client.srem(_user_set_key(user_id), session_id)
                continue
            record = json.loads(_decode(raw))
            if str(record.get("user_id")) != str(user_id):
                continue
            record["current"] = session_id == current_id
            records.append(record)
    except Exception:
        current_app.logger.exception("Unable to list active user sessions")

    if current_id and not any(record.get("current") for record in records):
        timestamp = _now().isoformat()
        records.append(
            {
                "id": current_id,
                "user_id": str(user_id),
                "username": str(session.get("username") or ""),
                "auth_source": str(session.get("auth_source") or "local"),
                "created_at": timestamp,
                "last_seen_at": timestamp,
                "current": True,
                **_request_metadata(),
            }
        )
    return sorted(records, key=lambda record: (not bool(record.get("current")), str(record.get("created_at", ""))))


def revoke_user_session(user_id: str, session_id: str) -> bool:
    """Revoke one session only when it belongs to the requested owner."""
    try:
        client = _redis()
        raw = client.get(_session_key(session_id))
        if raw is None:
            return False
        record = json.loads(_decode(raw))
        if str(record.get("user_id")) != str(user_id):
            return False
        pipeline = client.pipeline()
        pipeline.setex(_revoked_key(session_id), _SESSION_TTL_SECONDS, "1")
        pipeline.delete(_session_key(session_id))
        pipeline.srem(_user_set_key(user_id), session_id)
        pipeline.execute()
        return True
    except Exception:
        current_app.logger.exception("Unable to revoke user session")
        return False


def revoke_other_sessions(user_id: str, current_session_id: str) -> int:
    """Revoke and verify every registered session except the current one.

    Unlike the presentation-oriented session listing, this security-sensitive
    path must surface Redis and decoding failures to its caller.
    """
    try:
        client = _redis()
        user_key = _user_set_key(user_id)
        members = client.smembers(user_key) or set()
        targets: list[str] = []
        stale: list[str] = []

        for raw_id in members:
            session_id = _decode(raw_id)
            if session_id == current_session_id:
                continue
            raw = client.get(_session_key(session_id))
            if raw is None:
                stale.append(session_id)
                continue
            record = json.loads(_decode(raw))
            if str(record.get("user_id")) != str(user_id):
                raise SessionRevocationError("Session registry ownership mismatch")
            targets.append(session_id)

        if not targets and not stale:
            return 0

        pipeline = client.pipeline()
        for session_id in stale:
            pipeline.srem(user_key, session_id)
        for session_id in targets:
            pipeline.setex(_revoked_key(session_id), _SESSION_TTL_SECONDS, "1")
            pipeline.delete(_session_key(session_id))
            pipeline.srem(user_key, session_id)
        pipeline.execute()

        remaining = {_decode(raw_id) for raw_id in (client.smembers(user_key) or set())}
        for session_id in targets:
            if (
                client.get(_session_key(session_id)) is not None
                or not client.get(_revoked_key(session_id))
                or session_id in remaining
            ):
                raise SessionRevocationError("Session revocation could not be verified")
        if any(session_id in remaining for session_id in stale):
            raise SessionRevocationError("Stale session registry entries could not be removed")
        return len(targets)
    except SessionRevocationError:
        current_app.logger.exception("Unable to revoke all other user sessions")
        raise
    except Exception as exc:
        current_app.logger.exception("Unable to revoke all other user sessions")
        raise SessionRevocationError("Session registry is unavailable") from exc


def revoke_current_session() -> None:
    """Best-effort removal of the current browser session during logout."""
    user_id = session.get("user_id")
    session_id = session.get("session_id")
    if user_id and session_id:
        revoke_user_session(str(user_id), str(session_id))
