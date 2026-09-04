"""Der Permission-Katalog.

Bewusst Code und keine Tabelle: Eine Berechtigung entsteht mit dem Code, der
sie prueft. Eine eigene Tabelle waere eine zweite Kopie, die auseinanderlaufen
kann -- Zuweisungen sind Daten, der Katalog nicht (ADR-004).

Schema: ``ressource:aktion``.
"""

from typing import Dict, FrozenSet, List, Tuple

# Gruppen nur fuer die Darstellung in der Rechtematrix; die Reihenfolge ist die
# der Oberflaeche.
PERMISSION_GROUPS: List[Tuple[str, List[Tuple[str, str]]]] = [
    (
        "Dashboard",
        [("dashboard:read", "Dashboard, KPIs and analytics")],
    ),
    (
        "Endpoints",
        [
            ("endpoint:read", "List, detail drawer, export"),
            ("endpoint:write", "Create, edit, clone, import"),
            ("endpoint:archive", "Archive and restore"),
            ("endpoint:test", "Test webhook and dry run"),
        ],
    ),
    (
        "Secrets",
        [
            ("secret:reveal", "Show bearer token and HMAC secret"),
            ("secret:rotate", "Rotate tokens"),
        ],
    ),
    (
        "History",
        [
            ("history:read", "Delivery history, payloads, diagnosis"),
            ("history:retry", "Retry and resend"),
            ("history:delete", "Delete entries and full history"),
        ],
    ),
    (
        "Operations",
        [
            ("tenantmap:read", "View tenant mapping"),
            ("tenantmap:write", "Maintain tenant mapping"),
            ("audit:read", "Read the audit log"),
            ("settings:read", "View system settings"),
            ("settings:write", "System settings, LLM, backups"),
            ("user:read", "View users and roles"),
            ("user:manage", "Manage users and roles"),
        ],
    ),
]

ALL_PERMISSIONS: FrozenSet[str] = frozenset(
    schluessel for _gruppe, rechte in PERMISSION_GROUPS for schluessel, _text in rechte
)

PERMISSION_TEXTS: Dict[str, str] = {
    schluessel: text for _gruppe, rechte in PERMISSION_GROUPS for schluessel, text in rechte
}

# Rechte, die eine automatisch angelegte Rolle niemals mitbringen darf: Wer sich
# selbst Rechte geben oder Zustell-Credentials lesen kann, ist kein Startzustand
# fuer einen frisch provisionierten Account (ADR-002).
PRIVILEGED_PERMISSIONS: FrozenSet[str] = frozenset(
    {"user:manage", "secret:reveal", "secret:rotate", "settings:write", "history:delete"}
)

_VIEWER: FrozenSet[str] = frozenset(
    {"dashboard:read", "endpoint:read", "history:read", "tenantmap:read", "settings:read"}
)
_OPERATOR: FrozenSet[str] = _VIEWER | frozenset(
    {
        "endpoint:write",
        "endpoint:archive",
        "endpoint:test",
        "history:retry",
        "tenantmap:write",
        "audit:read",
    }
)

# Eingebaute Rollen. is_builtin verhindert Bearbeiten und Loeschen, damit ein
# Update nie stillschweigend aendert, was eine Rolle gewaehrt.
ROLE_PRESETS: Dict[str, Dict[str, object]] = {
    "admin": {
        "name": "Administrator",
        "description": "Full access including secrets, settings and user management.",
        "permissions": ALL_PERMISSIONS,
    },
    "operator": {
        "name": "Operator",
        "description": "Runs the platform day to day, without secrets or user management.",
        "permissions": _OPERATOR,
    },
    "viewer": {
        "name": "Viewer",
        "description": "Read-only access to dashboard, endpoints and history.",
        "permissions": _VIEWER,
    },
}

# Bruecke aus der alten Ein-Rollen-Spalte. Greift, solange ein Nutzer noch keine
# Zuweisung hat oder das RBAC-Schema fehlt -- die Anwendung verhaelt sich dann
# exakt wie vor der Einfuehrung.
LEGACY_ROLE_MAP: Dict[str, FrozenSet[str]] = {
    "admin": ALL_PERMISSIONS,
    "user": _OPERATOR,
    "operator": _OPERATOR,
    "viewer": _VIEWER,
}

LEGACY_FALLBACK: FrozenSet[str] = _VIEWER


def permissions_for_legacy_role(role: str | None) -> FrozenSet[str]:
    """Rechte fuer einen Wert der alten ``User.role``-Spalte."""
    if not role:
        return LEGACY_FALLBACK
    return LEGACY_ROLE_MAP.get(role.strip().lower(), LEGACY_FALLBACK)


def is_assignable_start_role(permissions: FrozenSet[str]) -> bool:
    """Taugt die Rolle als Startrolle fuer Auto-Provisioning?"""
    return not (permissions & PRIVILEGED_PERMISSIONS)
