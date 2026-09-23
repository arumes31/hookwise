"""Rechteauflösung.

Rechte in der Session zu cachen ist schnell, aber ein Rollenentzug wuerde erst
beim naechsten Login greifen. Deshalb der Permissions-Epoch: ein Zaehler, den
jede Rechteaenderung erhoeht. Stimmt der Stand in der Session nicht mehr,
loest der naechste Request die Rechte neu auf -- ein DB-Roundtrip statt einer
Wartezeit bis zum naechsten Login.
"""

import logging
import time
from typing import Any, FrozenSet, Optional, Set, Tuple, cast

from flask import current_app, request, session
from sqlalchemy.exc import IntegrityError

from .catalog import ALL_PERMISSIONS, permissions_for_legacy_role

_logger = logging.getLogger(__name__)

SESSION_PERMS = "perms"
SESSION_EPOCH = "perms_epoch"
# Der Cache gilt nur fuer den Nutzer, fuer den er geschrieben wurde -- wechselt
# die user_id in einer bestehenden Session, wird neu aufgeloest.
SESSION_UID = "perms_uid"
ENTRA_NO_PERMISSIONS_ROLE = "none"

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


def aktueller_epoch(frisch: bool = False) -> Optional[int]:
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
        _logger.exception("Permissions-Epoch konnte nicht autoritativ gelesen werden")
        return None
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


def _authoritative_role(user: Any) -> Tuple[Optional[str], bool, str]:
    """Explizite Ein-Rollen-Quelle fuer Overrides und Entra-Konten.

    Der Boolean unterscheidet "keine autoritative Quelle" von einer kaputten
    autoritativen Quelle. Ein aktiver Override ohne Rolle darf deshalb niemals
    auf alte Zuweisungen oder die Viewer-Rolle zurueckfallen.
    """
    if bool(getattr(user, "is_override_active", False)):
        rolle = str(getattr(user, "override_role", "") or "").strip().lower()
        return (rolle or None), True, "manual_override"
    quelle = str(getattr(user, "auth_source", "local") or "local").strip().lower()
    entra_rolle = str(getattr(user, "entra_role", "") or "").strip().lower()
    if quelle == "entra" and entra_rolle:
        return entra_rolle, True, "entra_app_role"
    return None, False, "local"


def effective_role_key(user: Any) -> Optional[str]:
    """Effektiver Rollen-Key fuer Anzeige, Sitzung und Audit."""
    rolle, autoritativ, _quelle = _authoritative_role(user)
    if autoritativ:
        return rolle
    fallback = str(getattr(user, "role", "") or "").strip().lower()
    return fallback or None


def resolve_permissions(user: Any) -> Optional[FrozenSet[str]]:
    """Effektive Rechte eines Nutzers.

    Reihenfolge: manueller Override, Entra App Role, zugewiesene lokale Rollen,
    alter ``role``-String. Override und App Role sind exakte Ersatzrollen; ihre
    Rechte werden nicht mit lokalen Zuweisungen vereinigt. ``None`` signalisiert
    eine fehlgeschlagene autoritative Abfrage; ein erfolgreicher leerer Grant
    bleibt dagegen ein leeres ``frozenset``.
    """
    if user is None:
        return frozenset()
    if getattr(user, "is_active", True) is False:
        return frozenset()

    autoritative_rolle, autoritativ, quelle = _authoritative_role(user)
    if autoritativ and (
        not autoritative_rolle or (quelle == "entra_app_role" and autoritative_rolle == ENTRA_NO_PERMISSIONS_ROLE)
    ):
        return frozenset()

    if not schema_bereit():
        return permissions_for_legacy_role(autoritative_rolle if autoritativ else getattr(user, "role", None))

    try:
        from ..models import RbacRole, RbacRolePermission, RbacUserRole

        if autoritativ:
            rolle = RbacRole.query.filter_by(key=autoritative_rolle).first()
            if rolle is None:
                _logger.error("Autoritative Rolle %s existiert nicht", autoritative_rolle)
                return frozenset()
            autoritative_rechte = {z.permission for z in RbacRolePermission.query.filter_by(role_id=rolle.id)}
            return frozenset(autoritative_rechte & ALL_PERMISSIONS)

        rollen_ids = [z.role_id for z in RbacUserRole.query.filter_by(user_id=user.id)]
        if not rollen_ids:
            return permissions_for_legacy_role(getattr(user, "role", None))
        rechte: Set[str] = {
            z.permission for z in RbacRolePermission.query.filter(RbacRolePermission.role_id.in_(rollen_ids))
        }
        # Nur Rechte, die der Code auch kennt (ADR-004).
        return frozenset(rechte & ALL_PERMISSIONS)
    except Exception:  # pragma: no cover
        _logger.exception("Rechteaufloesung fehlgeschlagen; es werden keine Rechte gewaehrt")
        return None


def sitzung_setzen(user: Any, *, epoch: Optional[int] = None) -> FrozenSet[str]:
    """Rechte in die Session schreiben; beim Login und bei Epoch-Wechsel."""
    stand = aktueller_epoch(frisch=True) if epoch is None else epoch
    if stand is None:
        session.pop(SESSION_PERMS, None)
        session.pop(SESSION_EPOCH, None)
        session.pop(SESSION_UID, None)
        return frozenset()

    rechte = resolve_permissions(user)
    if rechte is None:
        session.pop(SESSION_PERMS, None)
        session.pop(SESSION_EPOCH, None)
        session.pop(SESSION_UID, None)
        return frozenset()

    rolle, autoritativ, quelle = _authoritative_role(user)
    if autoritativ:
        # ``None`` ist ein kaputter Override und bleibt in den Permissions leer;
        # ein Legacy-Fallback darf daraus keine Viewer-Sitzung machen.
        session["role"] = rolle or ""
        session["authz_source"] = quelle
    else:
        session["role"] = getattr(user, "role", None)
        session["authz_source"] = "local"
    session[SESSION_PERMS] = sorted(rechte)
    session[SESSION_EPOCH] = stand
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


def _request_epoch() -> Optional[int]:
    """Read the authoritative epoch once per request, never across requests."""
    cache_key = "hookwise.permissions_epoch"
    if cache_key not in request.environ:
        request.environ[cache_key] = aktueller_epoch(frisch=True)
    return cast(Optional[int], request.environ[cache_key])


def current_permissions() -> FrozenSet[str]:
    """Rechte der laufenden Session, bei Bedarf neu aufgeloest."""
    if "user_id" not in session:
        return frozenset()

    # Fuer Autorisierung ist ein Prozesscache keine autoritative Quelle: bei
    # Rollenentzug in einer anderen Instanz oder Datenbankausfall koennte eine
    # alte Session sonst weiter privilegiert bleiben.
    epoch = _request_epoch()
    if epoch is None:
        _logger.error("Permissions-Epoch nicht verfuegbar; es werden keine Rechte gewaehrt")
        return frozenset()

    stand = session.get(SESSION_EPOCH)
    if (
        stand is not None
        and stand == epoch
        and SESSION_PERMS in session
        and session.get(SESSION_UID) == session.get("user_id")
    ):
        return frozenset(session[SESSION_PERMS])

    nutzer, stoerung = _aktueller_nutzer()
    if nutzer is not None:
        return sitzung_setzen(nutzer, epoch=epoch)
    if stoerung:
        # Datenbank nicht erreichbar. Die Legacy-Rolle in der Sitzung stammt aus
        # der Anmeldung und altert nicht mit: ein zwischenzeitlich degradiertes
        # Konto haette waehrend der Stoerung seine alten Rechte behalten. Ein
        # Ausfall ist kein Grund, Rechte zu vergeben -- ohne Datenbank kann
        # ohnehin kaum eine Route arbeiten.
        _logger.error("Datenbank waehrend der Rechtepruefung nicht erreichbar; es werden keine Rechte gewaehrt")
        return frozenset()
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
