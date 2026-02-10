import base64
import logging
import os
import requests
from typing import Any, Dict, Optional, cast

logger = logging.getLogger(__name__)

class ConnectWiseClient:
    def __init__(self) -> None:
        self.base_url: str = os.getenv('CW_URL', 'https://api-na.myconnectwise.net/v4_6_release/apis/3.0')
        self.company: Optional[str] = os.getenv('CW_COMPANY')
        self.public_key: Optional[str] = os.getenv('CW_PUBLIC_KEY')
        self.private_key: Optional[str] = os.getenv('CW_PRIVATE_KEY')
        self.client_id: Optional[str] = os.getenv('CW_CLIENT_ID')
        
        self.service_board_name: str = os.getenv('CW_SERVICE_BOARD', 'Service Board')
        self.status_new: str = os.getenv('CW_STATUS_NEW', 'New')
        self.status_closed: str = os.getenv('CW_STATUS_CLOSED', 'Closed')
        
        if not all([self.base_url, self.company, self.public_key, self.private_key, self.client_id]):
            logger.warning("ConnectWise credentials (including CW_CLIENT_ID) are missing. API calls will fail.")

        self.headers: Dict[str, str] = self._get_headers()

    def _get_headers(self) -> Dict[str, str]:
        if not self.company or not self.public_key or not self.private_key:
            return {}
            
        auth_string = f"{self.company}+{self.public_key}:{self.private_key}"
        auth_header = f"Basic {base64.b64encode(auth_string.encode()).decode()}"
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if self.client_id:
            headers["clientId"] = self.client_id
        return headers

    def find_open_ticket(self, summary_contains: str) -> Optional[Dict[str, Any]]:
        try:
            conditions = f"closedFlag=false AND summary contains '{summary_contains}'"
            params: Dict[str, Any] = {
                "conditions": conditions,
                "pageSize": 1
            }
            response = requests.get(f"{self.base_url}/service/tickets", headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return cast(Dict[str, Any], data[0])
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error finding ticket: {e}")
            return None

    def create_ticket(self, summary: str, description: str, monitor_name: str, 
                      company_id: Optional[str] = None,
                      board: Optional[str] = None,
                      status: Optional[str] = None,
                      ticket_type: Optional[str] = None,
                      subtype: Optional[str] = None,
                      priority: Optional[str] = None) -> Optional[Dict[str, Any]]:
        try:
            payload: Dict[str, Any] = {
                "summary": summary,
                "recordType": "ServiceTicket",
                "board": {"name": board or self.service_board_name},
                "status": {"name": status or self.status_new},
                "initialDescription": description,
            }
            if ticket_type: payload["type"] = {"name": ticket_type}
            if subtype: payload["subType"] = {"name": subtype}
            if priority: payload["priority"] = {"name": priority}
            
            target_company_id = company_id or os.getenv('CW_DEFAULT_COMPANY_ID')
            if target_company_id: payload["company"] = {"identifier": target_company_id}

            response = requests.post(f"{self.base_url}/service/tickets", headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            ticket = response.json()
            logger.info(f"Created ticket #{ticket.get('id')} for {monitor_name}")
            return cast(Dict[str, Any], ticket)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating ticket: {e}")
            return None

    def close_ticket(self, ticket_id: int, resolution: str) -> bool:
        try:
            patch_payload = [{"op": "replace", "path": "/status/name", "value": self.status_closed}]
            response = requests.patch(f"{self.base_url}/service/tickets/{ticket_id}", headers=self.headers, json=patch_payload, timeout=30)
            response.raise_for_status()
            note_payload = {"text": resolution, "detailDescriptionFlag": True, "internalAnalysisFlag": False, "resolutionFlag": True}
            requests.post(f"{self.base_url}/service/tickets/{ticket_id}/notes", headers=self.headers, json=note_payload, timeout=30)
            logger.info(f"Closed ticket #{ticket_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Error closing ticket #{ticket_id}: {e}")
            return False
