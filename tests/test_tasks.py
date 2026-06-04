import json
from unittest.mock import patch

from hookwise.tasks import run_llm_rca


def test_run_llm_rca_success():
    payload = {"error": "connection failed"}
    config_id = "test-config"
    ai_prompt_template = "Custom system prompt"
    expected_rca = "The connection failed because of X, Y, Z."

    with patch("hookwise.utils.call_llm") as mock_call_llm:
        mock_call_llm.return_value = expected_rca

        result = run_llm_rca(config_id, payload, ai_prompt_template)

        assert result["status"] == "ok"
        assert result["rca"] == expected_rca

        # Verify call_llm was called with expected arguments
        args, kwargs = mock_call_llm.call_args
        assert "Payload: " + json.dumps(payload) in args[0]
        assert kwargs["system_prompt"] == ai_prompt_template


def test_run_llm_rca_no_response():
    payload = {"error": "timeout"}
    config_id = "test-config"

    with patch("hookwise.utils.call_llm") as mock_call_llm:
        mock_call_llm.return_value = None

        result = run_llm_rca(config_id, payload, None)

        assert result["status"] == "error"
        assert "LLM returned no response" in result["rca"]


def test_run_llm_rca_exception():
    payload = {"error": "unknown"}
    config_id = "test-config"

    with patch("hookwise.utils.call_llm") as mock_call_llm:
        mock_call_llm.side_effect = Exception("LLM is down")

        result = run_llm_rca(config_id, payload, None)

        assert result["status"] == "error"
        assert "LLM error: Exception" in result["rca"]
