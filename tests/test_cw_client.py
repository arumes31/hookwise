from unittest.mock import MagicMock, patch

import pytest
import requests

from hookwise.client import ConnectWiseClient, TicketNotFoundError, TicketRequestError


@pytest.fixture
def cw_client():
    with patch.dict("os.environ", {
        "CW_URL": "https://test.connectwise.com",
        "CW_COMPANY": "testco",
        "CW_PUBLIC_KEY": "public",
        "CW_PRIVATE_KEY": "private",
        "CW_CLIENT_ID": "clientid"
    }):
        return ConnectWiseClient()

def test_find_open_ticket_error(cw_client):
    cw_client.session.get = MagicMock(side_effect=requests.exceptions.RequestException("API Error"))
    result = cw_client.find_open_ticket("test summary")
    assert result is None

def test_get_ticket_error(cw_client):
    cw_client.session.get = MagicMock(side_effect=requests.exceptions.RequestException("API Error"))
    with pytest.raises(TicketRequestError):
        cw_client.get_ticket(123)

def test_get_ticket_404_error(cw_client):
    # Create an exception with a response object having status_code 404
    mock_response = MagicMock()
    mock_response.status_code = 404
    error = requests.exceptions.RequestException("Not Found")
    error.response = mock_response

    cw_client.session.get = MagicMock(side_effect=error)
    with pytest.raises(TicketNotFoundError):
        cw_client.get_ticket(123)

def test_create_ticket_error(cw_client):
    cw_client.session.post = MagicMock(side_effect=requests.exceptions.RequestException("API Error"))
    result = cw_client.create_ticket("summary", "desc", "monitor")
    assert result is None

def test_close_ticket_error(cw_client):
    cw_client.session.patch = MagicMock(side_effect=requests.exceptions.RequestException("API Error"))
    with pytest.raises(TicketRequestError):
        cw_client.close_ticket(123, "resolution")

def test_close_ticket_404_error(cw_client):
    mock_response = MagicMock()
    mock_response.status_code = 404
    error = requests.exceptions.RequestException("Not Found")
    error.response = mock_response

    cw_client.session.patch = MagicMock(side_effect=error)
    with pytest.raises(TicketNotFoundError):
        cw_client.close_ticket(123, "resolution")

def test_close_ticket_note_error(cw_client):
    # Status update succeeds, but adding note fails
    mock_response_ok = MagicMock()
    mock_response_ok.status_code = 200
    mock_response_ok.ok = True

    cw_client.session.patch = MagicMock(return_value=mock_response_ok)
    cw_client.session.post = MagicMock(side_effect=requests.exceptions.RequestException("Note Error"))

    result = cw_client.close_ticket(123, "resolution")
    assert result is True  # Should still return True as status was updated

def test_add_ticket_note_error(cw_client):
    cw_client.session.post = MagicMock(side_effect=requests.exceptions.RequestException("API Error"))
    result = cw_client.add_ticket_note(123, "note text")
    assert result is False

def test_get_lists_error(cw_client):
    cw_client.session.get = MagicMock(side_effect=requests.exceptions.RequestException("API Error"))

    assert cw_client.get_boards() == []
    assert cw_client.get_priorities() == []
    assert cw_client.get_board_statuses(1) == []
    assert cw_client.get_board_types(1) == []
    assert cw_client.get_board_subtypes(1) == []
    assert cw_client.get_board_items(1) == []
    assert cw_client.get_companies() == []
