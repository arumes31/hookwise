import base64
import logging
import os
from collections.abc import Sequence
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
_CONFIGURATION_QUERY_FIELDS = frozenset(
    {"id", "deviceIdentifier", "serialNumber", "macAddress", "tagNumber", "ipAddress", "name"}
)
_CONFIGURATION_RESPONSE_FIELDS = (
    "id,name,company,deviceIdentifier,serialNumber,macAddress,tagNumber,ipAddress,activeFlag"
)
_MAX_CONFIGURATION_RESULTS = 2
_MAX_CONFIGURATION_MATCH_CRITERIA = 16
_MAX_CONFIGURATION_MATCH_RESULTS = 129


class ConnectWiseError(Exception):
    pass


class ConfigurationRequestError(ConnectWiseError):
    """A configuration lookup or association request failed safely."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        status_code: Optional[int] = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


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

    @staticmethod
    def _positive_id(value: Any, field_name: str) -> int:
        if type(value) is int:
            parsed = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            parsed = int(value)
        else:
            raise ValueError(f"{field_name} must be a positive integer")
        if parsed <= 0:
            raise ValueError(f"{field_name} must be a positive integer")
        return parsed

    @staticmethod
    def _configuration_error(
        operation: str,
        *,
        status_code: Optional[int] = None,
        mutation: bool = False,
    ) -> ConfigurationRequestError:
        retryable = status_code is None or status_code in {408, 425, 429} or status_code >= 500
        outcome_unknown = mutation and (status_code is None or status_code in {408, 409} or status_code >= 500)
        status_suffix = f" (HTTP {status_code})" if status_code is not None else ""
        return ConfigurationRequestError(
            f"ConnectWise configuration {operation} failed{status_suffix}",
            operation=operation,
            status_code=status_code,
            retryable=retryable,
            outcome_unknown=outcome_unknown,
        )

    @staticmethod
    def _request_status_code(error: requests.exceptions.RequestException) -> Optional[int]:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None) if response is not None else None
        return int(status_code) if isinstance(status_code, int) else None

    def find_open_ticket(
        self,
        summary_contains: str,
        close_status: Optional[str] = None,
        company_identifier: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            safe_summary = summary_contains.replace("'", "''")
            excluded_statuses = [self.status_closed, "Cancelled", "Completed"]
            if close_status:
                excluded_statuses.append(close_status)
            status_clauses = " AND ".join([f"status/name != '{s.replace("'", "''")}'" for s in excluded_statuses])

            company_clause = ""
            if company_identifier:
                safe_company_identifier = company_identifier.replace("'", "''")
                company_clause = f"company/identifier = '{safe_company_identifier}' AND "
            conditions = f"closedFlag=false AND {company_clause}{status_clauses} AND summary contains '{safe_summary}'"
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

    def find_configurations(
        self,
        company_id: int,
        field: str,
        value: Any,
        *,
        page_size: int = _MAX_CONFIGURATION_RESULTS,
    ) -> List[Dict[str, Any]]:
        """Return at most two active, exact matches within one numeric company ID."""
        if type(company_id) is not int or company_id <= 0:
            raise ValueError("company_id must be a positive integer")
        if type(page_size) is not int or page_size <= 0:
            raise ValueError("page_size must be a positive integer")

        clause = self._configuration_query_clause(field, value)
        conditions = f"company/id = {company_id} AND activeFlag = true AND {clause}"
        return self._request_configurations(conditions, min(page_size, _MAX_CONFIGURATION_RESULTS))

    def find_matching_configurations(
        self,
        company_id: int,
        searches: Sequence[tuple[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Search all exact identifiers in one bounded provider request.

        One extra result beyond the matcher's 128-record validation budget lets
        the selector detect truncation instead of treating a capped page as unique.
        """
        if type(company_id) is not int or company_id <= 0:
            raise ValueError("company_id must be a positive integer")
        if not 1 <= len(searches) <= _MAX_CONFIGURATION_MATCH_CRITERIA:
            raise ValueError("configuration search requires 1 to 16 criteria")

        clauses: list[str] = []
        for search in searches:
            if not isinstance(search, tuple) or len(search) != 2:
                raise ValueError("configuration search criteria must be field/value pairs")
            clause = self._configuration_query_clause(search[0], search[1])
            if clause not in clauses:
                clauses.append(clause)
        joined_clauses = " OR ".join(clauses)
        conditions = f"company/id = {company_id} AND activeFlag = true AND ({joined_clauses})"
        return self._request_configurations(conditions, _MAX_CONFIGURATION_MATCH_RESULTS)

    def _configuration_query_clause(self, field: str, value: Any) -> str:
        if field not in _CONFIGURATION_QUERY_FIELDS:
            raise ValueError("field is not allowed for configuration lookup")
        if field == "id":
            query_value = str(self._positive_id(value, "configuration id"))
        else:
            if not isinstance(value, str):
                raise ValueError("configuration query value must be a string")
            query_value = value.strip()
            if not query_value or len(query_value) > 255:
                raise ValueError("configuration query value must contain 1 to 255 characters")
            query_value = f"'{query_value.replace("'", "''")}'"
        return f"{field} = {query_value}"

    def _request_configurations(self, conditions: str, page_size: int) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "conditions": conditions,
            "fields": _CONFIGURATION_RESPONSE_FIELDS,
            "pageSize": page_size,
        }
        try:
            response = self.session.get(
                f"{self.base_url}/company/configurations",
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            status_code = self._request_status_code(e)
            logger.error("ConnectWise configuration search failed (HTTP %s)", status_code)
            raise self._configuration_error("search", status_code=status_code) from e
        except ValueError as e:
            raise ConfigurationRequestError(
                "ConnectWise configuration search returned an unexpected response",
                operation="search",
            ) from e

        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ConfigurationRequestError(
                "ConnectWise configuration search returned an unexpected response",
                operation="search",
            )
        return cast(List[Dict[str, Any]], data)

    def is_configuration_attached(self, ticket_id: int, configuration_id: int) -> bool:
        """Read back one exact ticket/configuration association."""
        ticket_id = self._positive_id(ticket_id, "ticket_id")
        configuration_id = self._positive_id(configuration_id, "configuration_id")
        try:
            response = self.session.get(
                f"{self.base_url}/service/tickets/{ticket_id}/configurations/{configuration_id}",
                headers=self.headers,
                timeout=self.timeout,
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            if response.status_code != 200:
                raise self._configuration_error("readback", status_code=response.status_code)
            return True
        except ConfigurationRequestError:
            raise
        except requests.exceptions.RequestException as e:
            status_code = self._request_status_code(e)
            logger.error("ConnectWise configuration association readback failed (HTTP %s)", status_code)
            raise self._configuration_error("readback", status_code=status_code) from e

    def attach_configuration(self, ticket_id: int, configuration_id: int) -> Dict[str, Any]:
        """Attach one configuration; callers must perform readback before retrying."""
        ticket_id = self._positive_id(ticket_id, "ticket_id")
        configuration_id = self._positive_id(configuration_id, "configuration_id")
        try:
            response = self.session.post(
                f"{self.base_url}/service/tickets/{ticket_id}/configurations",
                headers=self.headers,
                json={"id": configuration_id},
                timeout=self.timeout,
            )
            response.raise_for_status()
            if response.status_code not in {200, 201}:
                raise self._configuration_error("attach", status_code=response.status_code, mutation=True)
            data = response.json()
        except ConfigurationRequestError:
            raise
        except requests.exceptions.RequestException as e:
            status_code = self._request_status_code(e)
            logger.error("ConnectWise configuration association failed (HTTP %s)", status_code)
            raise self._configuration_error("attach", status_code=status_code, mutation=True) from e
        except ValueError as e:
            raise ConfigurationRequestError(
                "ConnectWise configuration attach returned an unexpected response",
                operation="attach",
                outcome_unknown=True,
            ) from e

        if not isinstance(data, dict) or data.get("id") != configuration_id:
            raise ConfigurationRequestError(
                "ConnectWise configuration attach returned an unexpected response",
                operation="attach",
                outcome_unknown=True,
            )
        return cast(Dict[str, Any], data)

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
