from hookwise.extensions import redis_client
from app import app
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def clear_cw_cache():
    with app.app_context():
        keys = [
            "hookwise_cw_boards",
            "hookwise_cw_priorities",
            "hookwise_cw_companies_default"
        ]
        # Also need to find wildcard keys for statuses, types, subtypes, items if possible
        # Redis SCAN is better but for now let's try to list specific ones or flush all if acceptable?
        # Flushing all might lose session data if stored in Redis? Flask-Session usually uses Redis.
        # Let's stick to known keys and maybe use keys() pattern if needed.
        
        # Pattern match for dynamic keys
        try:
            for key in redis_client.scan_iter("hookwise_cw_*"):
                key_str = key.decode('utf-8')
                logger.info(f"Deleting cache key: {key_str}")
                redis_client.delete(key)
            logger.info("ConnectWise API cache cleared successfully.")
        except Exception as e:
            logger.error(f"Error clearing cache: {e}")

if __name__ == "__main__":
    clear_cw_cache()
