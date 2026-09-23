"""Transactional outbox for durable Celery delivery dispatch."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..extensions import db
from ..models import DeliveryOutbox, WebhookLog


def stage_delivery(
    log: WebhookLog,
    data: dict[str, Any],
    *,
    source_ip: str | None = None,
    headers: dict[str, str] | None = None,
) -> DeliveryOutbox:
    """Stage a task intent in the same transaction as its delivery log."""
    log.status = "pending_enqueue"
    db.session.add(log)
    db.session.flush()
    outbox = DeliveryOutbox(
        log=log,
        arguments=json.dumps(
            {
                "config_id": log.config_id,
                "data": data,
                "request_id": log.request_id,
                "source_ip": source_ip,
                "headers": headers,
                "log_id": log.id,
            },
            separators=(",", ":"),
        ),
    )
    db.session.add(outbox)
    return outbox


def dispatch_outbox(outbox: DeliveryOutbox, task: Any = None) -> bool:
    """Dispatch one committed outbox record and persist its observable result."""
    if task is None:
        from ..tasks import process_webhook_task

        task = process_webhook_task

    outbox.attempts = int(outbox.attempts or 0) + 1
    log = WebhookLog.query.get(outbox.log_id)
    if log is None:
        db.session.delete(outbox)
        db.session.commit()
        return False
    try:
        arguments = json.loads(outbox.arguments)
        optional_arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"config_id", "data", "request_id"} and value is not None
        }
        task.delay(
            arguments.pop("config_id"),
            arguments.pop("data"),
            arguments.pop("request_id"),
            **optional_arguments,
        )
    except Exception as exc:
        outbox.status = "pending"
        outbox.last_error = f"{type(exc).__name__}: {exc}"[:2_000]
        log.status = "enqueue_failed"
        log.error_type = "task_enqueue_failed"
        log.error_message = "Task broker unavailable; durable outbox will retry."
        db.session.commit()
        return False
    outbox.status = "dispatched"
    outbox.last_error = None
    outbox.dispatched_at = datetime.now(timezone.utc)
    log.status = "queued"
    log.error_type = None
    log.error_message = None
    log.queued_at = outbox.dispatched_at
    db.session.commit()
    return True


def commit_and_dispatch(outbox: DeliveryOutbox, task: Any = None) -> bool:
    """Commit the domain change and then attempt low-latency dispatch."""
    # Der Besitzvermerk geht mit derselben Transaktion raus wie die fachliche
    # Aenderung. Sonst sieht der Outbox-Sweep die Zeile im Fenster zwischen
    # Commit und Versand als "pending" und stellt denselben Task ein zweites Mal
    # in die Queue. Scheitert der Versand, setzt ``dispatch_outbox`` zurueck.
    outbox.status = "dispatching"
    outbox.dispatched_at = datetime.now(timezone.utc)
    db.session.commit()
    return dispatch_outbox(outbox, task)


#: Nach dieser Frist gilt ein Besitzvermerk als von einem abgestuerzten Lauf
#: liegengelassen und faellt zurueck in den Pool.
CLAIM_TIMEOUT_SECONDS = 300


def _reclaim_stale_claims() -> int:
    """Return claims abandoned by a crashed dispatcher to the pending pool."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
    reclaimed = DeliveryOutbox.query.filter(
        DeliveryOutbox.status == "dispatching",
        DeliveryOutbox.dispatched_at < cutoff,
    ).update({DeliveryOutbox.status: "pending"}, synchronize_session=False)
    if reclaimed:
        db.session.commit()
    return int(reclaimed)


def dispatch_pending(limit: int = 100) -> tuple[int, int]:
    """Retry pending records; safe to run repeatedly because each log has one outbox row."""
    _reclaim_stale_claims()
    query = (
        DeliveryOutbox.query.filter_by(status="pending")
        .order_by(DeliveryOutbox.created_at.asc())
        .limit(max(1, min(limit, 500)))
    )
    # Der Sweep laeuft alle zehn Sekunden und dauert bei Rueckstau laenger als
    # sein Intervall, zwei Laeufe ueberlappen sich also. Ohne Zeilensperre lesen
    # beide denselben Satz und stellen jeden Task doppelt in die Queue.
    if db.session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = query.all()
    if not rows:
        return 0, 0

    # Der Vermerk wird vor dem Versand festgeschrieben: danach findet ein
    # paralleler Sweep die Zeilen nicht mehr, auch nachdem die Sperre faellt.
    claimed_at = datetime.now(timezone.utc)
    for row in rows:
        row.status = "dispatching"
        row.dispatched_at = claimed_at
    db.session.commit()

    dispatched = 0
    for row in rows:
        if dispatch_outbox(row):
            dispatched += 1
    return dispatched, len(rows) - dispatched
