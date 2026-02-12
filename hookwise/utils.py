import json
import logging
import os
import requests
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Optional

from flask import Response, request
from jsonpath_ng import parse

from .extensions import socketio

logger = logging.getLogger(__name__)

def call_llm(prompt: str, system_prompt: str = "You are a helpful assistant specialized in ConnectWise ticketing and alert analysis. Be concise and return only the requested value.") -> Optional[str]:
    ollama_host = os.environ.get('OLLAMA_HOST', 'http://hookwise-llm:11434')
    try:
        response = requests.post(
            f"{ollama_host}/api/generate",
            json={
                "model": "phi3",
                "prompt": prompt,
                "system": system_prompt,
                "stream": False,
                "options": {
                    "num_predict": 100,
                    "temperature": 0.1
                }
            },
            timeout=90
        )
        response.raise_for_status()
        return response.json().get('response', '').strip()
    except Exception as e:
        logger.error(f"Error calling LLM: {e}")
        return None

def check_auth(username, password):
    """Check if a username/password combination is valid."""
    expected_username = os.environ.get('GUI_USERNAME')
    expected_password = os.environ.get('GUI_PASSWORD')
    if not expected_username or not expected_password:
        return True # Auth disabled if not set
    return username == expected_username and password == expected_password

def authenticate():
    """Sends a 401 response that enables basic auth."""
    return Response(
    'Could not verify your access level for that URL.\n'
    'You have to login with proper credentials', 401,
    {'WWW-Authenticate': 'Basic realm="Login Required"'})

import ipaddress

from flask import Response, request, session, redirect, url_for

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. IP Whitelist Check (Global)
        trusted_ips = os.environ.get('GUI_TRUSTED_IPS')
        if trusted_ips:
            client_ip = request.remote_addr
            trusted = False
            for trusted_range in [ip.strip() for ip in trusted_ips.split(',')]:
                try:
                    if ipaddress.ip_address(client_ip) in ipaddress.ip_network(trusted_range):
                        trusted = True
                        break
                except ValueError: continue
            if not trusted:
                return Response('Your IP is not authorized to access this GUI.', 403)

        # 2. Session Check
        if 'user_id' not in session:
            # Fallback to Basic Auth if set (for backward compatibility/headless access)
            auth = request.authorization
            if (os.environ.get('GUI_USERNAME') and os.environ.get('GUI_PASSWORD')):
                if not auth or not check_auth(auth.username, auth.password):
                    return authenticate()
            else:
                return redirect(url_for('main.login'))
        
        return f(*args, **kwargs)
    return decorated

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

from cryptography.fernet import Fernet


def get_fernet():
    key = os.environ.get('ENCRYPTION_KEY')
    if not key:
        # Fallback for dev, but should be set in prod
        key = Fernet.generate_key().decode()
        logger.warning("ENCRYPTION_KEY not set, using a temporary key. Data will not be decryptable after restart.")
    return Fernet(key.encode())

def encrypt_string(plain_text: str) -> str:
    if not plain_text: return plain_text
    f = get_fernet()
    return f.encrypt(plain_text.encode()).decode()

def decrypt_string(cipher_text: str) -> str:
    if not cipher_text: return cipher_text
    f = get_fernet()
    try:
        return f.decrypt(cipher_text.encode()).decode()
    except Exception:
        return cipher_text # Return as is if decryption fails (might be unencrypted)

def log_audit(action: str, config_id: Optional[str] = None, details: Optional[str] = None):
    """Helper to log configuration changes."""
    from flask import request

    from .extensions import db
    from .models import AuditLog
    
    user = "System"
    if request.authorization:
        user = request.authorization.username
    
    audit = AuditLog(
        config_id=config_id,
        action=action,
        user=user,
        details=details
    )
    db.session.add(audit)
    db.session.commit()

def mask_secrets(data: Any) -> Any:
    """Recursively mask fields that might contain sensitive information."""
    if not isinstance(data, (dict, list)):
        return data
    
    sensitive_keys = {'password', 'secret', 'token', 'key', 'auth', 'authorization', 'bearer'}
    
    if isinstance(data, list):
        return [mask_secrets(item) for item in data]
    
    masked = {}
    for k, v in data.items():
        if any(sk in k.lower() for sk in sensitive_keys):
            masked[k] = "********"
        elif isinstance(v, (dict, list)):
            masked[k] = mask_secrets(v)
        else:
            masked[k] = v
    return masked

def log_to_web(message: str, level: str = "info", config_name: str = "System", data: Optional[Dict[str, Any]] = None, ticket_id: Optional[int] = None):
    """Helper to send logs to the web GUI via WebSockets."""
    payload_to_send = mask_secrets(data) if data else None
    if isinstance(data, str):
        try:
            payload_to_send = mask_secrets(json.loads(data))
        except Exception:
            payload_to_send = {"raw": data}

    socketio.emit('new_log', {
        'timestamp': datetime.utcnow().isoformat() + "Z",
        'message': message,
        'level': level,
        'config_name': config_name,
        'payload': payload_to_send,
        'ticket_id': ticket_id
    })
