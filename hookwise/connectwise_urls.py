"""Safe ConnectWise Manage web links derived from deployment configuration."""

import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_CW_URL = "https://api-na.myconnectwise.net/v4_6_release/apis/3.0"
_RELEASE_PATH = "/v4_6_release"
_TICKET_PATH = "/services/system_io/Service/fv_sr100_request.rails"


def _connectwise_web_base_url() -> str:
    """Return the configured ConnectWise web origin and release path."""
    configured = os.environ.get("CW_WEB_URL", "").strip() or os.environ.get("CW_URL", DEFAULT_CW_URL).strip()
    parsed = urlsplit(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""

    path = parsed.path.replace("\\", "/")
    release_index = path.lower().find(_RELEASE_PATH)
    release_path = path[: release_index + len(_RELEASE_PATH)] if release_index >= 0 else _RELEASE_PATH
    return urlunsplit((parsed.scheme, parsed.netloc, release_path, "", ""))


def connectwise_ticket_url(ticket_id: Any) -> str:
    """Build the ConnectWise Manage UI URL for a positive numeric ticket ID."""
    if isinstance(ticket_id, bool):
        return ""
    normalized = str(ticket_id).strip()
    if not normalized.isascii() or not normalized.isdecimal() or len(normalized) > 20 or int(normalized) <= 0:
        return ""

    base_url = _connectwise_web_base_url()
    if not base_url:
        return ""
    return f"{base_url}{_TICKET_PATH}?service_recid={quote(normalized, safe='')}"


def connectwise_ticket_url_template() -> str:
    """Return the browser-side ticket URL template used by live activity rows."""
    base_url = _connectwise_web_base_url()
    if not base_url:
        return ""
    return f"{base_url}{_TICKET_PATH}?service_recid={{ticket_id}}"
