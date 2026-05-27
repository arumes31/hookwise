from unittest.mock import patch

from hookwise.tasks import run_llm_rca


def test_run_llm_rca_success():
    payload = {"key": "value"}
    config_id = "test-config"
    ai_prompt_template = "Custom prompt"

    with patch("hookwise.utils.call_llm") as mock_call_llm:
        mock_call_llm.return_value = "Possible root cause: CPU spike"

        result = run_llm_rca(config_id, payload, ai_prompt_template)

        assert result["status"] == "ok"
        assert result["rca"] == "Possible root cause: CPU spike"
        mock_call_llm.assert_called_once()

def test_run_llm_rca_no_response():
    payload = {"key": "value"}
    config_id = "test-config"
    ai_prompt_template = None

    with patch("hookwise.utils.call_llm") as mock_call_llm:
        mock_call_llm.return_value = None

        result = run_llm_rca(config_id, payload, ai_prompt_template)

        assert result["status"] == "error"
        assert "no response" in result["rca"]

def test_run_llm_rca_exception():
    payload = {"key": "value"}
    config_id = "test-config"
    ai_prompt_template = None

    with patch("hookwise.utils.call_llm") as mock_call_llm,          patch("hookwise.tasks.logger") as mock_logger:

        mock_call_llm.side_effect = Exception("Connection refused")

        result = run_llm_rca(config_id, payload, ai_prompt_template)

        assert result["status"] == "error"
        assert result["rca"] == "LLM error: Exception"
        mock_logger.error.assert_called_once()
