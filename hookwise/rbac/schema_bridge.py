"""Schema ohne Migrationsschritt (ADR-001).

Drei Schichten, die zusammen sicherstellen, dass die Anwendung vor, waehrend
und nach der Umstellung laeuft:

1. ``rbac_schema_state`` fragt ueber den SQLAlchemy-Inspector, was tatsaechlich
   da ist -- kein Raten, kein try/except um fehlende Spalten herum.
2. ``ensure_rbac_schema`` legt Fehlendes an, idempotent und unter einer
   Postgres-Beratungssperre. Bei euch starten proxy, worker, beat und migrate
   gleichzeitig; ohne Sperre waere das ein Rennen.
3. Fehlt das Schema trotzdem, faellt ``resolver`` auf die alte ``role``-Spalte
   zurueck.

Bewusst kein ``ADD COLUMN IF NOT EXISTS``: Postgres kennt es, SQLite nicht --
und die Tests laufen auf SQLite in-memory. Die Pruefung ueber den Inspector
plus schlichtes ``ADD COLUMN`` deckt in beiden Faellen denselben Codepfad ab.
"""

import logging
from typing import Any, Dict, FrozenSet, Set, cast

from sqlalchemy import inspect

_logger = logging.getLogger(__name__)

#: Frei gewaehlte, stabile Kennung fuer die Beratungssperre.
RBAC_LOCK_ID = 0x484F_4F4B  # "HOOK"


def _tabellennamen(engine: Any) -> Set[str]:
    return set(inspect(engine).get_table_names())


def rbac_schema_state(engine: Any) -> Dict[str, Any]:
    """Was ist tatsaechlich in der Datenbank vorhanden?"""
    from ..models import RBAC_TABLES, USER_BRIDGE_COLUMNS

    try:
        insp = inspect(engine)
        tabellen = set(insp.get_table_names())
        gewuenschte_tabellen = {t.name for t in RBAC_TABLES}
        fehlende_tabellen = gewuenschte_tabellen - tabellen

        user_spalten: Set[str] = set()
        if "user" in tabellen:
            user_spalten = {c["name"] for c in insp.get_columns("user")}
        fehlende_spalten = set(USER_BRIDGE_COLUMNS) - user_spalten if "user" in tabellen else set()

        return {
            "user_da": "user" in tabellen,
            "tabellen_da": not fehlende_tabellen,
            "fehlende_tabellen": fehlende_tabellen,
            "fehlende_spalten": fehlende_spalten,
            "vollstaendig": not fehlende_tabellen and not fehlende_spalten and "user" in tabellen,
        }
    except Exception:  # pragma: no cover - Datenbank nicht erreichbar
        _logger.exception("RBAC-Schemapruefung fehlgeschlagen")
        return {
            "user_da": False,
            "tabellen_da": False,
            "fehlende_tabellen": set(),
            "fehlende_spalten": set(),
            "vollstaendig": False,
        }


def ensure_rbac_schema(engine: Any) -> Dict[str, Any]:
    """Legt fehlende RBAC-Tabellen und User-Spalten an. Idempotent."""
    from ..models import RBAC_TABLES, USER_BRIDGE_COLUMNS

    zustand = rbac_schema_state(engine)
    if zustand["vollstaendig"] or not zustand["user_da"]:
        # Nichts zu tun -- oder die Basistabellen fehlen noch komplett, dann ist
        # create_all/Alembic zustaendig und wir laufen beim naechsten Start.
        return zustand

    try:
        with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                # Transaktionsweite Beratungssperre: Der zweite Container wartet
                # hier, findet danach ein vollstaendiges Schema und tut nichts.
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (RBAC_LOCK_ID,))

            # Nach dem Sperrerwerb erneut pruefen: Zwischen erster Pruefung und
            # Sperre kann ein anderer Prozess fertig geworden sein.
            zustand = rbac_schema_state(engine)

            for spalte in sorted(zustand["fehlende_spalten"]):
                typ = USER_BRIDGE_COLUMNS[spalte]
                conn.exec_driver_sql(f'ALTER TABLE "user" ADD COLUMN {spalte} {typ}')
                _logger.info("RBAC-Bridge: Spalte user.%s ergaenzt", spalte)

            fehlende_tabellen = zustand["fehlende_tabellen"]
            if fehlende_tabellen:
                for tabelle in RBAC_TABLES:
                    if tabelle.name in fehlende_tabellen:
                        tabelle.create(bind=conn, checkfirst=True)
                        _logger.info("RBAC-Bridge: Tabelle %s angelegt", tabelle.name)
    except Exception:
        # Ein misslungener Bootstrap darf die Anwendung nicht am Start hindern:
        # der Legacy-Fallback traegt sie weiter.
        _logger.exception("RBAC-Bridge: Schema-Bootstrap fehlgeschlagen, Legacy-Fallback aktiv")
        return rbac_schema_state(engine)

    return rbac_schema_state(engine)


def seed_builtin_roles() -> int:
    """Legt die eingebauten Rollen an und haelt ihre Rechte aktuell.

    Idempotent: bestehende Rollen werden nur nachgezogen, eigene Rollen bleiben
    unberuehrt. Gibt die Zahl der geaenderten Rollen zurueck.
    """
    from ..extensions import db
    from ..models import RbacRole, RbacRolePermission
    from .catalog import ROLE_PRESETS

    geaendert = 0
    for schluessel, vorgabe in ROLE_PRESETS.items():
        rolle = RbacRole.query.filter_by(key=schluessel).first()
        if rolle is None:
            rolle = RbacRole(
                key=schluessel,
                name=str(vorgabe["name"]),
                description=str(vorgabe["description"]),
                is_builtin=True,
            )
            db.session.add(rolle)
            db.session.flush()
            geaendert += 1
        elif (rolle.name, rolle.description) != (vorgabe["name"], vorgabe["description"]):
            rolle.name = str(vorgabe["name"])
            rolle.description = str(vorgabe["description"])
            geaendert += 1

        soll = set(cast("FrozenSet[str]", vorgabe["permissions"]))
        ist = {p.permission for p in RbacRolePermission.query.filter_by(role_id=rolle.id)}
        if ist != soll:
            RbacRolePermission.query.filter_by(role_id=rolle.id).delete(synchronize_session=False)
            for recht in sorted(soll):
                db.session.add(RbacRolePermission(role_id=rolle.id, permission=recht))
            geaendert += 1

    if geaendert:
        db.session.commit()
    return geaendert


def bootstrap(app: Any) -> None:
    """Beim Start: Schema sicherstellen, Rollen setzen, Zustand merken."""
    from ..extensions import db

    if not app.config.get("RBAC_SCHEMA_BOOTSTRAP", True):
        with app.app_context():
            zustand = rbac_schema_state(db.engine)
        app.config["RBAC_SCHEMA_OK"] = bool(zustand["vollstaendig"])
        return

    with app.app_context():
        zustand = ensure_rbac_schema(db.engine)
        app.config["RBAC_SCHEMA_OK"] = bool(zustand["vollstaendig"])
        if zustand["vollstaendig"]:
            try:
                seed_builtin_roles()
            except Exception:  # pragma: no cover
                db.session.rollback()
                _logger.exception("RBAC-Bridge: Rollen-Seed fehlgeschlagen")
        else:
            _logger.warning(
                "RBAC-Schema unvollstaendig (%s) - Legacy-Fallback ueber User.role aktiv",
                zustand,
            )
