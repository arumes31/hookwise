import logging

from app import app
from hookwise.extensions import redis_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def clear_cw_cache() -> None:
    with app.app_context():
        # Pattern match for dynamic keys
        try:
            for key in redis_client.scan_iter("hookwise_cw_*"):
                key_str = key.decode("utf-8")
                logger.info(f"Deleting cache key: {key_str}")
                redis_client.delete(key)
            logger.info("ConnectWise API cache cleared successfully.")
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")


if __name__ == "__main__":
    clear_cw_cache()
