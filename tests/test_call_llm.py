import os
from unittest.mock import MagicMock, patch

import requests

from hookwise.utils import call_llm


def test_call_llm_success():
    """Test successful LLM call."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": "This is a response."}

    with patch("requests.post", return_value=mock_response) as mock_post:
        result = call_llm("test prompt")

        assert result == "This is a response."
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["json"]["prompt"] == "test prompt"
        assert kwargs["json"]["model"] == "phi3"


def test_call_llm_http_error():
    """Test LLM call with HTTP error."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("Internal Server Error")

    with patch("requests.post", return_value=mock_response):
        result = call_llm("test prompt")
        assert result is None


def test_call_llm_exception():
    """Test LLM call with general exception (e.g., ConnectionError)."""
    with patch("requests.post", side_effect=requests.exceptions.ConnectionError("Connection refused")):
        result = call_llm("test prompt")
        assert result is None


def test_call_llm_custom_system_prompt():
    """Test LLM call with a custom system prompt."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": "Custom response"}

    with patch("requests.post", return_value=mock_response) as mock_post:
        call_llm("test prompt", system_prompt="Custom system prompt")

        kwargs = mock_post.call_args.kwargs
        assert kwargs["json"]["system"] == "Custom system prompt"


def test_call_llm_env_vars():
    """Test LLM call respects environment variables."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": "Response"}

    env_patch = {"OLLAMA_HOST": "http://custom-host:11434", "LLM_MAX_TOKENS": "128", "LLM_TIMEOUT": "10"}

    with patch.dict(os.environ, env_patch), patch("requests.post", return_value=mock_response) as mock_post:
        call_llm("test prompt")

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        kwargs = mock_post.call_args[1]

        assert url == "http://custom-host:11434/api/generate"
        assert kwargs["json"]["options"]["num_predict"] == 128
        assert kwargs["timeout"] == 10


def test_call_llm_malformed_json():
    """Test LLM call when response JSON is malformed or missing expected keys."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"not_response": "Value"}

    with patch("requests.post", return_value=mock_response):
        # response.json().get("response", "").strip()
        # .get("response", "") returns ""
        # "".strip() returns ""
        result = call_llm("test prompt")
        assert result == ""
