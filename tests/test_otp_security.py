import pyotp
import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import User
from hookwise.utils import decrypt_string, encrypt_string


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
        yield app.test_client()
        db.session.remove()
        db.drop_all()

def test_otp_secret_is_encrypted_in_db(app, client):
    """Test that OTP secret is stored encrypted in the database."""
    with app.app_context():
        username = "security_test_user"
        plain_secret = pyotp.random_base32()

        # Create user with encrypted secret
        user = User(username=username, password_hash="hash", otp_secret=encrypt_string(plain_secret))
        db.session.add(user)
        db.session.commit()

        # Fetch raw value from DB
        raw_secret = db.session.execute(
            db.select(User.otp_secret).where(User.username == username)
        ).scalar()

        assert raw_secret != plain_secret, "OTP secret MUST NOT be stored in plaintext"
        assert decrypt_string(raw_secret) == plain_secret, "Decrypted secret MUST match original"
