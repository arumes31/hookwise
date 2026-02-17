import logging

import click
from flask.cli import with_appcontext

from .extensions import redis_client

logger = logging.getLogger(__name__)

@click.command("clear-cw-cache")
@with_appcontext
def clear_cw_cache_command():
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
