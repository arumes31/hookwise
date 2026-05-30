import unittest.mock

def patch_redis():
    mock_redis = unittest.mock.MagicMock()
    mock_redis.get.return_value = None
    return unittest.mock.patch("hookwise.tasks.redis_client", mock_redis)
