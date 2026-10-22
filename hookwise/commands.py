import logging
import os
from typing import cast

import click
from flask import Flask, current_app
from flask.cli import with_appcontext

from .extensions import redis_client

logger = logging.getLogger(__name__)


@click.command("bootstrap-admin")
@with_appcontext
def bootstrap_admin_command() -> None:
    """Create or explicitly rotate the configured bootstrap administrator."""
    from . import _init_db_data

    _init_db_data(cast(Flask, current_app))
    click.echo("Administrator bootstrap completed.")


@click.command("clear-cw-cache")
@with_appcontext
def clear_cw_cache_command() -> None:
    """Clear ConnectWise API cache from Redis."""
    try:
        # Scan for keys starting with hookwise_cw_
        count = 0
        for key in redis_client.scan_iter("hookwise_cw_*"):
            redis_client.delete(key)
            count += 1

        click.echo(f"Successfully cleared {count} ConnectWise API cache keys.")
        logger.info(f"Cleared {count} ConnectWise API cache keys via CLI.")
    except Exception as e:
        click.echo(f"Error clearing cache: {e}", err=True)
        logger.error(f"Error clearing cache via CLI: {e}")


@click.command("rotate-encryption-key")
@with_appcontext
def rotate_encryption_key_command() -> None:
    """Re-encrypt stored secrets from OLD_ENCRYPTION_KEY to ENCRYPTION_KEY.

    Covers endpoint bearer tokens, HMAC secrets and TOTP seeds. Stop the web
    and worker containers first; existing backup files keep the old key.
    """
    from cryptography.fernet import Fernet, InvalidToken, MultiFernet
    from sqlalchemy import select, update

    from .extensions import db
    from .models import User, WebhookConfig
    from .utils import get_fernet

    old_key = os.environ.get("OLD_ENCRYPTION_KEY")
    if not old_key:
        raise click.UsageError("Set OLD_ENCRYPTION_KEY to the key the stored secrets were encrypted with.")
    try:
        old_fernet = Fernet(old_key.strip().encode())
    except (ValueError, TypeError) as exc:
        raise click.UsageError(f"OLD_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc
    # get_fernet() validates ENCRYPTION_KEY, including the published-key check.
    rotator = MultiFernet([get_fernet(), old_fernet])

    rotated = 0
    try:
        for model, columns in ((User, ("otp_secret",)), (WebhookConfig, ("bearer_token", "hmac_secret"))):
            rows = db.session.execute(select(model.id, *[getattr(model, column) for column in columns])).all()
            for row_id, *values in rows:
                changes = {
                    column: rotator.rotate(value.encode()).decode()
                    for column, value in zip(columns, values, strict=True)
                    if value
                }
                if changes:
                    # Core UPDATE: the ORM validators would re-encrypt with the
                    # new key only and cannot tell old ciphertext from plaintext.
                    db.session.execute(update(model).where(model.id == row_id).values(**changes))
                    rotated += len(changes)
        db.session.commit()
    except InvalidToken as exc:
        db.session.rollback()
        raise click.ClickException(
            "A stored secret matches neither key; nothing was changed. Check OLD_ENCRYPTION_KEY."
        ) from exc
    click.echo(f"Re-encrypted {rotated} stored secrets with the current ENCRYPTION_KEY.")
    logger.info("Rotated encryption key for %d stored secrets via CLI.", rotated)
