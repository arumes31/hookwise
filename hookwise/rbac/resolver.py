"""Rechteauflösung.

Rechte in der Session zu cachen ist schnell, aber ein Rollenentzug wuerde erst
beim naechsten Login greifen. Deshalb der Permissions-Epoch: ein Zaehler, den
jede Rechteaenderung erhoeht. Stimmt der Stand in der Session nicht mehr,
loest der naechste Request die Rechte neu auf -- ein DB-Roundtrip statt einer
Wartezeit bis zum naechsten Login.
"""

import logging
import time
from typing import Any, FrozenSet, Optional, Set, Tuple

from flask import current_app, session
from sqlalchemy.exc import IntegrityError

from .catalog import ALL_PERMISSIONS, permissions_for_legacy_role

_logger = logging.getLogger(__name__)

SESSION_PERMS = "perms"
SESSION_EPOCH = "perms_epoch"
# Der Cache gilt nur fuer den Nutzer, fuer den er geschrieben wurde -- wechselt
# die user_id in einer bestehenden Session, wird neu aufgeloest.
SESSION_UID = "perms_uid"

# Der Epoch wird pro Request gelesen; ein kurzer Prozess-Cache haelt die Last
# von der Datenbank fern, ohne dass ein Entzug spuerbar verzoegert wirkt.
_EPOCH_CACHE: dict[str, float | int] = {"wert": 1, "bis": 0.0}
_EPOCH_TTL = 5.0


_SCHEMA_CACHE: dict[str, float] = {"bis": 0.0}
_SCHEMA_TTL = 10.0


def schema_bereit() -> bool:
    """Steht das RBAC-Schema?

    Beim Start kann die Antwort noch "nein" lauten -- etwa weil die Tabellen
    erst danach angelegt werden (Tests nutzen create_all nach dem App-Bau).
    Solange die Antwort negativ ist, wird sie gelegentlich neu gestellt; sobald
    sie positiv ist, bleibt sie es.
    """
    if current_app.config.get("RBAC_SCHEMA_OK"):
        return True
    jetzt = time.monotonic()
    if jetzt < _SCHEMA_CACHE["bis"]:
        return False
    _SCHEMA_CACHE["bis"] = jetzt + _SCHEMA_TTL
    try:
        from ..extensions import db
        from .schema_bridge import rbac_schema_state, seed_builtin_roles

        zustand = rbac_schema_state(db.engine)
        if zustand["vollstaendig"]:
            current_app.config["RBAC_SCHEMA_OK"] = True
            try:
                seed_builtin_roles()
            except Exception:  # pragma: no cover
                db.session.rollback()
            return True
    except Exception:  # pragma: no cover
        pass
    return False


def aktueller_epoch(frisch: bool = False) -> int:
    """Aktueller Stand des Rechte-Zaehlers."""
    if not schema_bereit():
        return 0
    jetzt = time.monotonic()
    if not frisch and jetzt < float(_EPOCH_CACHE["bis"]):
        return int(_EPOCH_CACHE["wert"])
    try:
        from ..models import RbacMeta

        zeile = RbacMeta.query.get(1)
        wert = int(zeile.permissions_epoch) if zeile else 1
    except Exception:  # pragma: no cover
        wert = int(_EPOCH_CACHE["wert"])
    _EPOCH_CACHE["wert"] = wert
    _EPOCH_CACHE["bis"] = jetzt + _EPOCH_TTL
    return wert


def bump_epoch() -> int:
    """Nach jeder Rollen- oder Zuweisungsaenderung aufrufen."""
    if not schema_bereit():
        return 0
    from ..extensions import db
    from ..models import RbacMeta

    # Im SQL hochzaehlen statt lesen-rechnen-schreiben: zwei gleichzeitige
    # Rechteaenderungen wuerden sich sonst gegenseitig ueberschreiben.
    geaendert = (
        db.session.query(RbacMeta)
        .filter_by(id=1)
        .update({RbacMeta.permissions_epoch: RbacMeta.permissions_epoch + 1}, synchronize_session=False)
    )
    if not geaendert:
        try:
            db.session.add(RbacMeta(id=1, permissions_epoch=2))
            db.session.commit()
        except IntegrityError:
            # Ein paralleler Prozess war schneller -- dann zaehlt dessen Zeile.
            db.session.rollback()
            db.session.query(RbacMeta).filter_by(id=1).update(
                {RbacMeta.permissions_epoch: RbacMeta.permissions_epoch + 1}, synchronize_session=False
            )
            db.session.commit()
    else:
        db.session.commit()

    zeile = RbacMeta.query.get(1)
    wert = int(zeile.permissions_epoch) if zeile else 1
    _EPOCH_CACHE["wert"] = wert
    _EPOCH_CACHE["bis"] = time.monotonic() + _EPOCH_TTL
    return wert


def resolve_permissions(user: Any) -> FrozenSet[str]:
    """Effektive Rechte eines Nutzers.

    Reihenfolge: zugewiesene Rollen, sonst der alte ``role``-String. Damit
    verhaelt sich die Anwendung ohne RBAC-Schema und ohne Zuweisungen exakt wie
    vor der Einfuehrung.
    """
    if user is None:
        return frozenset()
    if getattr(user, "is_active", True) is False:
        return frozenset()

    if not schema_bereit():
        return permissions_for_legacy_role(getattr(user, "role", None))

    try:
        from ..models import RbacRolePermission, RbacUserRole

        rollen_ids = [z.role_id for z in RbacUserRole.query.filter_by(user_id=user.id)]
        if not rollen_ids:
            return permissions_for_legacy_role(getattr(user, "role", None))
        rechte: Set[str] = {
            z.permission for z in RbacRolePermission.query.filter(RbacRolePermission.role_id.in_(rollen_ids))
        }
        # Nur Rechte, die der Code auch kennt (ADR-004).
        return frozenset(rechte & ALL_PERMISSIONS)
    except Exception:  # pragma: no cover
        _logger.exception("Rechteaufloesung fehlgeschlagen, Legacy-Fallback")
        return permissions_for_legacy_role(getattr(user, "role", None))


def sitzung_setzen(user: Any) -> FrozenSet[str]:
    """Rechte in die Session schreiben; beim Login und bei Epoch-Wechsel."""
    rechte = resolve_permissions(user)
    session[SESSION_PERMS] = sorted(rechte)
    session[SESSION_EPOCH] = aktueller_epoch(frisch=True)
    session[SESSION_UID] = getattr(user, "id", None)
    return rechte


def _aktueller_nutzer() -> Tuple[Optional[Any], bool]:
    """(Nutzer, Stoerung).

    Ein geloeschtes Konto und eine nicht erreichbare Datenbank sehen gleich aus,
    bedeuten aber das Gegenteil: das eine ist ein Rechteentzug, das andere ein
    Ausfall. Nur beim Ausfall darf die Legacy-Rolle noch tragen.
    """
    from ..models import User

    user_id = session.get("user_id")
    if not user_id or user_id == "basic_auth":
        return None, False
    try:
        return User.query.get(user_id), False
    except Exception:  # pragma: no cover
        return None, True


def current_permissions() -> FrozenSet[str]:
    """Rechte der laufenden Session, bei Bedarf neu aufgeloest."""
    if "user_id" not in session:
        return frozenset()

    stand = session.get(SESSION_EPOCH)
    if (
        stand is not None
        and stand == aktueller_epoch()
        and SESSION_PERMS in session
        and session.get(SESSION_UID) == session.get("user_id")
    ):
        return frozenset(session[SESSION_PERMS])

    nutzer, stoerung = _aktueller_nutzer()
    if nutzer is not None:
        return sitzung_setzen(nutzer)
    if stoerung:
        # Datenbank nicht erreichbar: nicht mehr gewaehren als die Legacy-Rolle.
        return permissions_for_legacy_role(session.get("role"))
    if session.get(SESSION_UID) and session.get(SESSION_UID) == session.get("user_id"):
        # Diese Sitzung entstand aus einer echten Anmeldung an genau diesem
        # Konto -- und das Konto gibt es nicht mehr. Ohne den Schnitt behielte
        # sie die Rechte ihrer Legacy-Rolle, bei einem Administrator also alle.
        session.clear()
        return frozenset()
    # Sitzung ohne aufgeloesten DB-Bezug (Alt-Sitzung): Legacy-Rolle, nie mehr.
    return permissions_for_legacy_role(session.get("role"))


def has_permission(permission: str) -> bool:
    if current_app.config.get("RBAC_ENFORCE", "on") == "off":
        return True
    return permission in current_permissions()
