from typing import Any, Dict, Optional
from datetime import datetime
from .extensions import socketio

def get_nested_value(data: Dict[str, Any], path: str) -> Any:
    """Helper to get value from nested dict using dot notation."""
    keys = path.split('.')
    val = data
    for key in keys:
        if isinstance(val, dict):
            val = val.get(key)
        else:
            return None
    return val

def log_to_web(message: str, level: str = "info", config_name: str = "System", data: Optional[Dict[str, Any]] = None, ticket_id: Optional[int] = None):
    """Helper to send logs to the web GUI via WebSockets."""
    socketio.emit('new_log', {
        'timestamp': datetime.utcnow().isoformat() + "Z",
        'message': message,
        'level': level,
        'config_name': config_name,
        'payload': data,
        'ticket_id': ticket_id
    })
