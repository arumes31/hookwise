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
    old_key = decrypt_string(webhook.pagerduty_routing_key) if webhook.pagerduty_routing_key else None
    submitted_key = credentials.get("pagerduty_routing_key")
    key_changed = (
        bool(webhook.target_url)
        and "pagerduty_routing_key" in credentials
        and old_key != (decrypt_string(submitted_key) if submitted_key else None)
    )
    if changed:
        for field in ("bearer_token", "signing_secret", "pagerduty_routing_key"):
            old = getattr(webhook, field)
            new = credentials.get(field)
            # Replayed backup ciphertext is not a new credential for a new receiver.
            if drop_replayed and old and new and decrypt_string(old) == decrypt_string(new):
                credentials[field] = None
            setattr(webhook, field, None)
    if changed or key_changed:
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


def set_payload_format(
    webhook: CustomerWebhook, payload_format: str, routing_key: str | None = None, *, clear: bool = False
) -> None:
    """Explicitly select the wire protocol; never reuse a generic bearer token."""
    from .pagerduty_events import validate_pagerduty_url

    if payload_format not in ("hookwise", "pagerduty"):
        raise ValueError("Ungültiges Webhook-Format.")
    key = None if clear else webhook.pagerduty_routing_key
    if routing_key:
        if not re.fullmatch(r"[A-Za-z0-9]{32}", routing_key):
            raise ValueError("Der PagerDuty Integration Key muss 32 Buchstaben/Ziffern enthalten.")
        key = ensure_encrypted(routing_key)
    if payload_format == "pagerduty":
        for url in destination_urls(webhook):
            validate_pagerduty_url(url)
        if not key:
            raise ValueError("Für PagerDuty ist ein Integration Key erforderlich (kein REST-API-Token).")
        if not re.fullmatch(r"[A-Za-z0-9]{32}", decrypt_string(key)):
            raise ValueError("Der gespeicherte PagerDuty Integration Key ist ungültig.")
    else:
        key = None
    old_format = webhook.payload_format or "hookwise"
    old_key = decrypt_string(webhook.pagerduty_routing_key) if webhook.pagerduty_routing_key else None
    new_key = decrypt_string(key) if key else None
    if old_format != payload_format or old_key != new_key:
        webhook.destination_generation = (webhook.destination_generation or 1) + 1
        CustomerWebhookDelivery.query.filter(
            CustomerWebhookDelivery.webhook_id == webhook.id,
            CustomerWebhookDelivery.status.in_(("pending", "sending")),
        ).update(
            {"status": "cancelled", "payload": "{}", "completed_at": datetime.now(timezone.utc)},
            synchronize_session="fetch",
        )
    webhook.payload_format, webhook.pagerduty_routing_key = payload_format, key


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


def _company_resolution(config: Any, data: dict[str, Any]) -> tuple[str | None, str]:
    """Keep the production routing decision and its diagnostic reason together."""
    from ..tasks import _global_mapping_for_tenant, get_all_global_mappings

    decision = evaluate_routing(data, config.to_dict())
    if decision.values.get("drop"):
        return None, "routing_drop"
    if decision.alert_type == "UP":
        return None, "recovery"
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
            return None, "tenant_unmapped"
        company = mapping.get("company_id")
    if not company:
        company = config.customer_id_default or os.environ.get("CW_DEFAULT_COMPANY_ID")
    return (str(company).strip() or None, "resolved") if company else (None, "tenant_unmapped")


def _sanitized_alert(data: Any) -> Any:
    """Do not forward CIPP identity headers or its private ingress URL metadata."""
    if isinstance(data, list):
        return [_sanitized_alert(item) for item in data]
    if not isinstance(data, dict):
        return data
    sanitized = mask_secrets(data)
    for key, value in sanitized.items():
        if key.lower().startswith("x-ms-client-principal") or key.lower() in (
            "cookie",
            "set-cookie",
            "postexecutionresults",
            "headers",
        ):
            sanitized[key] = "***"
        else:
            sanitized[key] = _sanitized_alert(value)
    return sanitized


@dataclass
class ForwardingEvaluation:
    eligible: bool
    code: str
    message: str
    company_id: str | None = None
    matches: list[tuple[float | None, Any]] | None = None
    targets: list[str] | None = None


def evaluate_customer_webhook(webhook: CustomerWebhook, data: dict[str, Any]) -> ForwardingEvaluation:
    """Evaluate the saved connector without network calls or database mutations."""
    from ..tasks import is_in_maintenance

    def blocked(code: str, message: str, company: str | None = None) -> ForwardingEvaluation:
        return ForwardingEvaluation(False, code, message, company)

    config = db.session.get(WebhookConfig, webhook.config_id)
    if not webhook.is_enabled:
        return blocked("connector_disabled", "Der Forwarding-Connector ist pausiert.")
    if not config or not config.is_enabled:
        return blocked("endpoint_disabled", "Der Quell-Endpoint fehlt oder ist pausiert.")
    if config.archived_at:
        return blocked("archived", "Der Quell-Endpoint ist archiviert.")
    if config.is_draft:
        return blocked("draft", "Der Quell-Endpoint ist ein Entwurf.")
    if not config.forwarding_enabled:
        return blocked("forwarding_disabled", "Forwarding ist im Quell-Endpoint deaktiviert.")
    if is_in_maintenance(config):
        return blocked("maintenance", "Für den Quell-Endpoint ist ein Wartungsfenster aktiv.")
    company_id, reason = _company_resolution(config, data)
    if reason == "routing_drop":
        return blocked(reason, "Eine Routing-Regel verwirft diesen Alarm.")
    if reason == "recovery":
        return blocked(reason, "Der Endpoint erkennt diese Meldung als Entwarnung (Recovery).")
    if not company_id:
        return blocked("tenant_unmapped", "Keine eindeutige Kundenzuordnung. TenantMap und Endpoint-Routing prüfen.")
    if webhook.company_id != company_id:
        return blocked("company_mismatch", "Der ermittelte Kunde stimmt nicht mit dem Connector überein.", company_id)
    try:
        targets = destination_urls(webhook)
        if webhook.payload_format == "pagerduty":
            from .pagerduty_events import validate_pagerduty_url

            for url in targets:
                validate_pagerduty_url(url)
            if not webhook.pagerduty_routing_key:
                return blocked("invalid_destinations", "Der PagerDuty Integration Key fehlt.", company_id)
        elif any(urlsplit(url).hostname in ("events.pagerduty.com", "events.eu.pagerduty.com") for url in targets):
            return blocked(
                "payload_format_mismatch",
                "Dieses Ziel ist PagerDuty. Webhook-Format auf PagerDuty Events v2 umstellen "
                "und Integration Key hinterlegen.",
                company_id,
            )
    except ValueError:
        return blocked(
            "invalid_destinations", "Gespeicherte Ziel-URLs sind ungültig oder nicht entschlüsselbar.", company_id
        )
    matched: list[tuple[float | None, Any]]
    if webhook.score_path:
        try:
            validate_score_path(webhook.score_path)
        except ValueError:
            return blocked("invalid_score_path", "Der gespeicherte Score-Pfad ist ungültig.", company_id)
        found = parse(webhook.score_path).find(data)
        if not found:
            return blocked(
                "score_missing",
                "Das konfigurierte Score-Feld fehlt. CIPP-Severity ist kein numerischer Score; "
                "für Meldungen ohne Score den Score-Filter deaktivieren.",
                company_id,
            )
        numeric = [(numeric_score(match.value), match.context.value) for match in found]
        if not any(score is not None for score, _ in numeric):
            return blocked(
                "score_invalid", "Die gefundenen Score-Werte sind nicht numerisch (z. B. Severity).", company_id
            )
        matched = [(score, alert) for score, alert in numeric if score is not None and score >= webhook.minimum_score]
        if not matched:
            return blocked("score_below_threshold", "Kein Score erreicht den eingestellten Mindestscore.", company_id)
    else:
        matched = [(None, data)]
    matches = [(score, _sanitized_alert(alert)) for score, alert in matched]
    return ForwardingEvaluation(True, "ready", "Kundenzuordnung und Filter passen.", company_id, matches, targets)


def _alert_key(company_id: str, alert: Any) -> str:
    canonical = json.dumps(alert, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{company_id}:{canonical}".encode()).hexdigest()


def _target_key(event_key: str, target_index: int) -> str:
    # Keep the legacy key for the first target across upgrades.
    return event_key if target_index == 0 else hashlib.sha256(f"{event_key}:{target_index}".encode()).hexdigest()


def build_alert_payload(
    webhook: CustomerWebhook,
    request_id: str,
    score: float | None,
    alert: Any,
    *,
    test: bool = False,
) -> dict[str, Any]:
    source = _public_source(webhook.config_id, webhook.config.name)
    payload = {
        "event": "hookwise.customer_alert",
        "version": 1,
        "company_id": webhook.company_id,
        "source_endpoint_id": source["id"],
        "source": source,
        "request_id": request_id,
        "score": score,
        "minimum_score": webhook.minimum_score if webhook.score_path else None,
        "alert": alert,
    }
    if test:
        payload["test"] = True
    return payload


def _public_source(config_id: str, name: str) -> dict[str, str]:
    # The real config ID is the private /w/<id> ingress token.
    return {"id": hashlib.sha256(f"hookwise-source:{config_id}".encode()).hexdigest(), "name": name}


def stage_customer_deliveries(log: WebhookLog, data: dict[str, Any]) -> None:
    """Persist matching alarms atomically with ingress, before any ticket API call."""
    subscriptions = CustomerWebhook.query.filter_by(config_id=log.config_id, is_enabled=True).populate_existing().all()
    for webhook in subscriptions:
        evaluation = evaluate_customer_webhook(webhook, data)
        if not evaluation.eligible:
            log_message = (
                logger.warning if evaluation.code in ("invalid_score_path", "invalid_destinations") else logger.info
            )
            reason = "invalid score_path" if evaluation.code == "invalid_score_path" else evaluation.code
            log_message("Skipping Forwarding subscription %s: %s", webhook.id, reason)
            continue
        for score, alert in evaluation.matches or []:
            event_key = _alert_key(webhook.company_id, alert)
            payload = build_alert_payload(webhook, log.request_id, score, alert)
            for target_index in range(len(evaluation.targets or [])):
                target_key = _target_key(event_key, target_index)
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
    payload_format: str = "hookwise"
    pagerduty_routing_key: str | None = None


def preview_delivery_payload(webhook: CustomerWebhook, payload: dict[str, Any]) -> dict[str, Any]:
    """Display the real wire format with an explicit placeholder for the key."""
    if webhook.payload_format == "pagerduty":
        from .pagerduty_events import build_pagerduty_event

        return build_pagerduty_event({**payload, "id": "dry-run"}, "***")
    return payload


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
        if attempt.payload_format == "pagerduty":
            from .pagerduty_events import build_pagerduty_event, validate_pagerduty_url

            validate_pagerduty_url(target_url)
            if not attempt.pagerduty_routing_key:
                raise ValueError("PagerDuty key missing")
            event = build_pagerduty_event(
                {**json.loads(body), "id": delivery_id}, decrypt_string(attempt.pagerduty_routing_key)
            )
            body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        elif attempt.payload_format != "hookwise":
            raise ValueError("Unsupported payload format")
        elif bearer_token:
            headers["Authorization"] = "Bearer " + decrypt_string(bearer_token)
        if signing_secret and attempt.payload_format == "hookwise":
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
        payload_format, routing_key = webhook.payload_format or "hookwise", webhook.pagerduty_routing_key
        # Also protect envelopes staged by an older worker before this upgrade.
        try:
            stored_payload = _sanitized_alert(json.loads(delivery.payload))
            stored_payload["source"] = _public_source(config.id, config.name)
            stored_payload["source_endpoint_id"] = stored_payload["source"]["id"]
            body = json.dumps(stored_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except ValueError, TypeError, RecursionError:
            delivery.status, delivery.payload = "failed", "{}"
            delivery.error_message = "Ungültiger gespeicherter Alarm."
            delivery.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            result["failed"] += 1
            continue
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
        jobs.append(
            DeliveryAttempt(
                delivery_id,
                attempts,
                claim_until,
                target_url,
                bearer_token,
                signing_secret,
                body,
                payload_format,
                routing_key,
            )
        )
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
