import pyotp
import pytest
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User
from hookwise.utils import decrypt_string, encrypt_string

@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["ENCRYPTION_KEY"] = "8_I5vGgB2kZ3T6q9xWp-1uY4sN7rL0cI2mP5vA8eK1M="
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
    # Need to patch in all modules that might use it
    with patch("hookwise.tasks.redis_client") as m1,          patch("hookwise.api.redis_client") as m2,          patch("hookwise.extensions.redis_client") as m3:
        m1.get.return_value = None
        m2.get.return_value = None
        m3.get.return_value = None
        yield (m1, m2, m3)

def test_otp_secret_is_encrypted_in_db(client, app):
    """Verify that OTP secret is encrypted when saved and decrypted when read."""
    with app.app_context():
        # Setup a user
        u = User(username="testuser", password_hash=generate_password_hash("testpass"))
        db.session.add(u)
        db.session.commit()

        # Login
        client.post("/login", data={"username": "testuser", "password": "testpass"})

        # Enable 2FA
        # 1. Get the secret from session (simulating setup_2fa GET)
        with client.session_transaction() as sess:
            secret = pyotp.random_base32()
            sess["pending_otp_secret"] = secret
            sess["user_id"] = u.id

        # 2. Verify with OTP
        totp = pyotp.TOTP(secret)
        otp = totp.now()
        client.post("/settings/2fa/setup", data={"otp": otp})

        # Check database
        user_in_db = User.query.filter_by(username="testuser").first()
        assert user_in_db.otp_secret != secret
        assert decrypt_string(user_in_db.otp_secret) == secret

        # Verify authentication still works
        # Logout first
        client.get("/logout")

        # Login with credentials -> should get 2FA prompt
        resp = client.post("/login", data={"username": "testuser", "password": "testpass"})
        assert b"Verify Code" in resp.data

        # Submit OTP
        resp = client.post("/login", data={"otp": otp}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Logout" in resp.data

def test_backward_compatibility(client, app):
    """Verify that plaintext secrets still work (for migration period)."""
    with app.app_context():
        secret = pyotp.random_base32()
        u = User(username="legacyuser", password_hash=generate_password_hash("testpass"))
        u.is_2fa_enabled = True
        u.otp_secret = secret # Plaintext
        db.session.add(u)
        db.session.commit()

        # Login
        client.post("/login", data={"username": "legacyuser", "password": "testpass"})

        # Verify OTP
        totp = pyotp.TOTP(secret)
        otp = totp.now()
        resp = client.post("/login", data={"otp": otp}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Logout" in resp.data
