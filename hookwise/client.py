import base64
import logging
import os
from math import isfinite
from typing import Any, Dict, List, Optional, cast

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_QUOTA_HEADER_ALIASES = {
    "limit": ("x-ratelimit-limit", "ratelimit-limit", "x-rate-limit-limit", "x-quota-limit"),
    "remaining": ("x-ratelimit-remaining", "ratelimit-remaining", "x-rate-limit-remaining", "x-quota-remaining"),
    "reset": ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset", "x-quota-reset"),
}


class ConnectWiseError(Exception):
    pass


class TicketNotFoundError(ConnectWiseError):
    pass


class TicketRequestError(ConnectWiseError):
    pass


class TicketCreationRejected(TicketRequestError):
    """ConnectWise definitively rejected a ticket create request."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class TicketCreationOutcomeUnknown(TicketRequestError):
    """The ticket request may have reached ConnectWise without a response."""

    retry_after_seconds = 600.0


class ConnectWiseClient:
    def __init__(self) -> None:
        self.base_url: str = os.getenv("CW_URL", "https://api-na.myconnectwise.net/v4_6_release/apis/3.0")
        self.company: Optional[str] = os.getenv("CW_COMPANY")
        self.public_key: Optional[str] = os.getenv("CW_PUBLIC_KEY")
        self.private_key: Optional[str] = os.getenv("CW_PRIVATE_KEY")
        self.client_id: Optional[str] = os.getenv("CW_CLIENT_ID")

        self.service_board_name: str = os.getenv("CW_SERVICE_BOARD", "Service Board")
        self.status_new: str = os.getenv("CW_STATUS_NEW", "New")
        self.status_closed: str = os.getenv("CW_STATUS_CLOSED", "Closed")
        self.timeout = (
            max(1.0, float(os.getenv("CW_CONNECT_TIMEOUT", "5"))),
            max(1.0, float(os.getenv("CW_READ_TIMEOUT", "30"))),
        )

        if not all([self.base_url, self.company, self.public_key, self.private_key, self.client_id]):
            logger.warning("ConnectWise credentials (including CW_CLIENT_ID) are missing. API calls will fail.")

        self.headers: Dict[str, str] = self._get_headers()
        self.session = self._get_session()

    def _get_headers(self) -> Dict[str, str]:
        if not self.company or not self.public_key or not self.private_key:
            return {}

        auth_string = f"{self.company}+{self.public_key}:{self.private_key}"
        auth_header = f"Basic {base64.b64encode(auth_string.encode()).decode()}"
        headers = {"Authorization": auth_header, "Content-Type": "application/json", "Accept": "application/json"}
        if self.client_id:
            headers["clientId"] = self.client_id
        return headers

    def _get_session(self) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=2,  # Exponential backoff: 2, 4, 8, 16, 32 seconds
            backoff_jitter=0.1,  # Added jitter to prevent thundering herd
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.hooks["response"].append(self._capture_quota_headers)
        return session

    @staticmethod
    def _numeric_header(headers: Any, aliases: tuple[str, ...]) -> Optional[str]:
        """Return a finite numeric quota header, never a raw provider value."""
        lowered = {str(key).lower(): value for key, value in headers.items()}
        for header in aliases:
            value = lowered.get(header)
            if value is None:
                continue
            try:
                parsed = float(value)
            except TypeError, ValueError:
                continue
            if isfinite(parsed):
                return str(int(parsed)) if parsed.is_integer() else str(parsed)
        return None

    def _capture_quota_headers(self, response: requests.Response, *args: Any, **kwargs: Any) -> requests.Response:
        """Cache only numeric API quota/rate-limit telemetry for operational views."""
        try:
            from .extensions import redis_client

            for metric, aliases in _QUOTA_HEADER_ALIASES.items():
                value = self._numeric_header(response.headers, aliases)
                if value is not None:
                    redis_client.setex(f"hookwise:cw:quota:{metric}", 3600, value)
        except Exception:
            # Provider telemetry must never affect a ConnectWise request result.
            pass
        return response

    def find_open_ticket(self, summary_contains: str, close_status: Optional[str] = None) -> Optional[Dict[str, Any]]:
        try:
            safe_summary = summary_contains.replace("'", "''")
            excluded_statuses = [self.status_closed, "Cancelled", "Completed"]
            if close_status:
                excluded_statuses.append(close_status)
            status_clauses = " AND ".join([f"status/name != '{s.replace("'", "''")}'" for s in excluded_statuses])

            conditions = f"closedFlag=false AND {status_clauses} AND summary contains '{safe_summary}'"
            params: Dict[str, Any] = {"conditions": conditions, "pageSize": 1}
            response = self.session.get(
                f"{self.base_url}/service/tickets", headers=self.headers, params=params, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return cast(Dict[str, Any], data[0])
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error finding ticket: {e}")
            raise TicketRequestError("Unable to determine whether an open ticket exists") from e

    def get_ticket(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/service/tickets/{ticket_id}", headers=self.headers, timeout=self.timeout
            )
            if response.status_code == 404:
                raise TicketNotFoundError(f"Ticket {ticket_id} not found")
            response.raise_for_status()
            return cast(Dict[str, Any], response.json())
        except requests.exceptions.RequestException as e:
            if hasattr(e, "response") and e.response is not None and getattr(e.response, "status_code", None) == 404:
                raise TicketNotFoundError(f"Ticket {ticket_id} not found") from e
            logger.error(f"Error getting ticket {ticket_id}: {e}")
            raise TicketRequestError(str(e)) from e

    def _build_ticket_payload(
        self,
        summary: str,
        description: str,
        company_id: Optional[str] = None,
        board: Optional[str] = None,
        status: Optional[str] = None,
        ticket_type: Optional[str] = None,
        subtype: Optional[str] = None,
        item: Optional[str] = None,
        priority: Optional[str] = None,
        severity: Optional[str] = None,
        impact: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "summary": summary,
            "recordType": "ServiceTicket",
            "board": {"name": board or self.service_board_name},
            "status": {"name": status or self.status_new},
            "initialDescription": description,
        }
        if ticket_type:
            payload["type"] = {"name": ticket_type}
        if subtype:
            payload["subType"] = {"name": subtype}
        if item:
            payload["item"] = {"name": item}
        if priority:
            payload["priority"] = {"name": priority}
        if severity:
            payload["severity"] = severity
        if impact:
            payload["impact"] = impact

        target_company_id = company_id or os.getenv("CW_DEFAULT_COMPANY_ID")
        if target_company_id:
            payload["company"] = {"identifier": target_company_id}

        return payload

    def create_ticket(
        self,
        summary: str,
        description: str,
        monitor_name: str,
        company_id: Optional[str] = None,
        board: Optional[str] = None,
        status: Optional[str] = None,
        ticket_type: Optional[str] = None,
        subtype: Optional[str] = None,
        item: Optional[str] = None,
        priority: Optional[str] = None,
        severity: Optional[str] = None,
        impact: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            payload = self._build_ticket_payload(
                summary=summary,
                description=description,
                company_id=company_id,
                board=board,
                status=status,
                ticket_type=ticket_type,
                subtype=subtype,
                item=item,
                priority=priority,
                severity=severity,
                impact=impact,
            )

            response = self.session.post(
                f"{self.base_url}/service/tickets", headers=self.headers, json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            ticket = response.json()
            logger.info(f"Created ticket #{ticket.get('id')} for {monitor_name}")
            return cast(Dict[str, Any], ticket)
        except requests.exceptions.RequestException as e:
            error_msg = f"Error creating ticket: {e}"
            if e.response is not None:
                error_msg += f" | Response: {e.response.text}"
            logger.error(error_msg)
            if e.response is not None:
                status_code = int(e.response.status_code)
                detail = ""
                try:
                    response_data = e.response.json()
                    errors = response_data.get("errors", []) if isinstance(response_data, dict) else []
                    if isinstance(errors, list):
                        messages = [
                            str(item["message"])[:300]
                            for item in errors
                            if isinstance(item, dict) and item.get("message")
                        ]
                        detail = "; ".join(messages[:3])
                    if not detail and isinstance(response_data, dict) and response_data.get("message"):
                        detail = str(response_data["message"])[:500]
                except TypeError, ValueError:
                    pass
                message = f"ConnectWise rejected ticket creation (HTTP {status_code})"
                if detail:
                    message += f": {detail}"
                if status_code in {408, 409} or status_code >= 500:
                    raise TicketCreationOutcomeUnknown(
                        message.replace("rejected", "returned an ambiguous response for")
                    ) from e
                retryable = status_code in {425, 429}
                raise TicketCreationRejected(message, retryable=retryable) from e
            raise TicketCreationOutcomeUnknown(
                "ConnectWise ticket creation outcome is unknown because no response was received"
            ) from e

    def close_ticket(self, ticket_id: int, resolution: str, status_name: Optional[str] = None) -> bool:
        target_status = status_name or self.status_closed
        patch_payload = [{"op": "replace", "path": "/status/name", "value": target_status}]
        try:
            response = self.session.patch(
                f"{self.base_url}/service/tickets/{ticket_id}",
                headers=self.headers,
                json=patch_payload,
                timeout=self.timeout,
            )
            if response.status_code == 404:
                raise TicketNotFoundError(f"Ticket {ticket_id} not found")
            if not response.ok:
                logger.error(
                    "Error closing ticket #%s with status '%s': %s - %s",
                    ticket_id,
                    target_status,
                    response.status_code,
                    response.text,
                )
                return False
        except requests.exceptions.RequestException as e:
            if hasattr(e, "response") and e.response is not None and getattr(e.response, "status_code", None) == 404:
                raise TicketNotFoundError(f"Ticket {ticket_id} not found") from e
            logger.error("Request exception closing ticket #%s: %s", ticket_id, e)
            raise TicketRequestError(str(e)) from e

        note_payload = {
            "text": resolution,
            "detailDescriptionFlag": True,
            "internalAnalysisFlag": False,
            "resolutionFlag": True,
        }
        try:
            note_response = self.session.post(
                f"{self.base_url}/service/tickets/{ticket_id}/notes",
                headers=self.headers,
                json=note_payload,
                timeout=self.timeout,
            )
            if note_response.status_code not in [200, 201]:
                logger.error(
                    "Error adding closing note to ticket #%s: %s - %s",
                    ticket_id,
                    note_response.status_code,
                    note_response.text,
                )
        except requests.exceptions.RequestException as e:
            logger.error("Request exception adding closing note to ticket #%s: %s", ticket_id, e)

        logger.info("Closed ticket #%s", ticket_id)
        return True

    def add_ticket_note(self, ticket_id: int, note_text: str, is_internal: bool = False) -> bool:
        try:
            note_payload = {
                "text": note_text,
                "detailDescriptionFlag": True,
                "internalAnalysisFlag": is_internal,
                "resolutionFlag": False,
            }
            response = self.session.post(
                f"{self.base_url}/service/tickets/{ticket_id}/notes",
                headers=self.headers,
                json=note_payload,
                timeout=self.timeout,
            )
            if response.status_code not in [200, 201]:
                logger.error(f"Error adding note to ticket #{ticket_id}: {response.status_code} - {response.text}")
                return False

            logger.info(f"Added note to ticket #{ticket_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Request exception adding note to ticket #{ticket_id}: {e}")
            return False

    def get_boards(self) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(f"{self.base_url}/service/boards", headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching boards: {e}")
            return []

    def get_priorities(self) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/service/priorities", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching priorities: {e}")
            return []

    def get_board_statuses(self, board_id: int) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/service/boards/{board_id}/statuses", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching statuses for board {board_id}: {e}")
            return []

    def get_board_types(self, board_id: int) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/service/boards/{board_id}/types", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching types for board {board_id}: {e}")
            return []

    def get_board_subtypes(self, board_id: int) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/service/boards/{board_id}/subtypes", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching subtypes for board {board_id}: {e}")
            return []

    def get_board_items(self, board_id: int) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                f"{self.base_url}/service/boards/{board_id}/items", headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching items for board {board_id}: {e}")
            return []

    def get_companies(self, search: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            params: Dict[str, Any] = {"pageSize": 50}
            if search:
                params["conditions"] = f"identifier contains '{search}' OR name contains '{search}'"
            response = self.session.get(
                f"{self.base_url}/company/companies", headers=self.headers, params=params, timeout=self.timeout
            )
            response.raise_for_status()
            return cast(List[Dict[str, Any]], response.json())
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching companies: {e}")
            return []
