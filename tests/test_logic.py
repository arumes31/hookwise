import json
import os
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.client import ConfigurationRequestError, TicketCreationOutcomeUnknown, TicketCreationRejected
from hookwise.extensions import db
from hookwise.models import TicketOperation, WebhookConfig, WebhookLog
from hookwise.services.ticket_operations import TicketOperationInProgress
from hookwise.tasks import handle_webhook_logic
from hookwise.utils import CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY, resolve_jsonpath


@pytest.fixture
def app():
    import tempfile

    # Use a unique temporary file for the sqlite database to ensure process isolation
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_hookwise_")
    os.close(fd)

    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        with patch("hookwise.tasks._app", app):
            yield app
        db.session.remove()
        db.drop_all()

    # Dispose engine to close all connections and release the file lock on Windows
    with app.app_context():
        db.engine.dispose()

    if os.path.exists(path):
        try:
            os.remove(path)
        except PermissionError:
            pass  # Fallback for Windows lock issues in some environments


@pytest.fixture
def client(app):
    return app.test_client()


def test_resolve_jsonpath():
    data = {"status": "down", "monitor": {"name": "Test Server"}, "details": [{"id": 1, "msg": "Error"}]}
    assert resolve_jsonpath(data, "$.status") == "down"
    assert resolve_jsonpath(data, "$.monitor.name") == "Test Server"
    assert resolve_jsonpath(data, "$.details[0].msg") == "Error"
    assert resolve_jsonpath(data, "$.invalid") is None


GREENBONE_DESCRIPTION = """Site2Nite Boat Classifieds Multiple SQLi Vulnerabilities - Active Check

Evidence
Vulnerable URL: http://10.70.10.20:7090/products/boat-webdesign/www/detail.asp?ID=999999

Greenbone context
Customer: eworxRO
Asset: 10.70.10.20:7090/tcp
"""


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_matches_greenbone_ip_with_port(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {
        "id": 42,
        "company": {"id": 321, "identifier": "EWORXRO"},
    }
    mock_cw.find_matching_configurations.return_value = [
        {
            "id": 137,
            "name": "DEXTER",
            "activeFlag": True,
            "company": {"id": 321},
            "ipAddress": "10.70.10.20",
        }
    ]
    mock_cw.is_configuration_attached.return_value = False
    mock_cw.attach_configuration.return_value = {"id": 137}

    with app.app_context():
        config = WebhookConfig(
            name="Greenbone",
            json_mapping=json.dumps({"summary": "$.title", "description": "$.summary"}),
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(
            config.id,
            {
                "title": "Alert: CVE-2010-2687 on 10.70.10.20:7090/tcp [v1:9e2e50a7a]",
                "summary": GREENBONE_DESCRIPTION,
            },
            "req-greenbone-asset",
        )

        mock_cw.find_matching_configurations.assert_called_once_with(321, [("ipAddress", "10.70.10.20")])
        mock_cw.attach_configuration.assert_called_once_with(42, 137)
        log_entry = WebhookLog.query.filter_by(request_id="req-greenbone-asset").one()
        assert log_entry.status == "processed"
        assert log_entry.configuration_link_status == "attached"
        assert log_entry.configuration_id == 137


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_queries_common_mac_address_formats(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 42, "company": {"id": 321}}
    configuration = {
        "id": 137,
        "name": "DEXTER",
        "activeFlag": True,
        "company": {"id": 321},
        "macAddress": "00-15-5D-65-66-88",
    }
    mock_cw.find_matching_configurations.return_value = [configuration]
    mock_cw.is_configuration_attached.return_value = False
    mock_cw.attach_configuration.return_value = {"id": 137}

    with app.app_context():
        config = WebhookConfig(
            name="MAC linking",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"mac_address": "00:15:5D:65:66:88"}, "req-link-mac")

        mock_cw.find_matching_configurations.assert_called_once()
        queried_values = {value for _field, value in mock_cw.find_matching_configurations.call_args.args[1]}
        assert "00155d656688" in queried_values
        assert "00-15-5d-65-66-88" in queried_values
        mock_cw.attach_configuration.assert_called_once_with(42, 137)


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_is_disabled_by_default(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {
        "id": 42,
        "company": {"id": 321, "identifier": "EWORXRO"},
    }

    with app.app_context():
        config = WebhookConfig(name="Disabled linking", board="Test Board", customer_id_default="EWORXRO")
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"asset": "10.70.10.20:7090/tcp"}, "req-link-disabled")

        mock_cw.find_matching_configurations.assert_not_called()
        mock_cw.attach_configuration.assert_not_called()
        log_entry = WebhookLog.query.filter_by(request_id="req-link-disabled").one()
        assert log_entry.configuration_link_status == "disabled"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_open_ticket_deduplication_is_scoped_to_resolved_company(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 45, "company": {"id": 321}}

    with app.app_context():
        config = WebhookConfig(name="Company scoped", board="Test Board", customer_id_default="EWORXRO")
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"monitor": {"name": "Scoped alert"}}, "req-company-scope")

        mock_cw.find_open_ticket.assert_called_once_with(
            "Alert: Scoped alert", close_status=None, company_identifier="EWORXRO"
        )


@patch.dict(os.environ, {"CW_DEFAULT_COMPANY_ID": ""})
@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_remote_ticket_deduplication_is_skipped_without_resolved_company(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = {"id": 999, "company": {"id": 999}}
    mock_cw.create_ticket.return_value = {"id": 45, "company": {"id": 321}}

    with app.app_context():
        config = WebhookConfig(name="No company", board="Test Board")
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"monitor": {"name": "Unscoped alert"}}, "req-no-company-scope")

        mock_cw.find_open_ticket.assert_not_called()
        mock_cw.create_ticket.assert_called_once()


@patch.dict(os.environ, {"CW_DEFAULT_COMPANY_ID": ""})
@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_remote_ticket_close_lookup_is_skipped_without_resolved_company(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None

    with app.app_context():
        config = WebhookConfig(
            name="No company close",
            trigger_field="status",
            open_value="down",
            close_value="up",
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"status": "up"}, "req-no-company-close")

        mock_cw.find_open_ticket.assert_not_called()
        mock_cw.close_ticket.assert_not_called()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_ticket_cache_does_not_collide_for_distinct_company_identifiers(mock_cw, mock_redis, app):
    cache: dict[str, bytes] = {}
    mock_redis.get.side_effect = cache.get
    mock_redis.set.side_effect = lambda key, value, **_kwargs: cache.__setitem__(key, str(value).encode())
    mock_redis.delete.side_effect = lambda key: cache.pop(key, None)
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.side_effect = [
        {"id": 42, "company": {"id": 1, "identifier": "ACME/US"}},
        {"id": 43, "company": {"id": 2, "identifier": "ACME_US"}},
    ]
    mock_cw.get_ticket.return_value = {
        "id": 42,
        "closedFlag": False,
        "status": {"name": "New"},
        "company": {"id": 1, "identifier": "ACME/US"},
    }

    with app.app_context():
        config = WebhookConfig(
            name="Multi-company routing",
            json_mapping=json.dumps({"summary": "$.title", "customer_id": "$.company"}),
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"title": "Same alert", "company": "ACME/US"}, "req-company-a")
        handle_webhook_logic(config.id, {"title": "Same alert", "company": "ACME_US"}, "req-company-b")

        assert mock_cw.create_ticket.call_count == 2
        assert [call.kwargs["company_id"] for call in mock_cw.create_ticket.call_args_list] == [
            "ACME/US",
            "ACME_US",
        ]


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_skips_ambiguous_ip(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 43, "company": {"id": 321}}
    mock_cw.find_matching_configurations.return_value = [
        {"id": 137, "name": "DEXTER", "activeFlag": True, "company": {"id": 321}, "ipAddress": "10.70.10.20"},
        {"id": 138, "name": "OTHER", "activeFlag": True, "company": {"id": 321}, "ipAddress": "10.70.10.20"},
    ]

    with app.app_context():
        config = WebhookConfig(
            name="Ambiguous asset",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"asset": "10.70.10.20:7090/tcp"}, "req-link-ambiguous")

        mock_cw.attach_configuration.assert_not_called()
        log_entry = WebhookLog.query.filter_by(request_id="req-link-ambiguous").one()
        assert log_entry.status == "processed"
        assert log_entry.configuration_link_status == "ambiguous"
        assert log_entry.configuration_id is None


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_enriches_company_scoped_cached_ticket(mock_cw, mock_redis, app):
    def redis_get(key):
        if str(key).endswith(":viable"):
            return None
        if str(key).startswith("hookwise_ticket:"):
            return b"99"
        return None

    mock_redis.get.side_effect = redis_get
    mock_cw.get_ticket.return_value = {
        "id": 99,
        "closedFlag": False,
        "status": {"name": "New"},
        "company": {"id": 321, "identifier": "EWORXRO"},
    }
    mock_cw.find_matching_configurations.return_value = [
        {"id": 137, "name": "DEXTER", "activeFlag": True, "company": {"id": 321}, "ipAddress": "10.70.10.20"}
    ]
    mock_cw.is_configuration_attached.return_value = False
    mock_cw.attach_configuration.return_value = {"id": 137}
    mock_cw.add_ticket_note.return_value = True

    with app.app_context():
        config = WebhookConfig(
            name="Cached Greenbone",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"asset": "10.70.10.20:7090/tcp"}, "req-link-cached")

        ticket_cache_keys = [
            str(call.args[0])
            for call in mock_redis.get.call_args_list
            if call.args
            and str(call.args[0]).startswith("hookwise_ticket:")
            and not str(call.args[0]).endswith(":viable")
        ]
        assert ticket_cache_keys
        company_tokens = {key.split(":", 3)[2] for key in ticket_cache_keys}
        assert len(company_tokens) == 1
        assert all(len(token) == 64 and set(token) <= set("0123456789abcdef") for token in company_tokens)
        mock_cw.create_ticket.assert_not_called()
        mock_cw.attach_configuration.assert_called_once_with(99, 137)
        log_entry = WebhookLog.query.filter_by(request_id="req-link-cached").one()
        assert log_entry.action == "update"
        assert log_entry.configuration_link_status == "attached"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_uses_rendered_description_for_reused_ticket(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = {"id": 42, "company": {"id": 321}}
    mock_cw.find_matching_configurations.return_value = [
        {
            "id": 137,
            "name": "DEXTER",
            "activeFlag": True,
            "company": {"id": 321},
            "ipAddress": "10.70.10.20",
        }
    ]
    mock_cw.is_configuration_attached.return_value = False
    mock_cw.attach_configuration.return_value = {"id": 137}

    with app.app_context():
        config = WebhookConfig(
            name="Description matching",
            description_template="Asset endpoint: {$.details.address}",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(
            config.id,
            {
                "heartbeat": {"status": "0"},
                "monitor": {"name": "Template-only alert"},
                "details": {"address": "10.70.10.20:7090/tcp"},
            },
            "req-link-rendered-description",
        )

        mock_cw.find_matching_configurations.assert_called_once_with(321, [("ipAddress", "10.70.10.20")])
        mock_cw.attach_configuration.assert_called_once_with(42, 137)


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_configuration_attach_failure_does_not_fail_created_ticket(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 44, "company": {"id": 321}}
    mock_cw.find_matching_configurations.return_value = [
        {"id": 137, "name": "DEXTER", "activeFlag": True, "company": {"id": 321}, "ipAddress": "10.70.10.20"}
    ]
    mock_cw.is_configuration_attached.return_value = False
    mock_cw.attach_configuration.side_effect = RuntimeError("association unavailable")

    with app.app_context():
        config = WebhookConfig(
            name="Best effort linking",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"asset": "10.70.10.20:7090/tcp"}, "req-link-error")

        log_entry = WebhookLog.query.filter_by(request_id="req-link-error").one()
        assert log_entry.status == "processed"
        assert log_entry.ticket_id == 44
        assert log_entry.configuration_link_status == "attach_error"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_transport_source_ip_is_never_an_asset_candidate(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 46, "company": {"id": 321}}

    with app.app_context():
        config = WebhookConfig(
            name="Transport IP",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"message": "No asset supplied"}, "req-source-ip", source_ip="10.70.10.20")

        mock_cw.find_matching_configurations.assert_not_called()
        log_entry = WebhookLog.query.filter_by(request_id="req-source-ip").one()
        assert log_entry.configuration_link_status == "no_identifiers"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_auto_link_configuration_does_not_post_existing_association(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 47, "company": {"id": 321}}
    mock_cw.find_matching_configurations.return_value = [
        {"id": 137, "name": "DEXTER", "activeFlag": True, "company": {"id": 321}, "ipAddress": "10.70.10.20"}
    ]
    mock_cw.is_configuration_attached.return_value = True

    with app.app_context():
        config = WebhookConfig(
            name="Existing association",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"asset": "10.70.10.20:7090/tcp"}, "req-link-existing")

        mock_cw.attach_configuration.assert_not_called()
        log_entry = WebhookLog.query.filter_by(request_id="req-link-existing").one()
        assert log_entry.configuration_link_status == "already_attached"
        assert log_entry.configuration_id == 137


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_unknown_attach_outcome_is_reconciled_by_readback(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 48, "company": {"id": 321}}
    mock_cw.find_matching_configurations.return_value = [
        {"id": 137, "name": "DEXTER", "activeFlag": True, "company": {"id": 321}, "ipAddress": "10.70.10.20"}
    ]
    mock_cw.is_configuration_attached.side_effect = [False, True]
    mock_cw.attach_configuration.side_effect = ConfigurationRequestError(
        "association outcome unknown",
        operation="attach",
        retryable=True,
        outcome_unknown=True,
    )

    with app.app_context():
        config = WebhookConfig(
            name="Unknown association outcome",
            board="Test Board",
            customer_id_default="EWORXRO",
            auto_link_configuration_enabled=True,
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"asset": "10.70.10.20:7090/tcp"}, "req-link-unknown")

        assert mock_cw.is_configuration_attached.call_count == 2
        log_entry = WebhookLog.query.filter_by(request_id="req-link-unknown").one()
        assert log_entry.status == "processed"
        assert log_entry.configuration_link_status == "attached"
        assert log_entry.configuration_id == 137


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_logic_with_jsonpath(mock_cw, mock_redis, app):
    """Test that JSON mapping fields are resolved and passed to create_ticket."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 42}

    with app.app_context():
        config = WebhookConfig(
            name="Test Mapping",
            json_mapping=json.dumps({"summary": "$.alert_name", "description": "$.extra_info"}),
            trigger_field="status",
            open_value="down",
            close_value="up",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {
            "status": "down",
            "alert_name": "Mapped Server Down",
            "extra_info": "Detailed error message here",
            "monitor": {"name": "TestMonitor"},
        }

        handle_webhook_logic(config_id, data, "req-mapping-1")

        mock_cw.create_ticket.assert_called_once()
        call_kwargs = mock_cw.create_ticket.call_args.kwargs
        assert "Mapped Server Down" in call_kwargs["summary"]


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_definitive_ticket_rejection_releases_operation_for_a_later_attempt(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.side_effect = TicketCreationRejected(
        "ConnectWise rejected ticket creation (HTTP 400): The field severity is invalid.",
        retryable=False,
    )

    with app.app_context():
        config = WebhookConfig(
            name="Invalid severity",
            trigger_field="status",
            open_value="down",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        with pytest.raises(TicketCreationRejected, match="severity"):
            handle_webhook_logic(config.id, {"status": "down"}, "req-rejected")

        assert TicketOperation.query.count() == 0

        mock_cw.create_ticket.side_effect = None
        mock_cw.create_ticket.return_value = {"id": 46}
        handle_webhook_logic(config.id, {"status": "down"}, "req-rejected")

        operation = TicketOperation.query.one()
        assert operation.status == "completed"
        assert operation.ticket_id == 46


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_unknown_ticket_outcome_keeps_duplicate_guard(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.side_effect = TicketCreationOutcomeUnknown("No response")

    with app.app_context():
        config = WebhookConfig(
            name="Unknown outcome",
            trigger_field="status",
            open_value="down",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        with pytest.raises(TicketCreationOutcomeUnknown):
            handle_webhook_logic(config.id, {"status": "down"}, "req-unknown")

        operation = TicketOperation.query.one()
        assert operation.status == "started"

        mock_cw.create_ticket.reset_mock()
        mock_cw.create_ticket.side_effect = None
        mock_cw.create_ticket.return_value = {"id": 47}
        with pytest.raises(TicketOperationInProgress) as raised:
            handle_webhook_logic(config.id, {"status": "down"}, "req-unknown")

        assert raised.value.retry_after_seconds > 500
        mock_cw.create_ticket.assert_not_called()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_cipp_results_are_inserted_after_jsonpath_substitution(mock_cw, mock_redis, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 43}

    with app.app_context():
        config = WebhookConfig(
            name="CIPP Template",
            description_template="Tenant: {$.Tenant}\n{{ cipp_results }}",
            trigger_field="status",
            open_value="down",
            close_value="up",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        data = {
            "status": "down",
            "Tenant": "example.com",
            "TaskInfo": {"Command": "Get-CIPPAlertDefenderAlerts"},
            "Results": [{"Title": "Literal {$.Tenant}"}],
        }

        handle_webhook_logic(config.id, data, "req-cipp-template")

        description = mock_cw.create_ticket.call_args.kwargs["description"]
        assert "Tenant: example.com" in description
        assert "Title: Literal {$.Tenant}" in description


@patch("hookwise.tasks.format_cipp_results")
@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_description_without_cipp_placeholder_skips_formatter(mock_cw, mock_redis, mock_formatter, app):
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 44}

    with app.app_context():
        config = WebhookConfig(
            name="Standard Template",
            description_template="Tenant: {$.Tenant}",
            trigger_field="status",
            open_value="down",
            close_value="up",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        handle_webhook_logic(config.id, {"status": "down", "Tenant": "example.com"}, "req-standard-template")

        mock_formatter.assert_not_called()
        assert mock_cw.create_ticket.call_args.kwargs["description"] == "Tenant: example.com"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_all_excluded_cipp_certificate_results_skip_ticket(mock_cw, mock_redis, app):
    mock_redis.get.side_effect = lambda key: (
        b"ConnectSyncProvisioning_*" if key == CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY else None
    )
    with app.app_context():
        config = WebhookConfig(
            name="CIPP Certificate Expiry",
            description_template="{{ cipp_results }}",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        data = {
            "TaskInfo": {"Command": "Get-CIPPAlertAppCertificateExpiry"},
            "Results": [{"DisplayName": "ConnectSyncProvisioning_ANAP02_363a343699fd"}],
        }

        handle_webhook_logic(config.id, data, "req-cipp-all-excluded")

        mock_cw.find_open_ticket.assert_not_called()
        mock_cw.create_ticket.assert_not_called()
        log_entry = WebhookLog.query.filter_by(request_id="req-cipp-all-excluded").one()
        assert log_entry.status == "skipped"
        assert log_entry.error_message == "Skipped: All CIPP application results were globally excluded"


@patch("hookwise.metrics.redis_client")
@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_mixed_cipp_certificate_results_only_render_included_apps(mock_cw, mock_redis, mock_metrics_redis, app):
    mock_redis.get.side_effect = lambda key: (
        b"Hornetsecurity 365 Permission Manager Application" if key == CIPP_APP_CERTIFICATE_EXCLUDE_REDIS_KEY else None
    )
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 45}

    with app.app_context():
        config = WebhookConfig(
            name="CIPP Certificate Expiry",
            description_template="{{ cipp_results }}",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()

        data = {
            "TaskInfo": {"Command": "Get-CIPPAlertAppCertificateExpiry"},
            "Results": [
                {"DisplayName": "Hornetsecurity 365 Permission Manager Application", "AppId": "excluded"},
                {"DisplayName": "Customer SAML Application", "AppId": "included"},
            ],
        }

        handle_webhook_logic(config.id, data, "req-cipp-partially-excluded")

        description = mock_cw.create_ticket.call_args.kwargs["description"]
        assert "Customer SAML Application" in description
        assert "Hornetsecurity 365 Permission Manager Application" not in description


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_logic_with_routing_rules(mock_cw, mock_redis, app):
    """Test that routing rule overrides are applied when regex matches."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 99}

    with app.app_context():
        config = WebhookConfig(
            name="Test Routing",
            routing_rules=json.dumps(
                [
                    {
                        "path": "$.severity",
                        "regex": "critical",
                        "overrides": {"board": "Critical Board", "priority": "P1"},
                    }
                ]
            ),
            board="Default Board",
            priority="P3",
            trigger_field="status",
            open_value="down",
            close_value="up",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"status": "down", "severity": "CRITICAL alert!", "monitor": {"name": "ServerX"}}

        handle_webhook_logic(config_id, data, "req-routing-1")

        mock_cw.create_ticket.assert_called_once()
        call_kwargs = mock_cw.create_ticket.call_args.kwargs
        assert call_kwargs["board"] == "Critical Board"
        assert call_kwargs["priority"] == "P1"


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_close_ticket_on_up_signal(mock_cw, mock_redis, app):
    """Test that an UP signal closes an existing ticket."""
    mock_redis.get.return_value = b"42"  # Cached ticket ID
    mock_cw.close_ticket.return_value = True

    with app.app_context():
        config = WebhookConfig(
            name="Test Close",
            trigger_field="heartbeat.status",
            open_value="0",
            close_value="1",
            board="Test Board",
            customer_id_default="TESTCO",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"heartbeat": {"status": "1"}, "monitor": {"name": "TestServer"}, "msg": "UP"}

        handle_webhook_logic(config_id, data, "req-close-1")

        mock_cw.close_ticket.assert_called_once()
        args = mock_cw.close_ticket.call_args
        assert args[0][0] == 42  # ticket_id


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_close_ticket_with_custom_status(mock_cw, mock_redis, app):
    """Test that an UP signal closes a ticket with a custom status name."""
    mock_redis.get.return_value = b"123"
    mock_cw.close_ticket.return_value = True

    with app.app_context():
        config = WebhookConfig(
            name="Test Custom Close",
            trigger_field="status",
            open_value="0",
            close_value="1",
            close_status="Completed",
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"status": "1", "monitor": {"name": "CustomServer"}, "msg": "UP"}
        handle_webhook_logic(config_id, data, "req-custom-close-1")

        mock_cw.close_ticket.assert_called_once()
        call_args = mock_cw.close_ticket.call_args
        assert call_args.args[0] == 123  # ticket_id
        call_kwargs = call_args.kwargs
        assert call_kwargs["status_name"] == "Completed"
        mock_redis.delete.assert_called_once()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_maintenance_window_blocks_processing(mock_cw, mock_redis, app):
    """Test that webhooks during a maintenance window are skipped."""
    import json
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")

    with app.app_context():
        config = WebhookConfig(
            name="Test Maintenance",
            maintenance_windows=json.dumps(
                [{"type": "weekly", "days": [now.strftime("%a")], "start": start, "end": end}]
            ),
            trigger_field="heartbeat.status",
            open_value="0",
            close_value="1",
            board="Test Board",
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        data = {"heartbeat": {"status": "0"}, "monitor": {"name": "MaintServer"}}

        handle_webhook_logic(config_id, data, "req-maint-1")

        # Should NOT create a ticket during maintenance
        mock_cw.create_ticket.assert_not_called()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_webhook_timeout_alerts(mock_cw, mock_redis, app):
    """Test that a timeout triggers a ticket and a new webhook closes it."""
    from datetime import datetime, timedelta, timezone

    from hookwise.tasks import check_webhook_timeouts, handle_webhook_logic

    with app.app_context():
        # 1. Create endpoint with 2-hour timeout
        config = WebhookConfig(
            name="Timeout Test",
            timeout_alerts_enabled=True,
            timeout_hours=2,
            is_enabled=True,
            is_draft=False,
            board="Test Board",
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

        # 2. Run timeout check
        mock_cw.create_ticket.return_value = {"id": 999}
        mock_cw.find_open_ticket.return_value = None  # Ensure it doesn't return a MagicMock
        check_webhook_timeouts()

        # Verify ticket was created
        mock_cw.create_ticket.assert_called_once()
        db.session.refresh(config)
        assert config.timeout_ticket_id == 999

        # 3. Simulate new webhook arrival
        mock_cw.close_ticket.return_value = True
        handle_webhook_logic(config_id, {"status": "ok"}, "request-123")

        # Verify ticket was closed
        from unittest.mock import ANY

        mock_cw.close_ticket.assert_called_once_with(999, ANY, status_name=config.close_status)
        db.session.refresh(config)
        assert config.timeout_ticket_id is None


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_maintenance_window_resolves_timeout(mock_cw, mock_redis, app):
    """Test that a webhook during maintenance still resolves an open timeout alert."""
    from datetime import datetime, timedelta, timezone

    from hookwise.tasks import handle_webhook_logic

    with app.app_context():
        # 1. Create endpoint with an open timeout ticket and a maintenance window
        # Current time is ~14:00, maintenance "12:00-16:00" will cover it
        config = WebhookConfig(
            name="Maint Resolution Test",
            timeout_alerts_enabled=True,
            timeout_ticket_id=888,
            maintenance_windows="12:00-16:00",
            is_enabled=True,
            is_draft=False,
            last_seen_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id
        old_last_seen = config.last_seen_at

        # 2. Simulate webhook arrival during maintenance
        mock_cw.close_ticket.return_value = True
        handle_webhook_logic(config_id, {"status": "ok"}, "maint-req-1")

        # 3. Verify:
        # - Ticket was closed
        mock_cw.close_ticket.assert_called_once()

        # - Config state updated
        db.session.refresh(config)
        assert config.timeout_ticket_id is None
        assert config.last_seen_at > old_last_seen

        # - But data was NOT pushed to CW (normal maintenance behavior)
        mock_cw.create_ticket.assert_not_called()
        mock_cw.find_open_ticket.assert_not_called()
