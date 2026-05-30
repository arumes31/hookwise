from unittest.mock import patch

from hookwise.tasks import run_llm_rca


def test_run_llm_rca_success():
    """Test successful LLM RCA response."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.return_value = "Test RCA Response"
        result = run_llm_rca("config-123", {"key": "val"}, None)
        assert result == {"status": "ok", "rca": "Test RCA Response"}
        mock_call.assert_called_once()


def test_run_llm_rca_no_response():
    """Test LLM RCA with no response."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.return_value = None
        result = run_llm_rca("config-123", {"key": "val"}, None)
        assert result["status"] == "error"
        assert "no response" in result["rca"]


def test_run_llm_rca_exception():
    """Test LLM RCA when an exception occurs."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.side_effect = Exception("Connection error")
        result = run_llm_rca("config-123", {"key": "val"}, "Custom Prompt")
        assert result == {"status": "error", "rca": "LLM error: Exception"}
