"""Shared rules for stale-endpoint monitoring.

The timeout task, the endpoint form, the configuration health check and the
service health API all need the same answers: which ConnectWise company a stale
ticket is created for, how often the check runs and whether the last run is
recent enough to trust.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

#: How often beat dispatches ``hookwise.check_webhook_timeouts``.
CHECK_INTERVAL_SECONDS = 300

#: A finished run older than this means beat or the worker stopped checking.
LAST_RUN_STALE_AFTER_SECONDS = 3 * CHECK_INTERVAL_SECONDS

LAST_RUN_REDIS_KEY = "hookwise_timeout_check_last_run"

#: Example values shipped in the compose files. They never name a real
#: ConnectWise company, so a ticket created with one is always rejected.
PLACEHOLDER_COMPANY_IDS = frozenset({"connectwise_company_id_for_tickets", "your_company_id"})

#: Results stored on the endpoint after each check. Failures are the ones the
#: endpoint overview highlights.
FAILED_RESULTS = frozenset({"create_failed", "repeat_failed", "error"})

MAX_ERROR_LENGTH = 1000


def effective_stale_company(customer_id_default: str | None) -> tuple[str | None, str]:
    """Company identifier a stale ticket is created for, and where it comes from.

    Mirrors the fallback in ``ConnectWiseClient._build_ticket_payload``.
    """
    endpoint_company = (customer_id_default or "").strip()
    if endpoint_company:
        return endpoint_company, "endpoint"
    env_company = (os.getenv("CW_DEFAULT_COMPANY_ID") or "").strip()
    if env_company:
        return env_company, "CW_DEFAULT_COMPANY_ID"
    return None, "none"


def stale_company_problem(customer_id_default: str | None) -> str | None:
    """Why a stale ticket for this endpoint cannot reach a real company, if it cannot."""
    company, source = effective_stale_company(customer_id_default)
    if company is None:
        return (
            "Stale endpoint alerts need a ConnectWise company: set a Default Company "
            "or configure CW_DEFAULT_COMPANY_ID."
        )
    if company.lower() in PLACEHOLDER_COMPANY_IDS:
        where = "the Default Company" if source == "endpoint" else "CW_DEFAULT_COMPANY_ID"
        return (
            f"Stale endpoint alerts would use the example company '{company}' from {where}, "
            "which ConnectWise rejects. Set a real Default Company."
        )
    return None


def bounded_error(message: str) -> str:
    return message if len(message) <= MAX_ERROR_LENGTH else message[: MAX_ERROR_LENGTH - 1] + "…"


def record_last_run(redis_client: Any, summary: dict[str, Any]) -> None:
    """Persist the latest run summary so health checks can judge freshness."""
    redis_client.set(LAST_RUN_REDIS_KEY, json.dumps(summary, default=str))


def last_run_status(redis_client: Any, now: datetime | None = None) -> dict[str, Any]:
    """Freshness and outcome of the latest finished timeout check."""
    raw = redis_client.get(LAST_RUN_REDIS_KEY)
    if not raw:
        return {"state": "unknown", "message": "No timeout check has finished yet."}
    try:
        summary = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        finished_at = datetime.fromisoformat(summary["finished_at"])
    except AttributeError, KeyError, TypeError, ValueError:
        return {"state": "unknown", "message": "The last timeout check summary is unreadable."}
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    age_seconds = int(((now or datetime.now(timezone.utc)) - finished_at).total_seconds())
    state = "stale" if age_seconds > LAST_RUN_STALE_AFTER_SECONDS else summary.get("status", "unknown")
    return {
        "state": state,
        "finished_at": finished_at.isoformat(),
        "age_seconds": age_seconds,
        "checked": summary.get("checked", 0),
        "failed": summary.get("failed", 0),
        "run_id": summary.get("run_id"),
    }
