import base64
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from hookwise.client import (
    ConfigurationRequestError,
    ConnectWiseClient,
    TicketCreationOutcomeUnknown,
    TicketCreationRejected,
    TicketNotFoundError,
    TicketRequestError,
)


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ,
        {
            "CW_URL": "https://api-test.com",
            "CW_COMPANY": "test-company",
            "CW_PUBLIC_KEY": "public-key",
            "CW_PRIVATE_KEY": "private-key",
            "CW_CLIENT_ID": "client-id",
            "CW_SERVICE_BOARD": "Test Board",
            "CW_STATUS_NEW": "New Status",
            "CW_STATUS_CLOSED": "Closed Status",
            "CW_DEFAULT_COMPANY_ID": "DEFAULT-CO",
        },
    ):
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
def test_find_open_ticket_scopes_by_escaped_company_identifier(mock_get, client):
    mock_get.return_value.json.return_value = []

    client.find_open_ticket("Test", company_identifier="O'Malley")

    conditions = mock_get.call_args.kwargs["params"]["conditions"]
    assert "company/identifier = 'O''Malley'" in conditions


@patch("requests.Session.get")
def test_find_open_ticket_preserves_unscoped_query_by_default(mock_get, client):
    mock_get.return_value.json.return_value = []

    client.find_open_ticket("Test")

    conditions = mock_get.call_args.kwargs["params"]["conditions"]
    assert "company/identifier" not in conditions


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
    with pytest.raises(TicketRequestError):
        client.find_open_ticket("Test")


@patch("requests.Session.get")
def test_find_configurations_uses_bounded_company_scoped_exact_query(mock_get, client):
    configurations = [
        {
            "id": 137,
            "name": "DEXTER",
            "company": {"id": 42},
            "ipAddress": "10.70.10.20",
            "activeFlag": True,
        }
    ]
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = configurations

    result = client.find_configurations(42, "ipAddress", "10.70.10.20", page_size=500)

    assert result == configurations
    assert mock_get.call_args.args[0] == "https://api-test.com/company/configurations"
    params = mock_get.call_args.kwargs["params"]
    assert params["conditions"] == ("company/id = 42 AND activeFlag = true AND ipAddress = '10.70.10.20'")
    assert params["pageSize"] == 2
    assert params["fields"] == (
        "id,name,company,deviceIdentifier,serialNumber,macAddress,tagNumber,ipAddress,activeFlag"
    )


@patch("requests.Session.get")
def test_find_configurations_escapes_quoted_values(mock_get, client):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = []

    client.find_configurations(42, "name", "O'Malley")

    conditions = mock_get.call_args.kwargs["params"]["conditions"]
    assert "name = 'O''Malley'" in conditions


@patch("requests.Session.get")
def test_find_configurations_parses_configuration_id_as_positive_integer(mock_get, client):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = []

    client.find_configurations(42, "id", "137")

    conditions = mock_get.call_args.kwargs["params"]["conditions"]
    assert conditions.endswith("id = 137")


@patch("requests.Session.get")
def test_find_matching_configurations_combines_criteria_in_one_bounded_request(mock_get, client):
    configurations = [
        {
            "id": 137,
            "name": "DEXTER",
            "company": {"id": 42},
            "activeFlag": True,
            "ipAddress": "10.70.10.20",
        }
    ]
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = configurations

    result = client.find_matching_configurations(
        42,
        [
            ("ipAddress", "10.70.10.20"),
            ("macAddress", "00-15-5d-65-66-88"),
        ],
    )

    assert result == configurations
    mock_get.assert_called_once()
    params = mock_get.call_args.kwargs["params"]
    assert params["conditions"] == (
        "company/id = 42 AND activeFlag = true AND (ipAddress = '10.70.10.20' OR macAddress = '00-15-5d-65-66-88')"
    )
    assert params["pageSize"] == 129


@pytest.mark.parametrize(
    "searches",
    [
        [],
        [("ipAddress", "10.70.10.20")] * 17,
        [("manufacturer", "Microsoft")],
        [("id", "137 OR 1=1")],
    ],
)
def test_find_matching_configurations_rejects_unsafe_or_unbounded_criteria(client, searches):
    client.session.get = MagicMock()

    with pytest.raises(ValueError):
        client.find_matching_configurations(42, searches)

    client.session.get.assert_not_called()


@pytest.mark.parametrize(
    ("company_id", "field", "value"),
    [
        (0, "ipAddress", "10.70.10.20"),
        (True, "ipAddress", "10.70.10.20"),
        (42, "manufacturer", "Microsoft"),
        (42, "id", "137 OR 1=1"),
        (42, "id", 0),
    ],
)
def test_find_configurations_rejects_unsafe_query_inputs(client, company_id, field, value):
    client.session.get = MagicMock()

    with pytest.raises(ValueError):
        client.find_configurations(company_id, field, value)

    client.session.get.assert_not_called()


@patch("requests.Session.get")
def test_find_configurations_rejects_invalid_provider_shape(mock_get, client):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = [{"id": 137}, "not-an-object"]

    with pytest.raises(ConfigurationRequestError, match="unexpected response") as raised:
        client.find_configurations(42, "ipAddress", "10.70.10.20")

    assert raised.value.operation == "search"
    assert raised.value.retryable is False
    assert raised.value.outcome_unknown is False


@patch("requests.Session.get")
def test_find_configurations_classifies_transient_provider_error(mock_get, client):
    response = MagicMock(status_code=503)
    mock_get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "503 Server Error", response=response
    )

    with pytest.raises(ConfigurationRequestError) as raised:
        client.find_configurations(42, "ipAddress", "10.70.10.20")

    assert raised.value.operation == "search"
    assert raised.value.status_code == 503
    assert raised.value.retryable is True
    assert raised.value.outcome_unknown is False


@pytest.mark.parametrize(("status_code", "expected"), [(200, True), (404, False)])
@patch("requests.Session.get")
def test_is_configuration_attached_reads_exact_association(mock_get, status_code, expected, client):
    mock_get.return_value.status_code = status_code

    result = client.is_configuration_attached(321, 137)

    assert result is expected
    assert mock_get.call_args.args[0] == "https://api-test.com/service/tickets/321/configurations/137"


@patch("requests.Session.get")
def test_is_configuration_attached_classifies_provider_errors(mock_get, client):
    response = MagicMock(status_code=403)
    mock_get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "403 Client Error", response=response
    )

    with pytest.raises(ConfigurationRequestError) as raised:
        client.is_configuration_attached(321, 137)

    assert raised.value.operation == "readback"
    assert raised.value.status_code == 403
    assert raised.value.retryable is False
    assert raised.value.outcome_unknown is False


@pytest.mark.parametrize("status_code", [200, 201])
@patch("requests.Session.post")
def test_attach_configuration_posts_reference_and_returns_validated_object(mock_post, status_code, client):
    association = {"id": 137, "name": "DEXTER"}
    mock_post.return_value.status_code = status_code
    mock_post.return_value.json.return_value = association

    result = client.attach_configuration(321, 137)

    assert result == association
    mock_post.assert_called_once_with(
        "https://api-test.com/service/tickets/321/configurations",
        headers=client.headers,
        json={"id": 137},
        timeout=client.timeout,
    )


@patch("requests.Session.post")
def test_attach_configuration_rejects_invalid_provider_shape(mock_post, client):
    mock_post.return_value.status_code = 201
    mock_post.return_value.json.return_value = [137]

    with pytest.raises(ConfigurationRequestError, match="unexpected response") as raised:
        client.attach_configuration(321, 137)

    assert raised.value.operation == "attach"
    assert raised.value.retryable is False
    assert raised.value.outcome_unknown is True


@patch("requests.Session.post")
def test_attach_configuration_classifies_ambiguous_failure_without_retrying(mock_post, client):
    response = MagicMock(status_code=503)
    mock_post.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "503 Server Error", response=response
    )

    with pytest.raises(ConfigurationRequestError) as raised:
        client.attach_configuration(321, 137)

    assert raised.value.operation == "attach"
    assert raised.value.status_code == 503
    assert raised.value.retryable is True
    assert raised.value.outcome_unknown is True
    mock_post.assert_called_once()


@patch("requests.Session.post")
def test_attach_configuration_classifies_transport_failure_as_unknown_without_retrying(mock_post, client):
    mock_post.side_effect = requests.exceptions.Timeout("timed out")

    with pytest.raises(ConfigurationRequestError) as raised:
        client.attach_configuration(321, 137)

    assert raised.value.operation == "attach"
    assert raised.value.status_code is None
    assert raised.value.retryable is True
    assert raised.value.outcome_unknown is True
    mock_post.assert_called_once()


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
    with pytest.raises(TicketCreationOutcomeUnknown):
        client.create_ticket("Summary", "Desc", "Monitor")


@patch("requests.Session.post")
def test_create_ticket_rejection_exposes_safe_field_error(mock_post, client):
    response = MagicMock(status_code=400, text='{"errors": [{"message": "The field severity is invalid."}]}')
    response.json.return_value = {"errors": [{"message": "The field severity is invalid."}]}
    error = requests.exceptions.HTTPError("400 Client Error", response=response)
    mock_post.return_value.raise_for_status.side_effect = error

    with pytest.raises(TicketCreationRejected, match="HTTP 400.*severity") as raised:
        client.create_ticket("Summary", "Desc", "Monitor")

    assert raised.value.retryable is False


@patch("requests.Session.post")
def test_create_ticket_server_error_preserves_ambiguous_outcome_guard(mock_post, client):
    response = MagicMock(status_code=503, text="Service unavailable")
    response.json.side_effect = ValueError("not JSON")
    error = requests.exceptions.HTTPError("503 Server Error", response=response)
    mock_post.return_value.raise_for_status.side_effect = error

    with pytest.raises(TicketCreationOutcomeUnknown, match="HTTP 503"):
        client.create_ticket("Summary", "Desc", "Monitor")


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

    mock_post.assert_called_once()  # For adding the note


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
