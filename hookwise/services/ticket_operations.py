"""Database-backed idempotency guards for external ticket mutations."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import TicketOperation


class TicketOperationInProgress(RuntimeError):
    """Another worker owns an ambiguous external operation."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def reserve(log_id: str, operation: str) -> tuple[TicketOperation, bool]:
    """Reserve an operation, returning whether this caller may perform it."""
    existing = TicketOperation.query.filter_by(log_id=log_id, operation=operation).first()
    if existing:
        return existing, False
    row = TicketOperation(log_id=log_id, operation=operation)
    try:
        db.session.add(row)
        db.session.commit()
        return row, True
    except IntegrityError:
        db.session.rollback()
        winner = TicketOperation.query.filter_by(log_id=log_id, operation=operation).one()
        return winner, False


def may_take_over(row: TicketOperation) -> bool:
    """Permit recovery only after the original worker's maximum execution window."""
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created < datetime.now(timezone.utc) - timedelta(minutes=10)


def seconds_until_takeover(row: TicketOperation) -> float:
    """Return the remaining operation lease, bounded away from an immediate retry."""
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    lease_expires = created + timedelta(minutes=10)
    return max(1.0, (lease_expires - datetime.now(timezone.utc)).total_seconds())


def release(row: TicketOperation) -> None:
    """Release a definitively failed mutation so a later attempt may reserve it."""
    db.session.delete(row)
    db.session.commit()


def complete(row: TicketOperation, ticket_id: int) -> None:
    row.status = "completed"
    row.ticket_id = ticket_id
    row.completed_at = datetime.now(timezone.utc)
    db.session.commit()
