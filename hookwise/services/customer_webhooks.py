"""Customer-scoped alarm matching and durable, signed HTTPS delivery."""

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import urllib3
from jsonpath_ng import parse
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import CustomerWebhook, CustomerWebhookDelivery, WebhookConfig, WebhookLog
from ..utils import decrypt_string, ensure_encrypted, mask_secrets, resolve_jsonpath, resolve_monitor_name
from .routing import evaluate_routing

MAX_ATTEMPTS = 5
MAX_TARGETS = 10
TERMINAL_STATUSES = ("delivered", "failed", "cancelled")
logger = logging.getLogger(__name__)


def destination_urls(webhook: CustomerWebhook) -> list[str]:
    additional = json.loads(decrypt_string(webhook.additional_urls)) if webhook.additional_urls else []
    if not isinstance(additional, list):
        raise ValueError("Ungültige Ziel-URLs.")
    urls = [decrypt_string(webhook.target_url), *additional]
    validate_target_urls(urls)
    return urls


def validate_target_urls(urls: list[str]) -> None:
    if not 1 <= len(urls) <= MAX_TARGETS or any(not isinstance(url, str) for url in urls):
        raise ValueError(f"Bitte 1 bis {MAX_TARGETS} Ziel-URLs angeben.")
    if len(set(urls)) != len(urls):
        raise ValueError("Jede Ziel-URL darf nur einmal vorkommen.")
    for url in urls:
        validate_target_url(url)


def set_destination(
    webhook: CustomerWebhook, url: str | list[str], credentials: dict[str, str | None], *, drop_replayed: bool = True
) -> None:
    """Bind credentials and queued alarms to the receiver, including backup restores."""
    credentials = {field: ensure_encrypted(value) if value else None for field, value in credentials.items()}
    urls = [url] if isinstance(url, str) else url
    validate_target_urls(urls)
    changed = bool(webhook.target_url) and destination_urls(webhook) != urls
    if changed:
        for field in ("bearer_token", "signing_secret"):
            old = getattr(webhook, field)
            new = credentials.get(field)
            # Replayed backup ciphertext is not a new credential for a new receiver.
            if drop_replayed and old and new and decrypt_string(old) == decrypt_string(new):
                credentials[field] = None
            setattr(webhook, field, None)
        webhook.destination_generation += 1
        CustomerWebhookDelivery.query.filter(
            CustomerWebhookDelivery.webhook_id == webhook.id,
            CustomerWebhookDelivery.status.in_(("pending", "sending")),
        ).update(
            {"status": "cancelled", "payload": "{}", "completed_at": datetime.now(timezone.utc)},
            synchronize_session="fetch",
        )
    webhook.target_url = urls[0]
    webhook.additional_urls = json.dumps(urls[1:]) if len(urls) > 1 else None
    for field, value in credentials.items():
        setattr(webhook, field, value)


def delete_delivery_history(log_ids: list[str] | None = None) -> int:
    """Explicit log deletion also removes queued and historical alert copies."""
    query = CustomerWebhookDelivery.query
    if log_ids is not None:
        query = query.filter(CustomerWebhookDelivery.log_id.in_(log_ids))
    return int(query.delete(synchronize_session="fetch"))


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not getattr(address, "is_site_local", False)


def validate_target_url(url: str) -> None:
    """Only standard HTTPS endpoints; credentials belong in dedicated fields."""
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and not parts.username
            and not parts.password
            and parts.port in (None, 443)
            and not parts.fragment
            and len(url) <= 2048
            and not any(ord(c) < 33 or ord(c) == 127 for c in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Die Ziel-URL muss HTTPS mit Port 443 verwenden, ohne Zugangsdaten oder Fragment.")
    try:
        public = _public_address(parts.hostname or "")
    except ValueError:
        return
    if not public:
        raise ValueError("Die Ziel-URL muss auf eine öffentliche Adresse zeigen.")


def validate_score_path(path: str) -> None:
    """Bound JSONPath to field navigation and array selection."""
    if len(path) > 200 or not re.fullmatch(r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[(?:\*|[0-9]{1,5})\])+", path):
        raise ValueError("Score-Feld als JSONPath angeben, z. B. $.score oder $.Results[*].Score.")
    if not re.search(r"\.[A-Za-z_][A-Za-z0-9_]*$", path):
        raise ValueError("Der Score-Pfad muss mit einem Feldnamen enden.")


def numeric_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError, OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _company_for_payload(config: Any, data: dict[str, Any]) -> str | None:
    """Use explicit routing; unknown tenants on shared sources never use a fallback."""
    from ..tasks import _global_mapping_for_tenant, get_all_global_mappings

    decision = evaluate_routing(data, config.to_dict())
    if decision.values.get("drop") or decision.alert_type == "UP":
        return None
    company = decision.values.get("customer_id")
    match = re.search(r"#CW-?(\w+)", resolve_monitor_name(data))
    company = company or (match.group(1) if match else None)
    if not company and config.global_routing_enabled:
        tenant = next(
            (
                value
                for field in ("Tenant", "tenant", "tenantId", "TenantId")
                if (value := resolve_jsonpath(data, f"$.{field}") or resolve_jsonpath(data, f"$.TaskInfo.{field}"))
            ),
            None,
        )
        mapping = _global_mapping_for_tenant(str(tenant or ""), get_all_global_mappings())
        if not mapping:
            return None
        company = mapping.get("company_id")
    if not company:
        company = config.customer_id_default or os.environ.get("CW_DEFAULT_COMPANY_ID")
    return str(company).strip() if company else None


def stage_customer_deliveries(log: WebhookLog, data: dict[str, Any]) -> None:
    """Persist matching alarms atomically with ingress, before any ticket API call."""
    from ..tasks import is_in_maintenance

    subscriptions = CustomerWebhook.query.filter_by(config_id=log.config_id, is_enabled=True).populate_existing().all()
    if not subscriptions:
        return
    config = db.session.get(WebhookConfig, log.config_id)
    if (
        not config
        or not config.is_enabled
        or not config.forwarding_enabled
        or config.is_draft
        or config.archived_at
        or is_in_maintenance(config)
    ):
        return
    company_id = _company_for_payload(config, data)
    if not company_id:
        return
    for webhook in subscriptions:
        if webhook.company_id != company_id:
            continue
        if webhook.score_path:
            try:
                validate_score_path(webhook.score_path)
            except ValueError:
                logger.warning("Skipping Forwarding subscription %s: invalid score_path", webhook.id)
                continue
            matches = [
                (numeric_score(match.value), match.context.value) for match in parse(webhook.score_path).find(data)
            ]
        else:
            matches = [(None, data)]
        try:
            targets = destination_urls(webhook)
        except ValueError:
            logger.warning("Skipping Forwarding subscription %s: invalid destinations", webhook.id)
            continue
        for score, matched_alert in matches:
            if webhook.score_path and (score is None or score < webhook.minimum_score):
                continue
            # Send only the object containing the matched score, never the whole
            # batch of unrelated alerts or the sender's authentication headers.
            alert = mask_secrets(matched_alert)
            canonical = json.dumps(alert, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            event_key = hashlib.sha256(f"{company_id}:{canonical}".encode()).hexdigest()
            payload = {
                "event": "hookwise.customer_alert",
                "version": 1,
                "company_id": company_id,
                "source_endpoint_id": config.id,
                "request_id": log.request_id,
                "score": score,
                "minimum_score": webhook.minimum_score if webhook.score_path else None,
                "alert": alert,
            }
            for target_index in range(len(targets)):
                # Keep the legacy key for the first target so existing deduplication survives upgrades.
                target_key = (
                    event_key
                    if target_index == 0
                    else hashlib.sha256(f"{event_key}:{target_index}".encode()).hexdigest()
                )
                if CustomerWebhookDelivery.query.filter_by(webhook_id=webhook.id, event_key=target_key).first():
                    continue
                try:
                    with db.session.begin_nested():
                        db.session.add(
                            CustomerWebhookDelivery(
                                webhook_id=webhook.id,
                                log_id=log.id,
                                event_key=target_key,
                                target_index=target_index,
                                destination_generation=webhook.destination_generation,
                                payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                            )
                        )
                        db.session.flush()
                except IntegrityError:
                    # A concurrent ingestion already persisted this exact alarm for this target.
                    continue


def post_webhook(url: str, body: bytes, headers: dict[str, str]) -> int:
    """Pin the validated public DNS result to the TLS connection (no redirects/proxies)."""
    validate_target_url(url)
    parts = urlsplit(url)
    hostname = (parts.hostname or "").encode("idna").decode("ascii")
    addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not _public_address(str(item[4][0])) for item in addresses):
        raise ValueError("Zieladresse ist nicht öffentlich.")
    address = str(addresses[0][4][0])
    pool = urllib3.HTTPSConnectionPool(
        address,
        port=443,
        server_hostname=hostname,
        assert_hostname=hostname,
        cert_reqs="CERT_REQUIRED",
        timeout=urllib3.Timeout(connect=5, read=10),
        retries=False,
    )
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    response = None
    try:
        response = pool.urlopen(
            "POST",
            target,
            body=body,
            headers={**headers, "Host": hostname},
            redirect=False,
            retries=False,
            preload_content=False,
            assert_same_host=False,
        )
        return int(response.status)
    finally:
        if response is not None:
            response.close()
        pool.close()


@dataclass(frozen=True)
class DeliveryAttempt:
    delivery_id: str
    attempts: int
    claim_until: datetime
    target_url: str
    bearer_token: str | None
    signing_secret: str | None
    body: bytes


def _send_attempt(attempt: DeliveryAttempt) -> dict[str, Any]:
    """Perform network I/O only; database sessions stay in the dispatcher thread."""
    delivery_id, attempts = attempt.delivery_id, attempt.attempts
    target_url, bearer_token, signing_secret = attempt.target_url, attempt.bearer_token, attempt.signing_secret
    body = attempt.body
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Hookwise/Customer-Webhooks",
        "Idempotency-Key": delivery_id,
        "X-HookWise-Delivery-ID": delivery_id,
    }
    code = None
    error_message = None
    try:
        if bearer_token:
            headers["Authorization"] = "Bearer " + decrypt_string(bearer_token)
        if signing_secret:
            timestamp = str(int(time.time()))
            signature = hmac.new(
                decrypt_string(signing_secret).encode(),
                timestamp.encode() + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-HookWise-Timestamp"] = timestamp
            headers["X-HookWise-Signature"] = "sha256=" + signature
        code = post_webhook(target_url, body, headers)
        if 200 <= code < 300:
            status = "delivered"
        else:
            retryable = code in (408, 425, 429) or code >= 500
            status = "pending" if retryable and attempts < MAX_ATTEMPTS else "failed"
            error_message = f"Empfänger antwortete mit HTTP {code}."
    except Exception as exc:
        # Exception strings can contain URLs, query credentials or headers.
        error_message = f"Zustellung fehlgeschlagen ({type(exc).__name__})."
        retryable = not isinstance(exc, ValueError)
        status = "pending" if retryable and attempts < MAX_ATTEMPTS else "failed"
    changes = {
        "status": status,
        "response_status": code,
        "error_message": error_message,
        "next_attempt_at": datetime.now(timezone.utc) + timedelta(seconds=min(30 * 2**attempts, 1800)),
    }
    if status in TERMINAL_STATUSES:
        changes.update(payload="{}", completed_at=datetime.now(timezone.utc))
    return changes


def dispatch_customer_deliveries(limit: int = 10) -> dict[str, int]:
    """Claim due rows atomically and recover abandoned claims after five minutes."""
    now = datetime.now(timezone.utc)
    CustomerWebhookDelivery.query.filter(
        CustomerWebhookDelivery.status == "sending",
        CustomerWebhookDelivery.next_attempt_at <= now,
    ).update({"status": "pending"}, synchronize_session=False)
    db.session.commit()
    ids = [
        row.id
        for row in CustomerWebhookDelivery.query.filter(
            CustomerWebhookDelivery.status == "pending",
            CustomerWebhookDelivery.next_attempt_at <= now,
        )
        .order_by(CustomerWebhookDelivery.next_attempt_at)
        .limit(max(1, min(limit, 100)))
        .all()
    ]
    result = {"delivered": 0, "failed": 0}
    jobs: list[DeliveryAttempt] = []
    for delivery_id in ids:
        claimed = CustomerWebhookDelivery.query.filter(
            CustomerWebhookDelivery.id == delivery_id,
            CustomerWebhookDelivery.status == "pending",
            CustomerWebhookDelivery.next_attempt_at <= datetime.now(timezone.utc),
        ).update(
            {"status": "sending", "next_attempt_at": datetime.now(timezone.utc) + timedelta(minutes=5)},
            synchronize_session=False,
        )
        db.session.commit()
        if not claimed:
            continue
        delivery = db.session.get(CustomerWebhookDelivery, delivery_id)
        if delivery is None:
            continue
        webhook = CustomerWebhook.query.filter_by(id=delivery.webhook_id).populate_existing().one()
        config = WebhookConfig.query.filter_by(id=webhook.config_id).populate_existing().one()
        if (
            not webhook.is_enabled
            or not config.is_enabled
            or not config.forwarding_enabled
            or config.archived_at
            or config.is_draft
            or delivery.destination_generation != webhook.destination_generation
        ):
            delivery.status = "cancelled"
            delivery.payload = "{}"
            delivery.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            continue
        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.payload = "{}"
            delivery.error_message = "Maximale Anzahl der Zustellversuche erreicht."
            delivery.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            continue
        # Snapshot one destination/credential generation before committing. An
        # in-flight request may finish to the old receiver, never to a new one.
        try:
            target_url = destination_urls(webhook)[delivery.target_index]
        except ValueError, IndexError:
            delivery.status, delivery.payload = "failed", "{}"
            delivery.error_message = "Ungültiges Zustellziel."
            delivery.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            result["failed"] += 1
            continue
        bearer_token, signing_secret = webhook.bearer_token, webhook.signing_secret
        body = delivery.payload.encode("utf-8")
        attempts = delivery.attempts + 1
        claim_until = delivery.next_attempt_at
        # A worker dying after the request must still consume an attempt.
        started = CustomerWebhookDelivery.query.filter_by(
            id=delivery_id,
            status="sending",
            next_attempt_at=claim_until,
            destination_generation=webhook.destination_generation,
        ).update({"attempts": attempts}, synchronize_session=False)
        db.session.commit()
        if not started:
            continue
        jobs.append(DeliveryAttempt(delivery_id, attempts, claim_until, target_url, bearer_token, signing_secret, body))
    if len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=MAX_TARGETS) as pool:
            outcomes = list(pool.map(_send_attempt, jobs))
    else:
        outcomes = [_send_attempt(job) for job in jobs]
    for job, changes in zip(jobs, outcomes, strict=True):
        delivery_id, attempts, claim_until = job.delivery_id, job.attempts, job.claim_until
        # A retarget, history deletion or another claim must win over this result.
        updated = CustomerWebhookDelivery.query.filter_by(
            id=delivery_id, status="sending", attempts=attempts, next_attempt_at=claim_until
        ).update(changes, synchronize_session="fetch")
        if updated:
            result["delivered" if changes["status"] == "delivered" else "failed"] += 1
        db.session.commit()
    return result
