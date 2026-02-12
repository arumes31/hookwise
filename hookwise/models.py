import secrets
import uuid
from datetime import datetime
from typing import Any, Dict

from .extensions import db


class User(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    otp_secret = db.Column(db.String(32))
    is_2fa_enabled = db.Column(db.Boolean, default=False, nullable=False)
    role = db.Column(db.String(20), default='user') # admin, user
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at.isoformat()
        }

class WebhookConfig(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), nullable=False)
    bearer_token = db.Column(db.String(512), nullable=False, default=lambda: __import__('hookwise.utils', fromlist=['encrypt_string']).encrypt_string(secrets.token_urlsafe(32)))
    customer_id_default = db.Column(db.String(50))
    board = db.Column(db.String(100))
    status = db.Column(db.String(100))
    ticket_type = db.Column(db.String(100))
    subtype = db.Column(db.String(100))
    item = db.Column(db.String(100))
    priority = db.Column(db.String(100))
    trigger_field = db.Column(db.String(100), default="heartbeat.status")
    open_value = db.Column(db.String(50), default="0")
    close_value = db.Column(db.String(50), default="1")
    ticket_prefix = db.Column(db.String(100))
    description_template = db.Column(db.Text)
    json_mapping = db.Column(db.Text)  # JSON string for field mappings
    routing_rules = db.Column(db.Text) # JSON string for regex routing
    maintenance_windows = db.Column(db.Text) # JSON string for maintenance intervals
    trusted_ips = db.Column(db.Text) # Comma-separated IPs or CIDRs
    hmac_secret = db.Column(db.String(256))
    is_enabled = db.Column(db.Boolean, default=True, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False, nullable=False)
    is_draft = db.Column(db.Boolean, default=False, nullable=False)
    display_order = db.Column(db.Integer, default=0)
    ai_rca_enabled = db.Column(db.Boolean, default=False, nullable=False)
    ai_prompt_template = db.Column(db.Text) # Custom instructions for the LLM
    last_rotated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bearer_token": self.bearer_token,
            "customer_id_default": self.customer_id_default,
            "board": self.board,
            "status": self.status,
            "ticket_type": self.ticket_type,
            "subtype": self.subtype,
            "item": self.item,
            "priority": self.priority,
            "trigger_field": self.trigger_field,
            "open_value": self.open_value,
            "close_value": self.close_value,
            "ticket_prefix": self.ticket_prefix,
            "json_mapping": self.json_mapping,
            "routing_rules": self.routing_rules,
            "maintenance_windows": self.maintenance_windows,
            "trusted_ips": self.trusted_ips,
            "is_enabled": self.is_enabled,
            "is_pinned": self.is_pinned,
            "ai_rca_enabled": self.ai_rca_enabled,
            "ai_prompt_template": self.ai_prompt_template,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None
        }

class WebhookLog(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id = db.Column(db.String(36), db.ForeignKey('webhook_config.id'), nullable=False)
    request_id = db.Column(db.String(100), nullable=False)
    payload = db.Column(db.Text, nullable=False) # JSON string
    headers = db.Column(db.Text) # JSON string
    status = db.Column(db.String(50), nullable=False, default="queued") # queued, processed, failed, skipped
    action = db.Column(db.String(50)) # create, update, close, None
    error_message = db.Column(db.Text)
    ticket_id = db.Column(db.Integer)
    matched_rule = db.Column(db.Text)
    processing_time = db.Column(db.Float) # in seconds
    source_ip = db.Column(db.String(50))
    retry_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    config = db.relationship('WebhookConfig', backref=db.backref('logs', lazy=True))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "config_id": self.config_id,
            "request_id": self.request_id,
            "payload": self.payload,
            "headers": self.headers,
            "status": self.status,
            "action": self.action,
            "error_message": self.error_message,
            "ticket_id": self.ticket_id,
            "matched_rule": self.matched_rule,
            "processing_time": self.processing_time,
            "source_ip": self.source_ip,
            "retry_count": self.retry_count,
            "created_at": self.created_at.isoformat(),
            "config_name": self.config.name if self.config else "Unknown"
        }

class AuditLog(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id = db.Column(db.String(36))
    action = db.Column(db.String(50), nullable=False) # create, update, delete, rotate_token
    user = db.Column(db.String(100))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "config_id": self.config_id,
            "action": self.action,
            "user": self.user,
            "details": self.details,
            "created_at": self.created_at.isoformat()
        }
