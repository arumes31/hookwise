"""Rollen- und Rechteverwaltung.

Der Katalog liegt im Code (``catalog``), Rollen und Zuweisungen in der
Datenbank. Das Schema bringt ``schema_bridge`` selbst mit, damit ein
Deployment ohne Migrationsschritt auskommt; solange es fehlt, greift der
Legacy-Fallback in ``resolver`` auf die alte ``User.role``-Spalte zurueck.
"""

from .catalog import ALL_PERMISSIONS, LEGACY_ROLE_MAP, PERMISSION_GROUPS, ROLE_PRESETS
from .decorators import public_endpoint, requires, verify_route_coverage
from .resolver import bump_epoch, current_permissions, has_permission, resolve_permissions
from .schema_bridge import ensure_rbac_schema, rbac_schema_state

__all__ = [
    "ALL_PERMISSIONS",
    "LEGACY_ROLE_MAP",
    "PERMISSION_GROUPS",
    "ROLE_PRESETS",
    "bump_epoch",
    "current_permissions",
    "ensure_rbac_schema",
    "has_permission",
    "public_endpoint",
    "rbac_schema_state",
    "requires",
    "resolve_permissions",
    "verify_route_coverage",
]
