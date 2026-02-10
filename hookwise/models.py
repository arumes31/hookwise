import secrets
import uuid
from datetime import datetime
from typing import Any, Dict

from .extensions import db


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
    json_mapping = db.Column(db.Text)  # JSON string for field mappings
    routing_rules = db.Column(db.Text) # JSON string for regex routing
    maintenance_windows = db.Column(db.Text) # JSON string for maintenance intervals
    trusted_ips = db.Column(db.Text) # Comma-separated IPs or CIDRs
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
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None
        }

class WebhookLog(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    config_id = db.Column(db.String(36), db.ForeignKey('webhook_config.id'), nullable=False)
    request_id = db.Column(db.String(100), nullable=False)
    payload = db.Column(db.Text, nullable=False) # JSON string
    status = db.Column(db.String(50), nullable=False, default="queued") # queued, processed, failed, skipped
    action = db.Column(db.String(50)) # create, update, close, None
    error_message = db.Column(db.Text)
    ticket_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    config = db.relationship('WebhookConfig', backref=db.backref('logs', lazy=True))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "config_id": self.config_id,
            "request_id": self.request_id,
            "payload": self.payload,
            "status": self.status,
            "action": self.action,
            "error_message": self.error_message,
            "ticket_id": self.ticket_id,
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
