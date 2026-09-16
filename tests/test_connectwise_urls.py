from hookwise.connectwise_urls import connectwise_ticket_url, connectwise_ticket_url_template


def test_ticket_url_uses_configured_api_origin(monkeypatch):
    """Derive the ticket link from the API origin when no web URL is set."""
    monkeypatch.delenv("CW_WEB_URL", raising=False)
    monkeypatch.setenv("CW_URL", "https://psa.example.at/v4_6_release/apis/3.0")

    assert connectwise_ticket_url(405505) == (
        "https://psa.example.at/v4_6_release/services/system_io/Service/fv_sr100_request.rails?service_recid=405505"
    )


def test_ticket_url_prefers_explicit_web_host(monkeypatch):
    """Prefer the explicit PSA browser host over the REST API host."""
    monkeypatch.setenv("CW_URL", "https://api.test.com/v4_6_release/apis/3.0")
    monkeypatch.setenv("CW_WEB_URL", "https://psa.test.com/v4_6_release")

    assert connectwise_ticket_url_template() == (
        "https://psa.test.com/v4_6_release/services/system_io/Service/fv_sr100_request.rails?service_recid={ticket_id}"
    )


def test_ticket_url_rejects_invalid_configuration_and_ids(monkeypatch):
    """Reject unsafe schemes and malformed ticket identifiers."""
    monkeypatch.setenv("CW_WEB_URL", "javascript:alert(1)")

    assert connectwise_ticket_url(405505) == ""
    monkeypatch.setenv("CW_WEB_URL", "https://psa.test.com")
    assert connectwise_ticket_url("../405505") == ""
    assert connectwise_ticket_url(0) == ""
    assert connectwise_ticket_url("1" * 21) == ""
