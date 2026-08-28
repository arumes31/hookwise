import fnmatch
import ipaddress
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from functools import lru_cache, wraps
from typing import Any, Dict, Optional, cast

import requests
from cryptography.fernet import Fernet
from flask import Response, jsonify, redirect, request, session, url_for
from jsonpath_ng import parse as _jsonpath_parse

from .extensions import socketio

logger = logging.getLogger(__name__)


def _bounded_timeout(name: str, default: float, maximum: float) -> float:
    try:
        configured = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(configured, 0.1), maximum)


def call_llm(
    prompt: str,
    system_prompt: str = (
        "You are a helpful assistant specialized in ConnectWise ticketing and alert analysis. "
        "Be concise and return only the requested value."
    ),
) -> Optional[str]:
    ollama_host = os.environ.get("OLLAMA_HOST", "http://hookwise-llm:11434")
    timeout = (
        _bounded_timeout("LLM_CONNECT_TIMEOUT", 5.0, 30.0),
        _bounded_timeout("LLM_TIMEOUT", 180.0, 600.0),
    )
    payload = {
        "model": "phi3",
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {"num_predict": int(os.environ.get("LLM_MAX_TOKENS", "512")), "temperature": 0.1},
    }

    for attempt in range(2):
        try:
            response = requests.post(
                f"{ollama_host}/api/generate",
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return cast(str, response.json().get("response", "").strip())
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == 0:
                time.sleep(0.25)
                continue
            logger.error("Error calling LLM after bounded retry: %s", exc)
        except requests.exceptions.HTTPError as exc:
            if attempt == 0 and exc.response is not None and exc.response.status_code in {429, 502, 503, 504}:
                time.sleep(0.25)
                continue
            logger.error("LLM returned an HTTP error: %s", exc)
        except (TypeError, ValueError, requests.exceptions.RequestException) as exc:
            logger.error("Error calling LLM: %s", exc)
        return None

    return None


def check_auth(username: str, password: str) -> bool:
    """Check if a username/password combination is valid."""
    import hmac as _hmac

    expected_username = os.environ.get("GUI_USERNAME", "admin")
    expected_password = os.environ.get("GUI_PASSWORD")
    if not expected_password:
        return False  # Fail closed if password not set
    return _hmac.compare_digest(username, expected_username) and _hmac.compare_digest(password, expected_password)


def authenticate() -> Response:
    """Sends a 401 response that enables basic auth."""
    return Response(
        "Could not verify your access level for that URL.\nYou have to login with proper credentials",
        401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'},
    )


@lru_cache(maxsize=128)
def parse_ip_network(network_str: str) -> Any:
    """Cache parsed IP network objects to avoid re-parsing the same range."""
    return ipaddress.ip_network(network_str)


def auth_required(f: Any) -> Any:
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        # 1. IP Whitelist Check (Global)
        trusted_ips = os.environ.get("GUI_TRUSTED_IPS")
        if trusted_ips:
            client_ip = request.remote_addr
            trusted = False
            for trusted_range in [ip.strip() for ip in trusted_ips.split(",")]:
                try:
                    if client_ip and ipaddress.ip_address(client_ip) in parse_ip_network(trusted_range):
                        trusted = True
                        break
                except ValueError:
                    continue
            if not trusted:
                return Response("Your IP is not authorized to access this GUI.", 403)

        # 2. Session Check (Primary for GUI)
        if "user_id" in session:
            return f(*args, **kwargs)

        # 3. Basic Auth Check (Fallback for API/Headless)
        auth = request.authorization
        gui_user = os.environ.get("GUI_USERNAME")
        gui_pass = os.environ.get("GUI_PASSWORD")

        if auth and gui_user and gui_pass:
            # Only check Basic Auth if the client sent the header
            if auth.username and auth.password and check_auth(auth.username, auth.password):
                # Valid Basic Auth - populate synthetic session
                from .models import User

                user = User.query.filter_by(username=auth.username).first()
                if user:
                    session["user_id"] = user.id
                    session["username"] = user.username
                    session["role"] = user.role
                else:
                    session["user_id"] = "basic_auth"
                    session["username"] = auth.username
                    session["role"] = "admin"
                return f(*args, **kwargs)
            else:
                # Client sent invalid Basic Auth credentials
                return authenticate()

        # 4. No valid auth found. Programmatic requests receive a stable 401;
        # browser page navigations retain the friendly login redirect.
        accepts_json = request.accept_mimetypes.best == "application/json"
        if request.path.startswith("/api/") or request.headers.get("Sec-Fetch-Dest") == "empty" or accepts_json:
            return jsonify({"status": "error", "message": "Authentication required"}), 401
        return redirect(url_for("main.login"))

    return decorated


@lru_cache(maxsize=128)
def _cached_jsonpath_parse(path: str) -> Any:
    """Cache parsed JSONPath expressions to avoid re-parsing the same path."""
    return _jsonpath_parse(path)


def resolve_jsonpath(data: Dict[str, Any], path: str) -> Optional[Any]:
    """Resolve a JSONPath expression against the data."""
    if not path:
        return None
    try:
        jsonpath_expr = _cached_jsonpath_parse(path)
        matches = jsonpath_expr.find(data)
        if matches:
            return matches[0].value
        return None
    except Exception:
        return None


_CIPP_FIELD_LABELS = {
    "Title": "Title",
    "Severity": "Severity",
    "Category": "Category",
    "ProductName": "Product",
    "DetectionSource": "Detection Source",
    "ServiceSource": "Service Source",
    "Classification": "Classification",
    "Determination": "Determination",
    "ThreatDisplayName": "Threat",
    "ThreatFamilyName": "Threat Family",
    "ActorDisplayName": "Threat Actor",
    "MitreTechniques": "MITRE Techniques",
    "AssignedTo": "Assigned To",
    "FirstActivityDateTime": "First Activity",
    "LastActivityDateTime": "Last Activity",
    "CreatedAt": "Created",
    "Description": "Description",
    "RecommendedActions": "Recommended Actions",
    "AlertId": "Alert ID",
    "IncidentId": "Incident ID",
    "AlertUrl": "Alert URL",
    "IncidentUrl": "Incident URL",
    "AppName": "Application Name",
    "DisplayName": "Application Name",
    "AppId": "Application ID",
    "SecretName": "Secret Name",
    "SecretID": "Secret ID",
    "Expires": "Expiration Date",
    "Type": "Application Type",
    "ServicePrincipalId": "Service Principal ID",
    "Tenant": "Tenant",
}

_CIPP_DEFENDER_SUMMARY_FIELDS = (
    "Title",
    "Severity",
    "Category",
    "ProductName",
    "DetectionSource",
    "ServiceSource",
    "Classification",
    "Determination",
    "ThreatDisplayName",
    "ThreatFamilyName",
    "ActorDisplayName",
    "MitreTechniques",
    "AssignedTo",
)
_CIPP_DEFENDER_TIMESTAMP_FIELDS = ("FirstActivityDateTime", "LastActivityDateTime", "CreatedAt")
_CIPP_DEFENDER_REFERENCE_FIELDS = ("AlertId", "IncidentId", "AlertUrl", "IncidentUrl")
_CIPP_APPLICATION_FIELDS = (
    "AppName",
    "DisplayName",
    "AppId",
    "SecretName",
    "SecretID",
    "Type",
    "ServicePrincipalId",
    "Expires",
    "Tenant",
)

_CIPP_APP_CERTIFICATE_EXPIRY_COMMAND = "Get-CIPPAlertAppCertificateExpiry"
CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY = "hookwise_cipp_app_certificate_exclude_names"


def parse_cipp_app_certificate_exclude_patterns(raw_patterns: Any) -> tuple[str, ...]:
    """Parse the GUI setting containing one exact application name or glob per line."""
    if not isinstance(raw_patterns, str):
        return ()

    patterns: list[str] = []
    seen: set[str] = set()
    for line in raw_patterns.splitlines():
        pattern = line.strip()
        normalized_pattern = pattern.casefold()
        if pattern and normalized_pattern not in seen:
            patterns.append(pattern)
            seen.add(normalized_pattern)
    return tuple(patterns)


def filter_cipp_app_certificate_expiry_results(
    data: Dict[str, Any], exclude_patterns: tuple[str, ...]
) -> tuple[Dict[str, Any], list[str]]:
    """Remove globally excluded applications from CIPP certificate-expiry results.

    Matching is case-insensitive and supports shell-style ``*`` and ``?`` wildcards.
    The input mapping is returned unchanged when the payload or command is not applicable.
    """
    raw_task_info = data.get("TaskInfo") if isinstance(data, dict) else None
    task_info: Dict[str, Any] = raw_task_info if isinstance(raw_task_info, dict) else {}
    if task_info.get("Command") != _CIPP_APP_CERTIFICATE_EXPIRY_COMMAND:
        return data, []

    results = data.get("Results")
    if not exclude_patterns or not isinstance(results, list):
        return data, []

    normalized_patterns = tuple(pattern.casefold() for pattern in exclude_patterns)
    included_results: list[Any] = []
    excluded_names: list[str] = []
    for item in results:
        raw_name = None
        if isinstance(item, dict):
            raw_name = item.get("DisplayName") or item.get("AppName")
        name = str(raw_name).strip() if raw_name is not None else ""
        if name and any(fnmatch.fnmatchcase(name.casefold(), pattern) for pattern in normalized_patterns):
            excluded_names.append(name)
        else:
            included_results.append(item)

    if not excluded_names:
        return data, []

    filtered_data = dict(data)
    filtered_data["Results"] = included_results
    return filtered_data, excluded_names


def _has_cipp_value(value: Any) -> bool:
    """Return whether a CIPP result value should be rendered."""
    if isinstance(value, str):
        normalized = value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        return bool(normalized.strip())
    if isinstance(value, list):
        return any(_has_cipp_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_cipp_value(item) for item in value.values())
    return value is not None


def _format_cipp_value(value: Any) -> str:
    """Format a CIPP field without falling back to Python object repr output."""
    if isinstance(value, str):
        return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").strip()
    if isinstance(value, list):
        populated = [item for item in value if _has_cipp_value(item)]
        if all(not isinstance(item, (dict, list)) for item in populated):
            return ", ".join(str(item) for item in populated)
        return json.dumps(populated, ensure_ascii=False, indent=2)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _humanize_cipp_key(key: str) -> str:
    """Turn an unknown CIPP payload key into a readable English label."""
    if key in _CIPP_FIELD_LABELS:
        return _CIPP_FIELD_LABELS[key]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).replace("_", " ")
    return words.strip().title()


def _format_cipp_labeled_value(key: str, value: Any) -> list[str]:
    formatted = _format_cipp_value(value)
    label = _humanize_cipp_key(key)
    if "\n" in formatted:
        return [f"{label}:", formatted]
    return [f"{label}: {formatted}"]


def _append_cipp_field_section(
    output: list[str], title: str, item: dict[str, Any], fields: tuple[str, ...], consumed: set[str]
) -> None:
    lines: list[str] = []
    for key in fields:
        consumed.add(key)
        value = item.get(key)
        if _has_cipp_value(value):
            lines.extend(_format_cipp_labeled_value(key, value))
    if lines:
        if output:
            output.append("")
        output.extend((title, "", *lines))


def _append_cipp_body_section(
    output: list[str], title: str, item: dict[str, Any], key: str, consumed: set[str], *, bullets: bool = False
) -> None:
    consumed.add(key)
    value = item.get(key)
    if not _has_cipp_value(value):
        return
    body = _format_cipp_value(value)
    if bullets:
        action_lines = [line.strip() for line in body.splitlines() if line.strip()]
        body = "\n".join(line if re.match(r"^(?:[-*]|\d+[.)])\s", line) else f"- {line}" for line in action_lines)
    if output:
        output.append("")
    output.extend((title, "", body))


def _format_cipp_result_item(item: Any, index: int, command: str) -> str:
    is_application = "AppSecretExpiry" in command or "AppCertificateExpiry" in command
    is_defender = "Defender" in command
    item_title = "APPLICATION" if is_application else "ALERT" if is_defender else "RESULT"
    output = [f"{item_title} {index}"]

    if not isinstance(item, dict):
        if _has_cipp_value(item):
            output.extend(("", _format_cipp_value(item)))
        return "\n".join(output)

    consumed: set[str] = set()
    if is_defender:
        _append_cipp_field_section(output, "ALERT DETAILS", item, _CIPP_DEFENDER_SUMMARY_FIELDS, consumed)
        _append_cipp_field_section(output, "TIMESTAMPS", item, _CIPP_DEFENDER_TIMESTAMP_FIELDS, consumed)
        _append_cipp_body_section(output, "DESCRIPTION", item, "Description", consumed)
        _append_cipp_body_section(output, "RECOMMENDED ACTIONS", item, "RecommendedActions", consumed, bullets=True)
        _append_cipp_field_section(output, "REFERENCES", item, _CIPP_DEFENDER_REFERENCE_FIELDS, consumed)
    elif is_application:
        _append_cipp_field_section(output, "APPLICATION DETAILS", item, _CIPP_APPLICATION_FIELDS, consumed)

    remaining = [key for key, value in item.items() if key not in consumed and _has_cipp_value(value)]
    if remaining:
        _append_cipp_field_section(output, "DETAILS", item, tuple(remaining), consumed)

    return "\n".join(output)


def format_cipp_results(data: Dict[str, Any]) -> str:
    """Render the CIPP ``Results`` collection as readable ConnectWise plain text."""
    results = data.get("Results") if isinstance(data, dict) else None
    if not _has_cipp_value(results):
        return "No alert results were returned."

    raw_task_info = data.get("TaskInfo")
    task_info: Dict[str, Any] = raw_task_info if isinstance(raw_task_info, dict) else {}
    command = str(task_info.get("Command", ""))
    result_items = results if isinstance(results, list) else [results]
    rendered = [_format_cipp_result_item(item, index, command) for index, item in enumerate(result_items, start=1)]
    return "\n\n".join(block for block in rendered if block)


def resolve_monitor_name(data: Dict[str, Any]) -> str:
    """Resolve a human-readable monitor/source name from a webhook payload.

    The ``monitor`` field may be a dict (e.g. Uptime Kuma ``{"name": ...}``) or a
    plain string (e.g. Fortinet/Graylog payloads). Fall back to ``title``/``name``
    and finally a generic placeholder.
    """
    monitor = data.get("monitor")
    if isinstance(monitor, dict):
        name = monitor.get("name")
        if name:
            return str(name)
    elif isinstance(monitor, str) and monitor:
        return monitor
    return str(data.get("title", data.get("name", "Unknown Source")))


_fernet_instance = None


def get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        logger.critical("ENCRYPTION_KEY not set! This is required for security.")
        raise RuntimeError("ENCRYPTION_KEY environment variable is not set.")
    try:
        _fernet_instance = Fernet(key.encode())
    except (ValueError, TypeError) as e:
        logger.critical(f"Invalid ENCRYPTION_KEY: {e}")
        raise RuntimeError(f"Invalid ENCRYPTION_KEY: {e}") from e
    return _fernet_instance


def encrypt_string(plain_text: str) -> str:
    if not plain_text:
        return plain_text
    f = get_fernet()
    return f.encrypt(plain_text.encode()).decode()


def decrypt_string(cipher_text: str) -> str:
    if not cipher_text:
        return cipher_text
    f = get_fernet()
    try:
        return f.decrypt(cipher_text.encode()).decode()
    except Exception:
        return cipher_text  # Return as is if decryption fails (might be unencrypted)


def log_audit(
    action: str,
    config_id: Optional[str] = None,
    details: Optional[str] = None,
    commit: bool = True,
    db_session: Any = None,
) -> None:
    """Helper to log configuration changes."""
    from flask import has_request_context, request, session

    from .extensions import db
    from .models import AuditLog

    user = "System"
    if has_request_context():
        sess_user = session.get("username")
        if sess_user:
            user = str(sess_user)
        elif (
            getattr(request, "authorization", None)
            and request.authorization is not None
            and getattr(request.authorization, "username", None)
        ):
            user = str(request.authorization.username)

    audit = AuditLog(config_id=config_id, action=action, user=user, details=details)
    target_session = db_session if db_session is not None else db.session
    target_session.add(audit)
    if commit:
        target_session.commit()


def mask_secrets(data: Any) -> Any:
    """Recursively mask fields that might contain sensitive information."""
    if not isinstance(data, (dict, list)):
        return data

    sensitive_keys = {"password", "secret", "token", "key", "auth", "authorization", "bearer"}

    if isinstance(data, list):
        return [mask_secrets(item) for item in data]

    masked = {}
    for k, v in data.items():
        if any(sk in k.lower() for sk in sensitive_keys):
            masked[k] = "***"
        elif isinstance(v, (dict, list)):
            masked[k] = mask_secrets(v)
        else:
            masked[k] = v
    return masked


def log_to_web(
    message: str,
    level: str = "info",
    config_name: str = "System",
    data: Optional[Dict[str, Any]] = None,
    ticket_id: Optional[int] = None,
) -> None:
    """Helper to send logs to the web GUI via WebSockets."""
    payload_to_send = mask_secrets(data) if data else None
    if isinstance(data, str):
        try:
            payload_to_send = mask_secrets(json.loads(data))
        except Exception:
            # If we can't parse it as JSON, it might contain secrets we can't easily identify.
            # Safer to redact than to leak.
            payload_to_send = {"raw": "[Redacted] Data could not be parsed safely."}

    socketio.emit(
        "new_log",
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "level": level,
            "config_name": config_name,
            "payload": payload_to_send,
            "ticket_id": ticket_id,
        },
    )
