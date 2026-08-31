"""Database-backed idempotency guards for external ticket mutations."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import TicketOperation


class TicketOperationInProgress(RuntimeError):
    """Another worker owns an ambiguous external operation."""


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


def complete(row: TicketOperation, ticket_id: int) -> None:
    row.status = "completed"
    row.ticket_id = ticket_id
    row.completed_at = datetime.now(timezone.utc)
    db.session.commit()
