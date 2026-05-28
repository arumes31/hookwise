from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["WTF_CSRF_ENABLED"] = False
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@patch("hookwise.tasks.redis_client")
@patch("hookwise.metrics.redis_client")
@patch("hookwise.webhook.process_webhook_task.delay")
def test_webhook_ip_whitelist(mock_delay, mock_metrics_redis, mock_tasks_redis, client, app):
    with app.app_context():
        config = WebhookConfig(
            name="IP Restricted",
            trusted_ips="192.168.1.0/24, 10.0.0.1",
            is_enabled=True,
            bearer_auth_enabled=False
        )
        db.session.add(config)
        db.session.commit()
        config_id = config.id

    payload = {"test": "data"}

    # Test allowed IP (subnet)
    response = client.post(f"/w/{config_id}", json=payload, environ_base={'REMOTE_ADDR': '192.168.1.5'})
    assert response.status_code == 202

    # Test allowed IP (exact)
    response = client.post(f"/w/{config_id}", json=payload, environ_base={'REMOTE_ADDR': '10.0.0.1'})
    assert response.status_code == 202

    # Test denied IP
    response = client.post(f"/w/{config_id}", json=payload, environ_base={'REMOTE_ADDR': '192.168.2.1'})
    assert response.status_code == 403
    assert "not allowed" in response.json["message"]

@patch("hookwise.utils.ipaddress.ip_network")
def test_ip_network_caching(mock_ip_network, app):
    from hookwise.utils import parse_ip_network

    # Reset cache for test predictability
    parse_ip_network.cache_clear()

    network_str = "192.168.1.0/24"
    mock_ip_network.return_value = "mock_network_object"

    # First call
    result1 = parse_ip_network(network_str)
    assert result1 == "mock_network_object"
    assert mock_ip_network.call_count == 1

    # Second call with same string
    result2 = parse_ip_network(network_str)
    assert result2 == "mock_network_object"
    assert mock_ip_network.call_count == 1  # Should still be 1 due to caching

    # Call with different string
    network_str2 = "10.0.0.0/8"
    parse_ip_network(network_str2)
    assert mock_ip_network.call_count == 2
