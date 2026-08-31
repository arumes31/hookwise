"""Discover external CIDs and resolve their configured ConnectWise company."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import CidMapping


class CompanyResolutionError(ValueError):
    """An observed custom CID has not been assigned in CIDMap."""


def observe_cid(cid: Any, customer_name: Any = None) -> CidMapping | None:
    """Persist a source-system CID/customer pair and return its current mapping."""
    raw_cid = str(cid).strip()[:100] if cid is not None else ""
    if not raw_cid:
        return None
    raw_customer = str(customer_name).strip()[:255] if customer_name is not None else ""
    now = datetime.now(timezone.utc)
    row = CidMapping.query.filter_by(cid=raw_cid).first()
    if row is None:
        row = CidMapping(cid=raw_cid, customer_name=raw_customer or None, first_seen_at=now, last_seen_at=now)
        db.session.add(row)
    else:
        row.last_seen_at = now
        row.seen_count = (row.seen_count or 0) + 1
        if raw_customer:
            row.customer_name = raw_customer
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        row = CidMapping.query.filter_by(cid=raw_cid).one()
        row.last_seen_at = now
        row.seen_count = (row.seen_count or 0) + 1
        if raw_customer:
            row.customer_name = raw_customer
        db.session.commit()
    return row


def resolve_company_identifier(value: Any, observed_cid: Any, mapping: CidMapping | None) -> str | None:
    """Resolve a mapped CID through CIDMap while preserving direct company identifiers."""
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return None
    cid = str(observed_cid).strip() if observed_cid is not None else ""
    if not cid or raw != cid:
        return raw
    if mapping and mapping.company_id:
        return mapping.company_id
    customer = f" ({mapping.customer_name})" if mapping and mapping.customer_name else ""
    raise CompanyResolutionError(f"CID {cid}{customer} is not assigned to a ConnectWise company in CIDMap")
