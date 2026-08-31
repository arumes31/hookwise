import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict

from sqlalchemy.orm import Mapped, validates

from .extensions import db

if TYPE_CHECKING:

    class Base:
        query: Any

        def __init__(self, **kwargs: Any) -> None: ...
else:
    Base = db.Model


endpoint_tag_association = db.Table(
    "endpoint_tag_association",
    db.Column("config_id", db.String(64), db.ForeignKey("webhook_config.id", ondelete="CASCADE"), primary_key=True),
    db.Column("tag_id", db.String(36), db.ForeignKey("endpoint_tag.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    otp_secret = db.Column(db.String(256))
    is_2fa_enabled = db.Column(db.Boolean, default=False, nullable=False)
    role = db.Column(db.String(20), default="user")  # admin, user
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "username": self.username, "role": self.role, "created_at": self.created_at.isoformat()}


class WebhookConfig(Base):
    __table_args__ = (db.CheckConstraint("timeout_hours >= 0", name="webhook_config_timeout_hours_check"),)

    id = db.Column(db.String(64), primary_key=True, default=lambda: secrets.token_urlsafe(48))
    name = db.Column(db.String(100), nullable=False)
    bearer_token = db.Column(
        db.String(512),
        nullable=False,
        default=lambda: __import__("hookwise.utils", fromlist=["encrypt_string"]).encrypt_string(
            secrets.token_urlsafe(32)
        ),
    )
    customer_id_default = db.Column(db.String(50))
    board = db.Column(db.String(100))
    status = db.Column(db.String(100))
    close_status = db.Column(db.String(100))
    ticket_type = db.Column(db.String(100))
    subtype = db.Column(db.String(100))
    item = db.Column(db.String(100))
    priority = db.Column(db.String(100))
    trigger_field = db.Column(db.String(100), default="heartbeat.status")
    open_value = db.Column(db.String(50), default="0")
    close_value = db.Column(db.String(50), default="1")
    ticket_prefix = db.Column(db.String(100))
    description_template = db.Column(db.Text)
    summary_remove_strings = db.Column(db.String(500), nullable=True)
    json_mapping = db.Column(db.Text)  # JSON string for field mappings
    routing_rules = db.Column(db.Text)  # JSON string for regex routing
    maintenance_windows = db.Column(db.Text)  # JSON string for maintenance intervals
    trusted_ips = db.Column(db.Text)  # Comma-separated IPs or CIDRs
    hmac_secret = db.Column(db.String(256))
    allow_unauthenticated = db.Column(db.Boolean, default=False, nullable=False)
    is_enabled = db.Column(db.Boolean, default=True, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False, nullable=False)
    is_draft = db.Column(db.Boolean, default=False, nullable=False)
    display_order = db.Column(db.Integer, default=0)
    ai_rca_enabled = db.Column(db.Boolean, default=False, nullable=False)
    ai_prompt_template = db.Column(db.Text)  # Custom instructions for the LLM
    global_routing_enabled = db.Column(db.Boolean, default=False, nullable=False)

    # Health & Security
    config_health_status = db.Column(db.String(20), default="OK")  # OK, WARNING, ERROR
    config_health_message = db.Column(db.String(255), nullable=True)
    last_ip = db.Column(db.String(45), nullable=True)

    last_rotated_at = db.Column(db.DateTime)
    bearer_auth_enabled = db.Column(db.Boolean, default=True, nullable=False)
    # A non-secret suffix used only for safe support/search UX. The encrypted
    # token remains the source of truth and is never decrypted for list queries.
    bearer_token_last4 = db.Column(db.String(4), nullable=True, index=True)

    # Endpoint-level delivery controls. Values are bounded at the mutation
    # boundary and again defensively by the task retry implementation.
    rate_limit_per_minute = db.Column(db.Integer, default=60, nullable=False)
    retry_enabled = db.Column(db.Boolean, default=True, nullable=False)
    retry_max_attempts = db.Column(db.Integer, default=5, nullable=False)
    retry_base_delay_seconds = db.Column(db.Integer, default=1, nullable=False)
    retry_max_delay_seconds = db.Column(db.Integer, default=300, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at = db.Column(db.DateTime)
    tags: Mapped[list["EndpointTag"]] = db.relationship(
        "EndpointTag", secondary=endpoint_tag_association, back_populates="configs", lazy="select"
    )  # type: ignore[assignment]

    # Timeout Monitoring
    timeout_alerts_enabled = db.Column(db.Boolean, default=False, nullable=False)
    timeout_hours = db.Column(db.Integer, default=24, nullable=False)
    timeout_ticket_id = db.Column(db.Integer, nullable=True)
    last_stale_alert_at = db.Column(db.DateTime, nullable=True)

    @validates("bearer_token", "hmac_secret")
    def _encrypt_secret(self, key: str, value: str | None) -> str | None:
        """Ensure secrets assigned through the model are never stored as plaintext."""
        if not value:
            return value
        from .utils import ensure_encrypted

        return ensure_encrypted(value)

    def to_dict(self, include_token: bool = False) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "customer_id_default": self.customer_id_default,
            "board": self.board,
            "status": self.status,
            "close_status": self.close_status,
            "ticket_type": self.ticket_type,
            "subtype": self.subtype,
            "item": self.item,
            "priority": self.priority,
            "trigger_field": self.trigger_field,
            "open_value": self.open_value,
            "close_value": self.close_value,
            "ticket_prefix": self.ticket_prefix,
            "description_template": self.description_template,
            "summary_remove_strings": self.summary_remove_strings,
            "json_mapping": self.json_mapping,
            "routing_rules": self.routing_rules,
            "maintenance_windows": self.maintenance_windows,
            "trusted_ips": self.trusted_ips,
            "bearer_auth_enabled": self.bearer_auth_enabled,
            "allow_unauthenticated": self.allow_unauthenticated,
            "bearer_token_last4": self.bearer_token_last4,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "retry_enabled": self.retry_enabled,
            "retry_max_attempts": self.retry_max_attempts,
            "retry_base_delay_seconds": self.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "is_enabled": self.is_enabled,
            "is_pinned": self.is_pinned,
            "ai_rca_enabled": self.ai_rca_enabled,
            "ai_prompt_template": self.ai_prompt_template,
            "global_routing_enabled": self.global_routing_enabled,
            "config_health_status": self.config_health_status,
            "config_health_message": self.config_health_message,
            "last_ip": self.last_ip,
            "timeout_alerts_enabled": self.timeout_alerts_enabled,
            "timeout_hours": self.timeout_hours,
            "timeout_ticket_id": self.timeout_ticket_id,
            "last_stale_alert_at": self.last_stale_alert_at.isoformat() if self.last_stale_alert_at else None,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }
        if include_token:
            d["bearer_token"] = self.bearer_token
            d["hmac_secret"] = self.hmac_secret
        return d


class WebhookLog(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id = db.Column(
        db.String(64), db.ForeignKey("webhook_config.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id = db.Column(db.String(100), nullable=False, index=True)
    payload = db.Column(db.Text, nullable=False)  # JSON string
    headers = db.Column(db.Text)  # JSON string
    status = db.Column(
        db.String(50), nullable=False, default="queued", index=True
    )  # queued, processed, failed, skipped
    action = db.Column(db.String(50))  # create, update, close, None
    error_message = db.Column(db.Text)
    ticket_id = db.Column(db.Integer)
    matched_rule = db.Column(db.Text)
    processing_time = db.Column(db.Float)  # in seconds
    source_ip = db.Column(db.String(50), index=True)
    retry_count = db.Column(db.Integer, default=0)
    correlation_id = db.Column(db.String(100), nullable=True, index=True)
    received_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    queued_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    processing_started_at = db.Column(db.DateTime, nullable=True)
    connectwise_started_at = db.Column(db.DateTime, nullable=True)
    connectwise_responded_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    connectwise_duration_ms = db.Column(db.Integer, nullable=True)
    error_type = db.Column(db.String(100), nullable=True, index=True)
    error_chain = db.Column(db.Text, nullable=True)  # JSON; sanitized error events
    replay_of_log_id = db.Column(
        db.String(36), db.ForeignKey("webhook_log.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retry_exhausted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    # Composite index for the common history query: filter by endpoint + status,
    # ordered by recency. Complements the single-column indexes above.
    __table_args__ = (
        db.UniqueConstraint("config_id", "request_id", name="uq_webhook_log_config_request"),
        db.Index("ix_webhook_log_config_status_created", "config_id", "status", "created_at"),
        db.Index("ix_webhook_log_config_request", "config_id", "request_id"),
        db.Index("ix_webhook_log_status_created", "status", "created_at"),
        db.Index("ix_webhook_log_ticket_id", "ticket_id"),
        db.Index("ix_webhook_log_processing_time", "processing_time"),
    )

    config = db.relationship(
        "WebhookConfig", backref=db.backref("logs", lazy=True, cascade="all, delete-orphan", passive_deletes=True)
    )

    @staticmethod
    def _masked_json(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            from .utils import mask_secrets

            return json.dumps(mask_secrets(json.loads(value)))
        except TypeError, json.JSONDecodeError:
            return value

    @property
    def masked_payload(self) -> str:
        return self._masked_json(self.payload) or "{}"

    @property
    def masked_headers(self) -> str | None:
        return self._masked_json(self.headers)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "config_id": self.config_id,
            "request_id": self.request_id,
            "payload": self.masked_payload,
            "headers": self.masked_headers,
            "status": self.status,
            "action": self.action,
            "error_message": self.error_message,
            "ticket_id": self.ticket_id,
            "matched_rule": self.matched_rule,
            "processing_time": self.processing_time,
            "source_ip": self.source_ip,
            "retry_count": self.retry_count,
            "correlation_id": self.correlation_id,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "processing_started_at": self.processing_started_at.isoformat() if self.processing_started_at else None,
            "connectwise_started_at": self.connectwise_started_at.isoformat() if self.connectwise_started_at else None,
            "connectwise_responded_at": self.connectwise_responded_at.isoformat()
            if self.connectwise_responded_at
            else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "connectwise_duration_ms": self.connectwise_duration_ms,
            "error_type": self.error_type,
            "error_chain": self.error_chain,
            "replay_of_log_id": self.replay_of_log_id,
            "retry_exhausted_at": self.retry_exhausted_at.isoformat() if self.retry_exhausted_at else None,
            "created_at": self.created_at.isoformat(),
            "config_name": self.config.name if self.config else "Unknown",
        }


class DeliveryOutbox(Base):
    """Durable task-dispatch intent committed atomically with a webhook log."""

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    log_id = db.Column(
        db.String(36), db.ForeignKey("webhook_log.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    task_name = db.Column(db.String(100), nullable=False, default="hookwise.process_webhook")
    arguments = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    dispatched_at = db.Column(db.DateTime, nullable=True)

    log = db.relationship(
        "WebhookLog",
        backref=db.backref("outbox", uselist=False, cascade="all, delete-orphan", passive_deletes=True),
    )


class TicketOperation(Base):
    """Idempotency record for external ticket mutations."""

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    log_id = db.Column(db.String(36), db.ForeignKey("webhook_log.id", ondelete="CASCADE"), nullable=False, index=True)
    operation = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="started")
    ticket_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (db.UniqueConstraint("log_id", "operation", name="uq_ticket_operation_log_operation"),)


class WebhookRetryAttempt(Base):
    """An immutable record of a delivery attempt for a webhook log."""

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    log_id = db.Column(db.String(36), db.ForeignKey("webhook_log.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_number = db.Column(db.Integer, nullable=False)
    scheduled_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    retry_interval_seconds = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(30), default="scheduled", nullable=False, index=True)
    error_message = db.Column(db.Text, nullable=True)

    __table_args__ = (db.UniqueConstraint("log_id", "attempt_number", name="uq_retry_attempt_log_number"),)

    log = db.relationship(
        "WebhookLog",
        backref=db.backref("retry_attempts", lazy=True, cascade="all, delete-orphan", passive_deletes=True),
        foreign_keys=[log_id],
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "log_id": self.log_id,
            "attempt_number": self.attempt_number,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "retry_interval_seconds": self.retry_interval_seconds,
            "status": self.status,
            "error_message": self.error_message,
        }


class EndpointTag(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    configs: Mapped[list[WebhookConfig]] = db.relationship(
        "WebhookConfig", secondary=endpoint_tag_association, back_populates="tags", lazy="select"
    )  # type: ignore[assignment]


class UserPreference(Base):
    """Owner-scoped persisted UI settings; JSON values are validated by APIs."""

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(
        db.String(36), db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    dashboard_layout = db.Column(db.Text, nullable=True)
    hidden_kpis = db.Column(db.Text, nullable=True)
    dashboard_compact_mode = db.Column(db.Boolean, default=False, nullable=False)
    dashboard_refresh_interval = db.Column(db.Integer, default=30, nullable=False)
    timezone = db.Column(db.String(64), nullable=True)
    activity_buffer_size = db.Column(db.Integer, default=200, nullable=False)
    browser_notifications_enabled = db.Column(db.Boolean, default=False, nullable=False)
    sound_notifications_enabled = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = db.relationship("User", backref=db.backref("preference", uselist=False, cascade="all, delete-orphan"))


class SavedHistorySearch(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    filters = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = db.relationship(
        "User", backref=db.backref("saved_history_searches", lazy=True, cascade="all, delete-orphan")
    )


class EventAnnotation(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    log_id = db.Column(db.String(36), db.ForeignKey("webhook_log.id", ondelete="CASCADE"), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = db.relationship("User", backref=db.backref("event_annotations", lazy=True, cascade="all, delete-orphan"))
    log = db.relationship("WebhookLog", backref=db.backref("annotations", lazy=True, cascade="all, delete-orphan"))


class AuditLog(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id = db.Column(db.String(64))
    action = db.Column(db.String(50), nullable=False)  # create, update, delete, rotate_token
    user = db.Column(db.String(100))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "config_id": self.config_id,
            "action": self.action,
            "user": self.user,
            "details": self.details,
            "created_at": self.created_at.isoformat(),
        }


class GlobalMapping(Base):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_value = db.Column(db.String(255), nullable=False, unique=True, index=True)
    company_id = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_value": self.tenant_value,
            "company_id": self.company_id,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
        }
