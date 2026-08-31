"""Encrypt legacy plaintext OTP secrets.

Revision ID: c8e3f1a2b5d6
Revises: a6c1d9e2f4b7
"""

import base64
import binascii
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, InvalidToken

revision: str = "c8e3f1a2b5d6"
down_revision: str | None = "a6c1d9e2f4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_legacy_otp_secret(value: str) -> bool:
    normalized = value.strip().upper()
    if not 16 <= len(normalized) <= 128 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for character in normalized
    ):
        return False
    try:
        return bool(base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True))
    except binascii.Error, ValueError:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    records = list(bind.execute(sa.text('SELECT id, otp_secret FROM "user" WHERE otp_secret IS NOT NULL')).mappings())
    if not records:
        return
    raw_key = os.environ.get("ENCRYPTION_KEY")
    if not raw_key:
        raise RuntimeError("ENCRYPTION_KEY is required to migrate stored OTP secrets")
    fernet = Fernet(raw_key.encode())
    for row in records:
        value = str(row["otp_secret"])
        try:
            fernet.decrypt(value.encode())
            continue
        except InvalidToken:
            if value.startswith("gAAAA"):
                raise RuntimeError(
                    f"Stored OTP secret for user {row['id']} cannot be decrypted; "
                    "restore the ENCRYPTION_KEY used when 2FA was enabled"
                ) from None
            if not _is_legacy_otp_secret(value):
                raise RuntimeError(
                    f"Stored OTP secret for user {row['id']} is not a valid legacy Base32 seed"
                ) from None
        bind.execute(
            sa.text('UPDATE "user" SET otp_secret = :otp_secret WHERE id = :id'),
            {"id": row["id"], "otp_secret": fernet.encrypt(value.encode()).decode()},
        )


def downgrade() -> None:
    # Encryption is intentionally irreversible during schema downgrade.
    pass
