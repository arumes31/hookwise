import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import cleanup_logs


def test_cleanup_logs_all_scenarios():
    """Test all cleanup_logs scenarios in one go to ensure database consistency."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_cleanup_all_")
    os.close(fd)
    uri = f"sqlite:///{path}"

    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": uri,
            "GUI_PASSWORD": "testpassword",
            "SECRET_KEY": "test",
            "ENCRYPTION_KEY": "vmJ34RDpkZk7-sUqAwq0lMA2QN0P0SEAEuC874kov5E=",
        },
    ):
        app = create_app()
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = uri

        with app.app_context():
            db.create_all()

            # Setup config
            config = WebhookConfig(name="Test Config", bearer_token="test-token")
            db.session.add(config)
            db.session.commit()
            cid = config.id

            now = datetime.now(timezone.utc)

            with patch("hookwise.tasks.redis_client") as mock_redis:
                # 1. Test Redis retention (7 days)
                mock_redis.get.return_value = b"7"
                db.session.add_all(
                    [
                        WebhookLog(
                            config_id=cid, request_id="old-redis", payload="{}", created_at=now - timedelta(days=8)
                        ),
                        WebhookLog(
                            config_id=cid, request_id="new-redis", payload="{}", created_at=now - timedelta(days=2)
                        ),
                    ]
                )
                db.session.commit()

                cleanup_logs.run()

                assert WebhookLog.query.filter(WebhookLog.request_id.startswith("old-redis")).count() == 0
                assert WebhookLog.query.filter(WebhookLog.request_id.startswith("new-redis")).count() == 1

                # Clean up for next scenario
                db.session.query(WebhookLog).delete()
                db.session.commit()

                # 2. Test Env fallback (5 days)
                mock_redis.get.return_value = None
                with patch.dict(os.environ, {"LOG_RETENTION_DAYS": "5"}):
                    db.session.add_all(
                        [
                            WebhookLog(
                                config_id=cid, request_id="old-env", payload="{}", created_at=now - timedelta(days=6)
                            ),
                            WebhookLog(
                                config_id=cid, request_id="new-env", payload="{}", created_at=now - timedelta(days=4)
                            ),
                        ]
                    )
                    db.session.commit()
                    cleanup_logs.run()
                    assert WebhookLog.query.filter(WebhookLog.request_id.startswith("old-env")).count() == 0
                    assert WebhookLog.query.filter(WebhookLog.request_id.startswith("new-env")).count() == 1

                # Clean up for next scenario
                db.session.query(WebhookLog).delete()
                db.session.commit()

                # 3. Test Default fallback (30 days)
                mock_redis.get.return_value = None
                old_val = os.environ.pop("LOG_RETENTION_DAYS", None)
                try:
                    db.session.add_all(
                        [
                            WebhookLog(
                                config_id=cid, request_id="old-def", payload="{}", created_at=now - timedelta(days=31)
                            ),
                            WebhookLog(
                                config_id=cid, request_id="new-def", payload="{}", created_at=now - timedelta(days=29)
                            ),
                        ]
                    )
                    db.session.commit()
                    cleanup_logs.run()
                    assert WebhookLog.query.filter(WebhookLog.request_id.startswith("old-def")).count() == 0
                    assert WebhookLog.query.filter(WebhookLog.request_id.startswith("new-def")).count() == 1
                finally:
                    if old_val:
                        os.environ["LOG_RETENTION_DAYS"] = old_val

            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    if os.path.exists(path):
        os.remove(path)
