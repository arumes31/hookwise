import sys
from unittest.mock import MagicMock

# Mock all dependencies
modules = [
    'flask', 'flask_sqlalchemy', 'flask_limiter', 'flask_socketio',
    'prometheus_client', 'jsonpath_ng', 'werkzeug', 'werkzeug.middleware.proxy_fix',
    'cryptography', 'cryptography.fernet', 'flask_migrate', 'flask_wtf',
    'celery', 'redis', 'sqlalchemy', 'sqlalchemy.orm', 'requests', 'pyotp'
]

for mod in modules:
    sys.modules[mod] = MagicMock()

# Mock internal modules to avoid circular imports and missing deps
sys.modules['hookwise.extensions'] = MagicMock()
sys.modules['hookwise.models'] = MagicMock()
sys.modules['hookwise.utils'] = MagicMock()
sys.modules['hookwise.routes'] = MagicMock()

import hookwise.endpoints as endpoints

def test_register_splits():
    # Verify that the new registration functions exist
    assert hasattr(endpoints, '_register_display_routes')
    assert hasattr(endpoints, '_register_crud_routes')
    assert hasattr(endpoints, '_register_action_routes')
    assert hasattr(endpoints, '_register_bulk_routes')
    assert hasattr(endpoints, '_get_int_form_value')
    print("Test passed: registration functions exist.")

if __name__ == "__main__":
    try:
        test_register_splits()
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)
