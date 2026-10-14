"""Beat may only dispatch tasks a worker actually knows.

A renamed or misspelled task name would be sent by beat on schedule and then
dropped by every worker as unregistered, without any visible error in the UI.
"""

import pytest


@pytest.fixture(scope="module")
def celery_app():
    """Celery app with every task registered, as in worker.py."""
    # Mirror worker.py: build the Flask app, then import the task module.
    from hookwise import create_app

    create_app()
    from hookwise.tasks import celery

    return celery


def test_beat_schedule_only_dispatches_registered_tasks(celery_app):
    """Beat must not dispatch task names that no worker registers."""
    registered = set(celery_app.tasks)
    schedule = celery_app.conf.beat_schedule
    assert schedule, "beat schedule is empty"

    unknown = {name: entry["task"] for name, entry in schedule.items() if entry["task"] not in registered}
    assert not unknown, f"beat schedule entries point to unregistered tasks: {unknown}"


def test_beat_schedule_intervals_are_positive(celery_app):
    """Every schedule entry runs on a positive interval."""
    for name, entry in celery_app.conf.beat_schedule.items():
        assert float(entry["schedule"]) > 0, f"{name} has a non-positive interval"
