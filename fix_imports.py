import re

with open("hookwise/tasks.py", "r") as f:
    content = f.read()

# Remove top-level imports that cause circular or early loading issues
content = content.replace("from .models import WebhookConfig, WebhookLog\n", "")

# Ensure check_webhook_timeouts has its imports
if "from .models import WebhookConfig" not in content.split("def check_webhook_timeouts"):
    # Already added by previous restoration, but let's be sure
    pass

# Ensure cleanup_logs has its imports
content = content.replace("def cleanup_logs() -> None:\n    \"\"\"Delete WebhookLog entries older than log_retention_days.\"\"\"",
                          "def cleanup_logs() -> None:\n    \"\"\"Delete WebhookLog entries older than log_retention_days.\"\"\"\n    from .extensions import db\n    from .models import WebhookLog")

# Ensure verify_config_health has its imports
content = content.replace("def verify_config_health() -> None:\n    \"\"\"Periodically verify the connectivity and validity of webhook configurations.\"\"\"",
                          "def verify_config_health() -> None:\n    \"\"\"Periodically verify the connectivity and validity of webhook configurations.\"\"\"\n    from .extensions import db\n    from .models import WebhookConfig")

# Ensure handle_webhook_logic has its imports
content = content.replace("def handle_webhook_logic(config_id: str, data: Dict[str, Any], request_id: str) -> None:",
                          "def handle_webhook_logic(config_id: str, data: Dict[str, Any], request_id: str) -> None:\n    from .extensions import db\n    from .models import WebhookConfig, WebhookLog")

# Ensure process_webhook_task has its imports
content = content.replace("def process_webhook_task(\n    self: Any,\n    config_id: str,\n    data: Dict[str, Any],\n    request_id: str,\n    source_ip: Optional[str] = None,\n) -> None:",
                          "def process_webhook_task(\n    self: Any,\n    config_id: str,\n    data: Dict[str, Any],\n    request_id: str,\n    source_ip: Optional[str] = None,\n) -> None:\n    from .extensions import db\n    from .models import WebhookConfig, WebhookLog")

with open("hookwise/tasks.py", "w") as f:
    f.write(content)
