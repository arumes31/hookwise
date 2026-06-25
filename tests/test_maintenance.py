import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hookwise.models import WebhookConfig
from hookwise.tasks import is_in_maintenance


@pytest.fixture
def mock_now():
    with patch("hookwise.tasks.datetime") as mock_datetime:
        yield mock_datetime


def test_no_maintenance_windows():
    config = WebhookConfig(maintenance_windows=None)
    assert is_in_maintenance(config) is False


def test_invalid_json_maintenance_windows():
    config = WebhookConfig(maintenance_windows="invalid json")
    assert is_in_maintenance(config) is False


def test_once_window_active(mock_now):
    # Now: 2024-01-01 12:00 UTC
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now
    mock_now.fromisoformat.side_effect = datetime.fromisoformat

    windows = [{"type": "once", "start": "2024-01-01T10:00:00Z", "end": "2024-01-01T14:00:00Z"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is True


def test_once_window_inactive(mock_now):
    now = datetime(2024, 1, 1, 15, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now
    mock_now.fromisoformat.side_effect = datetime.fromisoformat

    windows = [{"type": "once", "start": "2024-01-01T10:00:00Z", "end": "2024-01-01T14:00:00Z"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is False


def test_daily_window_active(mock_now):
    # Now: Monday 14:00
    now = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now
    # We need to mock replace and strftime because they are called on 'now'
    # Actually 'now' is a real datetime object, but datetime.now() returns it.

    windows = [{"type": "daily", "start": "12:00", "end": "16:00"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is True


def test_daily_window_overnight_active(mock_now):
    # Now: 23:00
    now = datetime(2024, 1, 1, 23, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now

    windows = [{"type": "daily", "start": "22:00", "end": "02:00"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is True


def test_daily_window_overnight_active_morning(mock_now):
    # Now: 01:00
    now = datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now

    windows = [{"type": "daily", "start": "22:00", "end": "02:00"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is True


def test_weekly_window_active(mock_now):
    # 2024-01-01 is Monday
    now = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now

    windows = [{"type": "weekly", "days": ["Mon"], "start": "12:00", "end": "16:00"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is True


def test_weekly_window_inactive_day(mock_now):
    # 2024-01-01 is Monday
    now = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now

    windows = [{"type": "weekly", "days": ["Tue"], "start": "12:00", "end": "16:00"}]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is False


def test_multiple_windows(mock_now):
    now = datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    mock_now.now.return_value = now
    mock_now.fromisoformat.side_effect = datetime.fromisoformat

    windows = [
        {"type": "once", "start": "2023-01-01T00:00:00Z", "end": "2023-01-01T01:00:00Z"},
        {"type": "daily", "start": "13:00", "end": "15:00"},
    ]
    config = WebhookConfig(maintenance_windows=json.dumps(windows))
    assert is_in_maintenance(config) is True
