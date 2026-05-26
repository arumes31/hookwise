import sys

with open("hookwise/tasks.py", "r") as f:
    lines = f.readlines()

new_lines = []
skip_until = -1
for i, line in enumerate(lines):
    if i < skip_until:
        continue
    if "def check_webhook_timeouts() -> None:" in line:
        new_lines.append(line)
        new_lines.append(lines[i+1]) # Docstring
        new_lines.append("    from .extensions import db\n")
        new_lines.append("    from .models import WebhookConfig\n")
        # Skip the original docstring and potential duplicate lines I just added
        skip_until = i + 2
        continue
    new_lines.append(line)

with open("hookwise/tasks.py", "w") as f:
    f.writelines(new_lines)
