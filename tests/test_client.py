from unittest.mock import MagicMock, patch

import pytest
import requests

from hookwise.client import ConnectWiseClient, TicketNotFoundError, TicketRequestError


@pytest.fixture
def client():
    with patch('hookwise.client.requests.Session'):
        return ConnectWiseClient()

def test_get_ticket_success(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": 123, "summary": "Test Ticket"}
    client.session.get.return_value = mock_response

    ticket = client.get_ticket(123)
    assert ticket == {"id": 123, "summary": "Test Ticket"}
    client.session.get.assert_called_once()

def test_get_ticket_not_found(client):
    mock_response = MagicMock()
    mock_response.status_code = 404
    client.session.get.return_value = mock_response

    with pytest.raises(TicketNotFoundError):
        client.get_ticket(123)

def test_get_ticket_server_error(client):
    mock_response = MagicMock()
    mock_response.status_code = 500
    # Create an exception with a response object
    exc = requests.exceptions.HTTPError("Internal Server Error")
    exc.response = mock_response
    mock_response.raise_for_status.side_effect = exc
    client.session.get.return_value = mock_response

    with pytest.raises(TicketRequestError):
        client.get_ticket(123)

def test_get_ticket_request_exception(client):
    client.session.get.side_effect = requests.exceptions.RequestException("Connection error")

    with pytest.raises(TicketRequestError):
        client.get_ticket(123)
