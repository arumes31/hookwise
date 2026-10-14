"""Stale-endpoint alerting across the real stack: ingress, broker, worker, beat, Postgres.

Runs only when the integration stack is up and HOOKWISE_IT_BASE_URL points at it;
see tests/integration/README.md.
"""

import json
import subprocess
import time
import urllib.request
from typing import Any

from .stack import APP_PRELUDE, BASE_URL, COMPOSE, ROOT, requires_stack, run_python

pytestmark = requires_stack


def _endpoint_state(name: str) -> Any:
    return run_python(
        "hookwise-worker",
        APP_PRELUDE
        + f"""
with app.app_context():
    c = WebhookConfig.query.filter_by(name={name!r}).one()
    logs = WebhookLog.query.filter_by(config_id=c.id).all()
    audits = AuditLog.query.filter_by(config_id=c.id).all()
    print("RESULT " + json.dumps({{
        "result": c.stale_check_result, "error": c.stale_check_error, "ticket": c.timeout_ticket_id,
        "logs": [{{"status": l.status, "action": l.action, "source_ip": l.source_ip,
                   "error_type": l.error_type, "ticket_id": l.ticket_id}} for l in logs],
        "audits": [a.action for a in audits],
    }}))
""",
    )


def test_stale_alert_outcomes_are_recorded_across_the_stack() -> None:
    rejected, accepted = "IT stale rejected", "IT stale accepted"
    setup = run_python(
        "hookwise-worker",
        APP_PRELUDE
        + f"""
import secrets
from hookwise.utils import encrypt_string
with app.app_context():
    for old in WebhookConfig.query.filter(WebhookConfig.name.in_([{rejected!r}, {accepted!r}])).all():
        db.session.delete(old)
    db.session.commit()
    token = secrets.token_urlsafe(32)
    ids = {{}}
    for name, company in (({rejected!r}, "UNKNOWN-CO"), ({accepted!r}, "ACME")):
        c = WebhookConfig(name=name, customer_id_default=company, board="Monitoring", status="New",
                          trigger_field="status", open_value="down", close_value="up",
                          bearer_token=encrypt_string(token), bearer_token_last4=token[-4:],
                          timeout_alerts_enabled=True, timeout_hours=1, retry_enabled=False)
        db.session.add(c)
        db.session.commit()
        ids[name] = c.id
    print("RESULT " + json.dumps({{"ids": ids, "token": token}}))
""",
    )

    # A real heartbeat through ingress, the outbox and the worker.
    request = urllib.request.Request(
        f"{BASE_URL}/w/{setup['ids'][accepted]}",
        data=json.dumps({"status": "up"}).encode(),
        headers={"Authorization": f"Bearer {setup['token']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 202
    deadline = time.monotonic() + 60
    while True:
        state = _endpoint_state(accepted)
        if state["logs"] and state["logs"][0]["status"] in {"processed", "skipped"}:
            break
        assert time.monotonic() < deadline, f"heartbeat was not processed: {state}"
        time.sleep(2)

    # Pretend an hour has passed, then run the check through the broker.
    summary = run_python(
        "hookwise-worker",
        APP_PRELUDE
        + f"""
from hookwise.tasks import celery
with app.app_context():
    for c in WebhookConfig.query.filter(WebhookConfig.name.in_([{rejected!r}, {accepted!r}])).all():
        c.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=61)
    db.session.commit()
outcome = celery.send_task("hookwise.check_webhook_timeouts").get(timeout=120)
print("RESULT " + json.dumps(outcome))
""",
    )
    assert summary["status"] == "partial_failed", summary
    assert [failure["name"] for failure in summary["failures"]] == [rejected], summary

    refused = _endpoint_state(rejected)
    assert refused["result"] == "create_failed"
    assert "UNKNOWN-CO" in (refused["error"] or "")
    assert refused["ticket"] is None
    assert {"status": "failed", "action": "create", "source_ip": "system"}.items() <= refused["logs"][0].items()
    assert refused["logs"][0]["error_type"] == "TicketCreationRejected"
    assert "timeout_error" in refused["audits"]

    created = _endpoint_state(accepted)
    assert created["result"] == "alert_created"
    assert created["ticket"] is not None
    assert any(log["action"] == "create" and log["ticket_id"] == created["ticket"] for log in created["logs"])

    sent = run_python(
        "cw-mock",
        "import json, urllib.request\n"
        "print('RESULT ' + urllib.request.urlopen('http://localhost:8080/__requests').read().decode())\n",
    )
    creates = {
        (entry["body"]["company"]["identifier"], entry["status"])
        for entry in sent
        if entry["method"] == "POST" and entry["path"].endswith("/service/tickets")
    }
    assert {("UNKNOWN-CO", 400), ("ACME", 201)} <= creates

    health = run_python(
        "hookwise-worker",
        "import json\nfrom worker import app\nfrom hookwise.extensions import redis_client\n"
        "from hookwise.services.stale_monitor import last_run_status\n"
        "print('RESULT ' + json.dumps(last_run_status(redis_client)))\n",
    )
    assert health["state"] == "partial_failed"


def test_scheduler_and_worker_report_healthy() -> None:
    result = subprocess.run(
        [*COMPOSE, "ps", "--format", "json", "hookwise-beat", "hookwise-worker"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    services = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    health = {service["Service"]: service.get("Health") for service in services}
    assert health == {"hookwise-beat": "healthy", "hookwise-worker": "healthy"}
