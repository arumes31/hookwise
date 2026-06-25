from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app

@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

@pytest.fixture(autouse=True)
def mock_redis():
    with patch("hookwise.tasks.redis_client") as mock_tasks,          patch("hookwise.api.redis_client") as mock_api:
        mock_tasks.get.return_value = None
        mock_api.get.return_value = None
        yield mock_tasks, mock_api

def login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "test_user"
        sess["username"] = "admin"
        sess["role"] = "admin"


def test_llm_test_no_auth(client):
    """Test /admin/llm-test requires authentication."""
    response = client.post("/admin/llm-test", json={"prompt": "hello"})
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

def test_llm_test_invalid_json(client):
    """Test /admin/llm-test with invalid JSON body (not a dict)."""
    login(client)
    # Sending a string instead of a dict
    response = client.post("/admin/llm-test", data="just a string", content_type="application/json")
    assert response.status_code == 400
    assert response.json["status"] == "error"
    assert "JSON body as dictionary is required" in response.json["message"]

def test_llm_test_missing_prompt(client):
    """Test /admin/llm-test with missing prompt."""
    login(client)
    response = client.post("/admin/llm-test", json={})
    assert response.status_code == 400
    assert response.json["status"] == "error"
    assert "Prompt is required" in response.json["message"]

@patch("hookwise.utils.call_llm")
def test_llm_test_call_failed(mock_call, client):
    """Test /admin/llm-test when call_llm returns None."""
    login(client)
    mock_call.return_value = None
    response = client.post("/admin/llm-test", json={"prompt": "hello"})
    assert response.status_code == 500
    assert response.json["status"] == "error"
    assert "LLM call failed" in response.json["message"]

@patch("hookwise.utils.call_llm")
def test_llm_test_success(mock_call, client):
    """Test /admin/llm-test success scenario."""
    login(client)
    mock_call.return_value = "This is a response from LLM"
    response = client.post("/admin/llm-test", json={"prompt": "hello"})
    assert response.status_code == 200
    assert response.json["status"] == "success"
    assert response.json["result"] == "This is a response from LLM"
