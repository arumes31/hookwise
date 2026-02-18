from unittest.mock import patch

import pyotp
import pytest
from werkzeug.security import generate_password_hash

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User


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
    """Mock Redis to avoid connection errors in before_request check_maintenance."""
    with patch("hookwise.tasks.redis_client") as mock:
        # Default behavior: maintenance mode is OFF (None or 'false')
        mock.get.return_value = None
        yield mock


@pytest.fixture
def sample_users(app):
    with app.app_context():
        # Normal user
        u1 = User(username="user1", password_hash=generate_password_hash("pass1"))

        # 2FA user
        secret = pyotp.random_base32()
        u2 = User(username="user2", password_hash=generate_password_hash("pass2"))
        u2.is_2fa_enabled = True
        u2.otp_secret = secret

        db.session.add_all([u1, u2])
        db.session.commit()
        return {"normal": u1, "2fa": u2, "secret": secret}


def test_login_normal(client, sample_users):
    """Test standard login flow."""
    # GET login page
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Login to HookWise" in resp.data

    # POST correct credentials
    resp = client.post("/login", data={"username": "user1", "password": "pass1"}, follow_redirects=True)
    assert resp.status_code == 200
    # Should be on dashboard
    assert b"HookWise" in resp.data
    assert b"Logout" in resp.data


def test_login_2fa_flow(client, sample_users):
    """Test 2FA login flow merged into /login."""
    # user_2fa = sample_users['2fa']  <- Removed unused variable
    secret = sample_users["secret"]

    # 1. Login with credentials
    resp = client.post("/login", data={"username": "user2", "password": "pass2"})

    # CURRENTLY: This redirects to /login/2fa (302)
    # DESIRED: This should render the 2FA form on /login (200) with a specific flag?
    # Actually, to keep it verifiable, let's assume we want to stay on /login (200) and see the OTP field.

    # If the refactor is successful, this should be 200 and contain "Two-Factor Auth" or "Verify Code"
    # But before refactor, this might be 302.
    # Let's write the test for the DESIRED state.

    # NOTE: If we use follow_redirects=True, we can't distinguish between
    # "redirected to /login/2fa" and "stayed on /login".
    # So we check the URL or content.

    assert resp.status_code == 200
    assert b"Verify Code" in resp.data
    # assert b"otp" in resp.data # Input name is still otp, but let's rely on visible text

    # 2. Enter OTP
    totp = pyotp.TOTP(secret)
    code = totp.now()

    resp = client.post("/login", data={"otp": code}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Logout" in resp.data


def test_login_2fa_back_button(client, sample_users):
    """Test the 'Back to login' functionality."""
    # 1. Login with credentials to get to 2FA screen
    client.post("/login", data={"username": "user2", "password": "pass2"})

    # 2. Click 'Back to Login' (which is likely a link to /login with a query param or just /login?reset=1)
    # The current implementation uses a link to main.login.
    # If we are strictly on /login, a GET /login should probably reset the state if we decide so,
    # OR we need a specific action.
    # Let's assume GET /login clears the pending state if it exists, or provided with a param.
    # For now, let's just assert that we can get back to the login form.

    resp = client.get("/login")
    assert b"Login to HookWise" in resp.data
    assert b"USERNAME" in resp.data
