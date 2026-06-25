import os
import time
from unittest.mock import patch

import pytest
from flask import json

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User, WebhookConfig


@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock


@pytest.fixture
def app():
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    with patch("hookwise.tasks.redis_client"):
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        yield app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        # Create a test user for auth
        user = User(username="testuser", password_hash="hash")
        db.session.add(user)
        db.session.commit()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = user.id
                sess["username"] = user.username
                sess["role"] = "admin"
            yield client
        db.session.remove()
        db.drop_all()


@patch("hookwise.tasks.redis_client")
def test_reorder_performance(mock_redis, client, app):
    mock_redis.get.return_value = None
    num_endpoints = 100
    endpoint_ids = []

    with app.app_context():
        for i in range(num_endpoints):
            config = WebhookConfig(name=f"Config {i}", bearer_token=f"token-{i}")
            db.session.add(config)
        db.session.commit()

        # Get IDs in order
        configs = WebhookConfig.query.all()
        endpoint_ids = [c.id for c in configs]

    # Reverse the order for the request
    new_order = endpoint_ids[::-1]

    start_time = time.time()
    response = client.post("/endpoint/reorder", data=json.dumps({"order": new_order}), content_type="application/json")
    end_time = time.time()

    assert response.status_code == 200
    duration = end_time - start_time
    print(f"\nReorder {num_endpoints} endpoints took: {duration:.4f} seconds")

    # Verify order was updated with a bulk fetch
    with app.app_context():
        configs = WebhookConfig.query.filter(WebhookConfig.id.in_(new_order)).all()
        config_map = {c.id: c for c in configs}
        for i, config_id in enumerate(new_order):
            assert config_map[config_id].display_order == i
