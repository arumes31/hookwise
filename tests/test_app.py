from unittest.mock import ANY, patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig
from hookwise.tasks import handle_webhook_logic


@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@pytest.fixture
def sample_config(app, client):
    with app.app_context():
        config = WebhookConfig(
            name="Test Config",
            bearer_token="test-token",
            customer_id_default="TESTCO",
            board="Test Board"
        )
        db.session.add(config)
        db.session.commit()
        return config.id

@patch('hookwise.api.redis_client.ping')
@patch('hookwise.api.cw_client')
def test_health(mock_cw, mock_ping, client):
    """Test the health endpoint."""
    mock_ping.return_value = True
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json['status'] == "ok"

@patch('hookwise.api.cw_client')
def test_metrics(mock_cw, client):
    """Test the metrics endpoint."""
    response = client.get('/metrics')
    assert response.status_code == 200
    assert b"hookwise_webhooks_total" in response.data

@patch('hookwise.webhook.process_webhook_task.delay')
def test_dynamic_webhook_queues_task(mock_delay, client, sample_config):
    """Test that the dynamic webhook queues the celery task."""
    payload = {
        "heartbeat": {"status": 0},
        "monitor": {"name": "Test Monitor"},
        "msg": "Test"
    }
    
    headers = {'Authorization': 'Bearer test-token'}
    response = client.post(f'/w/{sample_config}', json=payload, headers=headers)
    
    assert response.status_code == 202
    assert response.json['status'] == "queued"
    mock_delay.assert_called_once_with(sample_config, payload, ANY, source_ip=ANY, headers=ANY)

def test_dynamic_webhook_unauthorized(client, sample_config):
    """Test that unauthorized webhook fails."""
    response = client.post(f'/w/{sample_config}', json={"test": "data"})
    assert response.status_code == 401

@patch('hookwise.tasks.redis_client')
@patch('hookwise.tasks.cw_client')
def test_handle_webhook_logic_with_company_id_extraction(mock_cw, mock_redis, app, sample_config):
    """Test extraction of #CW company identifier."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.return_value = {"id": 123}
    
    data = {
        "monitor": {"name": "Test Monitor #CWCOMPANY_ABC"},
        "msg": "Test"
    }
    
    handle_webhook_logic(sample_config, data, "req-123")
    
    # Should call create_ticket with extracted company identifier
    mock_cw.create_ticket.assert_called_once()
    kwargs = mock_cw.create_ticket.call_args.kwargs
    assert kwargs['company_id'] == "COMPANY_ABC"
    assert kwargs['board'] == "Test Board"

@patch('hookwise.api.redis_client.ping')
@patch('hookwise.tasks.celery.control.inspect')
def test_health_services(mock_inspect, mock_ping, client):
    """Test the detailed health services endpoint."""
    mock_ping.return_value = True
    mock_inspect.return_value.stats.return_value = {"worker1": {}}
    
    response = client.get('/health/services')
    assert response.status_code == 200
    assert response.json['redis'] == "up"
    assert response.json['database'] == "up"
    assert response.json['celery'] == "up"

@patch('hookwise.tasks.redis_client')
@patch('hookwise.tasks.cw_client')
def test_last_seen_at_updates(mock_cw, mock_redis, app, sample_config):
    """Test that last_seen_at is updated when a webhook is processed."""
    data = {"heartbeat": {"status": 0}}
    handle_webhook_logic(sample_config, data, "req-1")
    
    with app.app_context():
        # Re-fetch config to see updates
        config = WebhookConfig.query.get(sample_config)
        assert config.last_seen_at is not None
