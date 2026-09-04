"""Rollen- und Rechtesystem: Katalog, Aufloesung, Middleware, Invarianten."""

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import RbacRole, RbacRolePermission, RbacUserRole, User
from hookwise.rbac.catalog import (
    ALL_PERMISSIONS,
    PERMISSION_GROUPS,
    ROLE_PRESETS,
    is_assignable_start_role,
    permissions_for_legacy_role,
)
from hookwise.rbac.decorators import verify_route_coverage
from hookwise.rbac.resolver import bump_epoch, resolve_permissions
from hookwise.rbac.routes import ENDPUNKT_RECHTE
from hookwise.rbac.schema_bridge import rbac_schema_state, seed_builtin_roles


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@pytest.fixture
def db_bereit(app):
    """Leeres Schema je Test -- create_all legt auch die RBAC-Tabellen an."""
    with app.app_context():
        db.create_all()
        app.config["RBAC_SCHEMA_OK"] = True
        yield
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------- Katalog ---
def test_katalog_ist_widerspruchsfrei():
    """Jede Gruppe zeigt nur Rechte, die es gibt -- und umgekehrt."""
    aus_gruppen = {k for _n, rechte in PERMISSION_GROUPS for k, _t in rechte}
    assert aus_gruppen == set(ALL_PERMISSIONS)
    assert len(aus_gruppen) == sum(len(r) for _n, r in PERMISSION_GROUPS), "doppelter Schluessel"


def test_eingebaute_rollen_nutzen_nur_bekannte_rechte():
    for schluessel, vorgabe in ROLE_PRESETS.items():
        unbekannt = set(vorgabe["permissions"]) - ALL_PERMISSIONS
        assert not unbekannt, f"{schluessel} nennt unbekannte Rechte: {unbekannt}"


def test_rollen_sind_aufsteigend():
    """viewer ist eine echte Teilmenge von operator, operator von admin."""
    viewer = set(ROLE_PRESETS["viewer"]["permissions"])
    operator = set(ROLE_PRESETS["operator"]["permissions"])
    admin = set(ROLE_PRESETS["admin"]["permissions"])
    assert viewer < operator < admin


def test_verwaltungsrechte_bleiben_beim_admin():
    """Operator betreibt inkl. Zustell-Credentials, verwaltet aber nichts."""
    for heikel in ("user:manage", "settings:write", "history:delete"):
        assert heikel not in ROLE_PRESETS["operator"]["permissions"], heikel
        assert heikel not in ROLE_PRESETS["viewer"]["permissions"], heikel
        assert heikel in ROLE_PRESETS["admin"]["permissions"], heikel
    for secret in ("secret:reveal", "secret:rotate"):
        assert secret in ROLE_PRESETS["operator"]["permissions"], secret
        assert secret not in ROLE_PRESETS["viewer"]["permissions"], secret


def test_startrolle_lehnt_privilegierte_rechte_ab():
    """Auto-Provisioning darf per Konstruktion keine Administratoren erzeugen.

    Seit operator Secrets haelt, faellt auch er als Startrolle aus."""
    assert is_assignable_start_role(frozenset(ROLE_PRESETS["viewer"]["permissions"]))
    assert not is_assignable_start_role(frozenset(ROLE_PRESETS["operator"]["permissions"]))
    assert not is_assignable_start_role(frozenset(ROLE_PRESETS["admin"]["permissions"]))


def test_legacy_abbildung():
    assert permissions_for_legacy_role("admin") == ALL_PERMISSIONS
    assert "endpoint:write" in permissions_for_legacy_role("user")
    assert "user:manage" not in permissions_for_legacy_role("user")
    # Unbekannt und leer fallen auf die schwaechste Stufe zurueck.
    assert permissions_for_legacy_role(None) == permissions_for_legacy_role("quatsch")


# ---------------------------------------------------------------- Routen ----
def test_jede_route_hat_ein_recht(app):
    """Der Startup-Check ist die Absicherung gegen die vergessene Route."""
    assert verify_route_coverage(app) == []


def test_zuordnung_nennt_nur_bekannte_rechte():
    unbekannt = {r for r in ENDPUNKT_RECHTE.values() if r is not None} - ALL_PERMISSIONS
    assert not unbekannt, f"Zuordnung nennt unbekannte Rechte: {unbekannt}"


def test_strenger_modus_meldet_luecke(app):
    """Eine Route ohne Deklaration muss den Start verhindern koennen."""

    @app.route("/nur-fuer-den-test-ohne-recht")
    def _luecke():  # pragma: no cover - wird nie aufgerufen
        return "x"

    with pytest.raises(RuntimeError, match="ohne Rechtedeklaration"):
        verify_route_coverage(app, streng=True)


# ------------------------------------------------------------- Aufloesung ---
def _nutzer(name="tester", rolle="user", aktiv=True):
    u = User(username=name, password_hash="x", role=rolle, auth_source="local", is_active=aktiv)
    db.session.add(u)
    db.session.commit()
    return u


def test_aufloesung_ohne_zuweisung_nutzt_legacy_rolle(app, db_bereit):
    with app.app_context():
        seed_builtin_roles()
        u = _nutzer("legacy-admin", "admin")
        assert resolve_permissions(u) == ALL_PERMISSIONS


def test_aufloesung_vereinigt_mehrere_rollen(app, db_bereit):
    with app.app_context():
        seed_builtin_roles()
        u = _nutzer("mehrfach", "viewer")
        viewer = RbacRole.query.filter_by(key="viewer").first()
        operator = RbacRole.query.filter_by(key="operator").first()
        db.session.add(RbacUserRole(user_id=u.id, role_id=viewer.id))
        db.session.add(RbacUserRole(user_id=u.id, role_id=operator.id))
        db.session.commit()
        rechte = resolve_permissions(u)
        assert "endpoint:write" in rechte  # aus operator
        assert "dashboard:read" in rechte  # aus viewer
        assert "user:manage" not in rechte


def test_deaktivierter_nutzer_hat_keine_rechte(app, db_bereit):
    with app.app_context():
        seed_builtin_roles()
        u = _nutzer("gesperrt", "admin", aktiv=False)
        assert resolve_permissions(u) == frozenset()


def test_unbekannte_rechte_aus_der_datenbank_werden_verworfen(app, db_bereit):
    """ADR-004: Nur was der Code kennt, zaehlt."""
    with app.app_context():
        seed_builtin_roles()
        u = _nutzer("geisterrecht", "viewer")
        rolle = RbacRole(key="geist", name="Geist", is_builtin=False)
        db.session.add(rolle)
        db.session.flush()
        db.session.add(RbacRolePermission(role_id=rolle.id, permission="gibt:es:nicht"))
        db.session.add(RbacRolePermission(role_id=rolle.id, permission="dashboard:read"))
        db.session.add(RbacUserRole(user_id=u.id, role_id=rolle.id))
        db.session.commit()
        assert resolve_permissions(u) == frozenset({"dashboard:read"})


def test_epoch_steigt_bei_aenderung(app, db_bereit):
    with app.app_context():
        seed_builtin_roles()
        vorher = bump_epoch()
        nachher = bump_epoch()
        assert nachher == vorher + 1


# ------------------------------------------------------------ Schema-Bridge -
def test_schema_ist_nach_create_all_vollstaendig(app, db_bereit):
    with app.app_context():
        zustand = rbac_schema_state(db.engine)
        assert zustand["vollstaendig"], zustand
        assert not zustand["fehlende_spalten"]


def test_seed_ist_idempotent(app, db_bereit):
    with app.app_context():
        seed_builtin_roles()
        vorher = RbacRole.query.count()
        assert seed_builtin_roles() == 0, "zweiter Lauf darf nichts aendern"
        assert RbacRole.query.count() == vorher


def test_seed_setzt_rechte_der_eingebauten_rollen(app, db_bereit):
    with app.app_context():
        seed_builtin_roles()
        admin = RbacRole.query.filter_by(key="admin").first()
        rechte = {p.permission for p in RbacRolePermission.query.filter_by(role_id=admin.id)}
        assert rechte == set(ALL_PERMISSIONS)
        assert admin.is_builtin is True
