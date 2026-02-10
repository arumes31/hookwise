import uuid
import secrets
from datetime import datetime
from typing import Any, Dict
from .extensions import db

class WebhookConfig(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), nullable=False)
    bearer_token = db.Column(db.String(100), nullable=False, default=lambda: secrets.token_urlsafe(32))
    customer_id_default = db.Column(db.String(50))
    board = db.Column(db.String(100))
    status = db.Column(db.String(100))
    ticket_type = db.Column(db.String(100))
    subtype = db.Column(db.String(100))
    priority = db.Column(db.String(100))
    trigger_field = db.Column(db.String(100), default="heartbeat.status")
    open_value = db.Column(db.String(50), default="0")
    close_value = db.Column(db.String(50), default="1")
    ticket_prefix = db.Column(db.String(100))
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
            "priority": self.priority,
            "trigger_field": self.trigger_field,
            "open_value": self.open_value,
            "close_value": self.close_value,
            "ticket_prefix": self.ticket_prefix,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None
        }
