import json
from typing import Any, Dict, Optional, Union
from datetime import datetime
from jsonpath_ng import parse
from .extensions import socketio

    return val

def resolve_jsonpath(data: Dict[str, Any], path: str) -> Optional[Any]:
    """Resolve a JSONPath expression against the data."""
    try:
        jsonpath_expr = parse(path)
        matches = jsonpath_expr.find(data)
        if matches:
            return matches[0].value
        return None
    except Exception:
        return None

def log_to_web(message: str, level: str = "info", config_name: str = "System", data: Optional[Dict[str, Any]] = None, ticket_id: Optional[int] = None):
    """Helper to send logs to the web GUI via WebSockets."""
    payload_to_send = data
    if isinstance(data, str):
        try:
            payload_to_send = json.loads(data)
        except:
            payload_to_send = {"raw": data}

    socketio.emit('new_log', {
        'timestamp': datetime.utcnow().isoformat() + "Z",
        'message': message,
        'level': level,
        'config_name': config_name,
        'payload': payload_to_send,
        'ticket_id': ticket_id
    })
