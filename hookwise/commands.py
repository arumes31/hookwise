import logging
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
