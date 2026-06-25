import base64
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from hookwise.client import ConnectWiseClient, TicketNotFoundError, TicketRequestError


@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {
        "CW_URL": "https://api-test.com",
        "CW_COMPANY": "test-company",
        "CW_PUBLIC_KEY": "public-key",
        "CW_PRIVATE_KEY": "private-key",
        "CW_CLIENT_ID": "client-id",
        "CW_SERVICE_BOARD": "Test Board",
        "CW_STATUS_NEW": "New Status",
        "CW_STATUS_CLOSED": "Closed Status",
        "CW_DEFAULT_COMPANY_ID": "DEFAULT-CO"
    }):
        yield

@pytest.fixture
def client(mock_env):
    return ConnectWiseClient()

def test_init(mock_env):
    client = ConnectWiseClient()
    assert client.base_url == "https://api-test.com"
    assert client.company == "test-company"
    assert client.public_key == "public-key"
    assert client.private_key == "private-key"
    assert client.client_id == "client-id"
    assert client.service_board_name == "Test Board"
    assert client.status_new == "New Status"
    assert client.status_closed == "Closed Status"

def test_get_headers(mock_env):
    client = ConnectWiseClient()
    headers = client._get_headers()

    auth_string = "test-company+public-key:private-key"
    expected_auth = f"Basic {base64.b64encode(auth_string.encode()).decode()}"

    assert headers["Authorization"] == expected_auth
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert headers["clientId"] == "client-id"

def test_get_headers_missing_creds():
    with patch.dict(os.environ, {}, clear=True):
        client = ConnectWiseClient()
        client.company = None
        assert client._get_headers() == {}

@patch("requests.Session.get")
def test_find_open_ticket_success(mock_get, client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{"id": 123, "summary": "Test Ticket"}]
    mock_get.return_value = mock_response

    result = client.find_open_ticket("Test")

    assert result == {"id": 123, "summary": "Test Ticket"}
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "conditions" in kwargs["params"]
    assert "summary contains 'Test'" in kwargs["params"]["conditions"]

@patch("requests.Session.get")
def test_find_open_ticket_none_found(mock_get, client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = []
    mock_get.return_value = mock_response

    result = client.find_open_ticket("Test")
    assert result is None

@patch("requests.Session.get")
def test_find_open_ticket_error(mock_get, client):
    mock_get.side_effect = requests.exceptions.RequestException("API Error")
    result = client.find_open_ticket("Test")
    assert result is None

@patch("requests.Session.get")
def test_get_ticket_success(mock_get, client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": 123}
    mock_get.return_value = mock_response

    result = client.get_ticket(123)
    assert result == {"id": 123}

@patch("requests.Session.get")
def test_get_ticket_not_found(mock_get, client):
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_get.return_value = mock_response

    # raise_for_status doesn't actually get called if we just set status_code,
    # but the code checks for 404 explicitly or via exception.
    # Actually client.py does:
    # if response.status_code == 404: raise TicketNotFoundError

    with pytest.raises(TicketNotFoundError):
        client.get_ticket(123)

@patch("requests.Session.get")
def test_get_ticket_request_error(mock_get, client):
    mock_get.side_effect = requests.exceptions.RequestException("Error")
    with pytest.raises(TicketRequestError):
        client.get_ticket(123)

@patch("requests.Session.post")
def test_create_ticket_success(mock_post, client):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": 456}
    mock_post.return_value = mock_response

    result = client.create_ticket("Summary", "Desc", "Monitor")
    assert result == {"id": 456}

    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["summary"] == "Summary"
    assert payload["company"]["identifier"] == "DEFAULT-CO"

@patch("requests.Session.post")
def test_create_ticket_error(mock_post, client):
    mock_post.side_effect = requests.exceptions.RequestException("Error")
    result = client.create_ticket("Summary", "Desc", "Monitor")
    assert result is None

@patch("requests.Session.patch")
@patch("requests.Session.post")
def test_close_ticket_success(mock_post, mock_patch, client):
    mock_patch.return_value.ok = True
    mock_post.return_value.status_code = 201

    result = client.close_ticket(123, "Resolved")
    assert result is True

    mock_patch.assert_called_once()
    patch_payload = mock_patch.call_args.kwargs["json"]
    assert patch_payload[0]["value"] == "Closed Status"

    mock_post.assert_called_once() # For adding the note

@patch("requests.Session.patch")
def test_close_ticket_not_found(mock_patch, client):
    mock_patch.return_value.status_code = 404
    with pytest.raises(TicketNotFoundError):
        client.close_ticket(123, "Resolved")

@patch("requests.Session.post")
def test_add_ticket_note_success(mock_post, client):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_post.return_value = mock_response

    result = client.add_ticket_note(123, "Test Note")
    assert result is True
    mock_post.assert_called_once()

@patch("requests.Session.get")
def test_get_boards(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": 1}]
    assert client.get_boards() == [{"id": 1}]

@patch("requests.Session.get")
def test_get_companies(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": "CO1"}]
    assert client.get_companies("search") == [{"id": "CO1"}]
    args, kwargs = mock_get.call_args
    assert "conditions" in kwargs["params"]
    assert "CO1" in str(client.get_companies("CO1"))


@patch("requests.Session.get")
def test_get_priorities(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": 1, "name": "P1"}]
    assert client.get_priorities() == [{"id": 1, "name": "P1"}]

@patch("requests.Session.get")
def test_get_board_statuses(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": 1, "name": "New"}]
    assert client.get_board_statuses(1) == [{"id": 1, "name": "New"}]

@patch("requests.Session.get")
def test_get_board_types(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": 1, "name": "Type"}]
    assert client.get_board_types(1) == [{"id": 1, "name": "Type"}]

@patch("requests.Session.get")
def test_get_board_subtypes(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": 1, "name": "Subtype"}]
    assert client.get_board_subtypes(1) == [{"id": 1, "name": "Subtype"}]

@patch("requests.Session.get")
def test_get_board_items(mock_get, client):
    mock_get.return_value.json.return_value = [{"id": 1, "name": "Item"}]
    assert client.get_board_items(1) == [{"id": 1, "name": "Item"}]
