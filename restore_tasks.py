import re

with open("hookwise/tasks.py", "r") as f:
    content = f.read()

# Pattern to find the messed up function definition
pattern = r'@celery\.task\(name="hookwise\.check_webhook_timeouts"\)  # type: ignore\[untyped-decorator\]\ndef check_webhook_timeouts\(\) -> None:.*?try:'
replacement = '@celery.task(name="hookwise.check_webhook_timeouts")  # type: ignore[untyped-decorator]\ndef check_webhook_timeouts() -> None:\n    """Check for endpoints that have not received data within the configured timeout period."""\n    from .extensions import db\n    from .models import WebhookConfig\n\n    try:'

# Use non-greedy match for the content between docstring and try
content = re.sub(pattern, replacement, content, flags=re.DOTALL)

with open("hookwise/tasks.py", "w") as f:
    f.write(content)
