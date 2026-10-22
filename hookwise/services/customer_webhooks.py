"""Customer-scoped alarm matching and durable, signed HTTPS delivery."""

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import urllib3
from jsonpath_ng import parse
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import CustomerWebhook, CustomerWebhookDelivery, WebhookConfig, WebhookLog
from ..utils import decrypt_string, mask_secrets, resolve_jsonpath, resolve_monitor_name
from .routing import evaluate_routing

MAX_ATTEMPTS = 5


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

    subscriptions = CustomerWebhook.query.filter_by(config_id=log.config_id, is_enabled=True).all()
    if not subscriptions:
        return
    config = db.session.get(WebhookConfig, log.config_id)
    if not config or not config.is_enabled or config.is_draft or config.archived_at or is_in_maintenance(config):
        return
    company_id = _company_for_payload(config, data)
    if not company_id:
        return
    for webhook in subscriptions:
        if webhook.company_id != company_id:
            continue
        validate_score_path(webhook.score_path)
        for match in parse(webhook.score_path).find(data):
            score = numeric_score(match.value)
            if score is None or score < webhook.minimum_score:
                continue
            # Send only the object containing the matched score, never the whole
            # batch of unrelated alerts or the sender's authentication headers.
            alert = mask_secrets(match.context.value)
            canonical = json.dumps(alert, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            event_key = hashlib.sha256(f"{company_id}:{canonical}".encode()).hexdigest()
            payload = {
                "event": "hookwise.customer_alert",
                "version": 1,
                "company_id": company_id,
                "source_endpoint_id": config.id,
                "request_id": log.request_id,
                "score": score,
                "minimum_score": webhook.minimum_score,
                "alert": alert,
            }
            if CustomerWebhookDelivery.query.filter_by(webhook_id=webhook.id, event_key=event_key).first():
                continue
            try:
                with db.session.begin_nested():
                    db.session.add(
                        CustomerWebhookDelivery(
                            webhook_id=webhook.id,
                            log_id=log.id,
                            event_key=event_key,
                            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        )
                    )
                    db.session.flush()
            except IntegrityError:
                # A concurrent ingestion already persisted this exact alarm.
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
        webhook = delivery.webhook
        if (
            not webhook.is_enabled
            or not webhook.config.is_enabled
            or webhook.config.archived_at
            or webhook.config.is_draft
        ):
            delivery.status = "cancelled"
            delivery.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            continue
        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = "failed"
            delivery.error_message = "Maximale Anzahl der Zustellversuche erreicht."
            delivery.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            continue
        delivery.attempts += 1
        # A worker dying after the request must still consume an attempt.
        db.session.commit()
        body = delivery.payload.encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Hookwise/Customer-Webhooks",
            "Idempotency-Key": delivery.id,
            "X-HookWise-Delivery-ID": delivery.id,
        }
        delivery.response_status = None
        try:
            if webhook.bearer_token:
                headers["Authorization"] = "Bearer " + decrypt_string(webhook.bearer_token)
            if webhook.signing_secret:
                timestamp = str(int(time.time()))
                signature = hmac.new(
                    decrypt_string(webhook.signing_secret).encode(),
                    timestamp.encode() + b"." + body,
                    hashlib.sha256,
                ).hexdigest()
                headers["X-HookWise-Timestamp"] = timestamp
                headers["X-HookWise-Signature"] = "sha256=" + signature
            code = post_webhook(decrypt_string(webhook.target_url), body, headers)
            delivery.response_status = code
            if 200 <= code < 300:
                delivery.status = "delivered"
                delivery.error_message = None
                delivery.completed_at = datetime.now(timezone.utc)
                result["delivered"] += 1
                db.session.commit()
                continue
            retryable = code in (408, 425, 429) or code >= 500
            delivery.error_message = f"Empfänger antwortete mit HTTP {code}."
        except Exception as exc:
            # Exception strings can contain URLs, query credentials or headers.
            delivery.error_message = f"Zustellung fehlgeschlagen ({type(exc).__name__})."
            retryable = not isinstance(exc, ValueError)
        delivery.status = "pending" if retryable and delivery.attempts < MAX_ATTEMPTS else "failed"
        delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=min(30 * 2**delivery.attempts, 1800))
        if delivery.status == "failed":
            delivery.completed_at = datetime.now(timezone.utc)
        result["failed"] += 1
        db.session.commit()
    return result
