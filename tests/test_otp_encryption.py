from unittest.mock import patch

import pyotp
import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User
from hookwise.utils import decrypt_string


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test_secret_key"
    app.config["ENCRYPTION_KEY"] = "v_mG9kX-7P6Pz8E9jB8z4X2x7j1B4W9z8P6Pz8E9jB8="  # Valid Fernet key
    app.config["GUI_PASSWORD"] = "password"
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
    with patch("hookwise.tasks.redis_client") as mock:
        mock.get.return_value = None
        yield mock


def test_otp_secret_encryption(client, app):
    """Test that OTP secret is stored encrypted in the database."""
    with app.app_context():
        # 1. Create a user and login
        from werkzeug.security import generate_password_hash

        user = User(username="testuser", password_hash=generate_password_hash("testpass"))
        db.session.add(user)
        db.session.commit()

    # Login to set session
    client.post("/login", data={"username": "testuser", "password": "testpass"})

    # 2. Simulate 2FA setup
    # First GET to generate secret
    client.get("/settings/2fa/setup")
    with client.session_transaction() as sess:
        secret = sess.get("pending_otp_secret")
        assert secret is not None

    # Verify the secret with a valid OTP
    totp = pyotp.TOTP(secret)
    otp = totp.now()
    resp = client.post("/settings/2fa/setup", data={"otp": otp}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"2FA has been enabled successfully" in resp.data

    # 3. Verify database storage
    with app.app_context():
        db_user = User.query.filter_by(username="testuser").first()
        stored_secret = db_user.otp_secret

        # The stored secret should NOT be the plaintext secret
        assert stored_secret != secret

        # The stored secret should be decryptable to the plaintext secret
        decrypted_secret = decrypt_string(stored_secret)
        assert decrypted_secret == secret

    # 4. Verify login still works with 2FA
    # Logout first
    client.get("/logout")

    # Initial login
    resp = client.post("/login", data={"username": "testuser", "password": "testpass"})
    assert resp.status_code == 200
    assert b"Verify Code" in resp.data

    # Submit OTP
    totp = pyotp.TOTP(secret)
    otp = totp.now()
    resp = client.post("/login", data={"otp": otp}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Logout" in resp.data


def test_legacy_plaintext_secret_support(client, app):
    """Test that legacy plaintext secrets still work (backward compatibility)."""
    secret = pyotp.random_base32()
    from werkzeug.security import generate_password_hash

    with app.app_context():
        # Manually insert a user with a plaintext OTP secret
        user = User(
            username="legacyuser",
            password_hash=generate_password_hash("legacypass"),
            otp_secret=secret,  # Plaintext
            is_2fa_enabled=True,
        )
        db.session.add(user)
        db.session.commit()

    # Attempt login
    client.post("/login", data={"username": "legacyuser", "password": "legacypass"})

    # Submit OTP (using the plaintext secret)
    totp = pyotp.TOTP(secret)
    otp = totp.now()
    resp = client.post("/login", data={"otp": otp}, follow_redirects=True)

    # Should succeed because decrypt_string returns the original string if decryption fails
    assert resp.status_code == 200
    assert b"Logout" in resp.data
