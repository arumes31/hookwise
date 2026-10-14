"""Shared access to the running integration stack."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

BASE_URL = os.environ.get("HOOKWISE_IT_BASE_URL", "").rstrip("/")
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "docker-compose.yml"),
    "-f",
    str(ROOT / "tests" / "integration" / "docker-compose.integration.yml"),
]

requires_stack = pytest.mark.skipif(not BASE_URL, reason="integration stack not configured (HOOKWISE_IT_BASE_URL)")


def run_python(service: str, code: str) -> Any:
    """Run code inside a stack container and return the JSON it prints after 'RESULT '."""
    result = subprocess.run(
        [*COMPOSE, "exec", "-T", service, "python", "-"],
        input=code,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        timeout=180,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    assert result.returncode == 0 and lines, f"{service} script failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(lines[-1].removeprefix("RESULT "))


APP_PRELUDE = """
import json
from datetime import datetime, timedelta, timezone
from worker import app
from hookwise.extensions import db
from hookwise.models import AuditLog, WebhookConfig, WebhookLog
"""
