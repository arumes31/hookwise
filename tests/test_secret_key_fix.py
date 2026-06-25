import os
import unittest
from unittest.mock import patch

from hookwise import create_app


class TestSecretKeyFix(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_secret_key_missing_production_raises_error(self):
        # Ensure SECRET_KEY and DEBUG_MODE are not in env
        os.environ["DEBUG_MODE"] = "false"
        if "SECRET_KEY" in os.environ:
            del os.environ["SECRET_KEY"]

        # We need to set GUI_PASSWORD to avoid its RuntimeError
        os.environ["GUI_PASSWORD"] = "testpass"
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"

        with self.assertRaises(RuntimeError) as cm:
            create_app()
        self.assertEqual(str(cm.exception), "SECRET_KEY env var is required")

    @patch.dict(os.environ, {}, clear=True)
    def test_secret_key_missing_debug_uses_generated_key(self):
        os.environ["DEBUG_MODE"] = "true"
        if "SECRET_KEY" in os.environ:
            del os.environ["SECRET_KEY"]

        # GUI_PASSWORD will also use default in debug mode, but setting it explicitly is safer
        os.environ["GUI_PASSWORD"] = "testpass"
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"

        app = create_app()
        secret_key = app.config.get("SECRET_KEY")
        # In debug mode a secure, ephemeral key is generated rather than a
        # predictable hardcoded value.
        self.assertTrue(secret_key)
        self.assertNotEqual(secret_key, "dev-secret-key")
        self.assertGreaterEqual(len(secret_key), 32)

    @patch.dict(os.environ, {}, clear=True)
    def test_secret_key_provided_is_used(self):
        os.environ["SECRET_KEY"] = "provided-secret"
        os.environ["GUI_PASSWORD"] = "testpass"
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"

        app = create_app()
        self.assertEqual(app.config.get("SECRET_KEY"), "provided-secret")


if __name__ == "__main__":
    unittest.main()
