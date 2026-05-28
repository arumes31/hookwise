from unittest.mock import patch

from hookwise.tasks import run_llm_rca


def test_run_llm_rca_success():
    """Test run_llm_rca returns ok status when call_llm succeeds."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.return_value = "Everything is fine."
        result = run_llm_rca("config_123", {"key": "value"}, "Template")

        assert result["status"] == "ok"
        assert result["rca"] == "Everything is fine."
        mock_call.assert_called_once()


def test_run_llm_rca_no_response():
    """Test run_llm_rca returns error status when call_llm returns no result."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.return_value = None
        result = run_llm_rca("config_123", {"key": "value"}, None)

        assert result["status"] == "error"
        assert "LLM returned no response" in result["rca"]


def test_run_llm_rca_exception():
    """Test run_llm_rca handles exceptions from call_llm."""
    with patch("hookwise.utils.call_llm") as mock_call:
        mock_call.side_effect = Exception("Connection failed")
        result = run_llm_rca("config_123", {"key": "value"}, None)

        assert result["status"] == "error"
        assert "LLM error: Exception" in result["rca"]
