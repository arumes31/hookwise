from unittest.mock import MagicMock, patch

import requests

from hookwise.utils import call_llm


def test_call_llm_success():
    """Test call_llm returns the response content on success."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": "This is a summary."}

    with patch("requests.post", return_value=mock_response) as mock_post:
        result = call_llm("Summarize this ticket", "System prompt")

        assert result == "This is a summary."
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["json"]["prompt"] == "Summarize this ticket"
        assert kwargs["json"]["system"] == "System prompt"


def test_call_llm_failure_status_code():
    """Test call_llm returns None when requests returns a non-200 status code."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("Internal Server Error")

    with patch("requests.post", return_value=mock_response):
        result = call_llm("test prompt")
        assert result is None


def test_call_llm_exception():
    """Test call_llm returns None when requests raises an exception."""
    with patch("requests.post", side_effect=requests.exceptions.ConnectionError("Connection failed")):
        result = call_llm("test prompt")
        assert result is None


def test_call_llm_empty_response():
    """Test call_llm returns an empty string if the response field is missing or empty."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}

    with patch("requests.post", return_value=mock_response):
        result = call_llm("test prompt")
        assert result == ""


def test_call_llm_trims_response():
    """Test call_llm trims whitespace from the response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": "  trimmed response  "}

    with patch("requests.post", return_value=mock_response):
        result = call_llm("test prompt")
        assert result == "trimmed response"
