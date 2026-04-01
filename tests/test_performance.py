import time

import pytest
from flask import json

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User, WebhookConfig


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app

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
            yield client
        db.session.remove()
        db.drop_all()

def test_reorder_performance(client, app):
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
    response = client.post("/endpoint/reorder",
                           data=json.dumps({"order": new_order}),
                           content_type='application/json')
    end_time = time.time()

    assert response.status_code == 200
    duration = end_time - start_time
    print(f"\nReorder {num_endpoints} endpoints took: {duration:.4f} seconds")

    # Verify order was updated
    with app.app_context():
        for i, config_id in enumerate(new_order):
            config = WebhookConfig.query.get(config_id)
            assert config.display_order == i
