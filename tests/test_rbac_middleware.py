"""Middleware: Blockiert der Guard wirklich -- und nur das Richtige?

Der Matrix-Test ist das Rueckgrat: Er faellt, sobald eine Route faelschlich
offen oder faelschlich gesperrt ist.
"""

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import RbacRole, RbacUserRole, User
from hookwise.rbac.catalog import ROLE_PRESETS
from hookwise.rbac.routes import ENDPUNKT_RECHTE
from hookwise.rbac.schema_bridge import seed_builtin_roles


def _app(modus):
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["RBAC_ENFORCE"] = modus
    return app


@pytest.fixture
def app_an():
    return _app("on")


@pytest.fixture
def app_log():
    return _app("log")


def _vorbereiten(app):
    """Schema, Rollen und je ein Nutzer pro Rolle."""
    db.create_all()
    app.config["RBAC_SCHEMA_OK"] = True
    seed_builtin_roles()
    for schluessel in ROLE_PRESETS:
        nutzer = User(
            username=schluessel,
            password_hash="x",
            role=schluessel,
            auth_source="local",
            is_active=True,
        )
        db.session.add(nutzer)
        db.session.flush()
        rolle = RbacRole.query.filter_by(key=schluessel).first()
        db.session.add(RbacUserRole(user_id=nutzer.id, role_id=rolle.id))
    db.session.commit()


def _anmelden(client, rolle):
    nutzer = User.query.filter_by(username=rolle).first()
    with client.session_transaction() as sess:
        sess["user_id"] = nutzer.id
        sess["username"] = nutzer.username
        sess["role"] = nutzer.role
    return nutzer


@pytest.mark.parametrize("rolle", sorted(ROLE_PRESETS))
def test_rechte_matrix(app_an, rolle):
    """Jede GET-Route antwortet fuer jede Rolle so, wie der Katalog es vorgibt."""
    with app_an.app_context():
        _vorbereiten(app_an)
        client = app_an.test_client()
        _anmelden(client, rolle)
        erlaubt = set(ROLE_PRESETS[rolle]["permissions"])

        geprueft = 0
        for regel in app_an.url_map.iter_rules():
            noetig = ENDPUNKT_RECHTE.get(regel.endpoint)
            if not noetig or "GET" not in (regel.methods or set()):
                continue
            if regel.arguments:  # Routen mit Parametern brauchen echte IDs
                continue
            antwort = client.get(str(regel.rule))
            geprueft += 1
            if noetig in erlaubt:
                assert antwort.status_code != 403, f"{regel.endpoint} faelschlich verweigert ({noetig})"
            else:
                assert antwort.status_code == 403, f"{regel.endpoint} faelschlich erlaubt ({noetig})"
        assert geprueft > 10, f"zu wenige Routen geprueft: {geprueft}"

        db.session.remove()
        db.drop_all()


def test_log_modus_blockiert_nicht(app_log):
    """Die Rollout-Stufe protokolliert nur -- sonst waere sie wertlos."""
    with app_log.app_context():
        _vorbereiten(app_log)
        client = app_log.test_client()
        _anmelden(client, "viewer")
        # viewer hat kein audit:read
        assert client.get("/audit").status_code != 403
        db.session.remove()
        db.drop_all()


def test_off_modus_prueft_gar_nicht():
    app = _app("off")
    with app.app_context():
        _vorbereiten(app)
        client = app.test_client()
        _anmelden(client, "viewer")
        assert client.get("/audit").status_code != 403
        db.session.remove()
        db.drop_all()


def test_verweigerung_liefert_json_fuer_api(app_an):
    with app_an.app_context():
        _vorbereiten(app_an)
        client = app_an.test_client()
        _anmelden(client, "viewer")
        antwort = client.get("/api/users")
        assert antwort.status_code == 403
        assert antwort.json["status"] == "error"
        assert antwort.json["required"] == "user:read"
        db.session.remove()
        db.drop_all()


def test_rechteentzug_wirkt_ohne_neuanmeldung(app_an):
    """Der Epoch sorgt dafuer, dass ein Entzug beim naechsten Request greift."""
    with app_an.app_context():
        _vorbereiten(app_an)
        client = app_an.test_client()
        nutzer = _anmelden(client, "admin")

        assert client.get("/api/users").status_code == 200

        from hookwise.rbac.resolver import bump_epoch

        RbacUserRole.query.filter_by(user_id=nutzer.id).delete(synchronize_session=False)
        viewer = RbacRole.query.filter_by(key="viewer").first()
        db.session.add(RbacUserRole(user_id=nutzer.id, role_id=viewer.id))
        db.session.commit()
        bump_epoch()

        assert client.get("/api/users").status_code == 403, "Entzug wirkte erst beim naechsten Login"
        db.session.remove()
        db.drop_all()


def test_oeffentliche_routen_bleiben_offen(app_an):
    with app_an.app_context():
        _vorbereiten(app_an)
        client = app_an.test_client()
        _anmelden(client, "viewer")
        for pfad in ("/health", "/readyz"):
            assert client.get(pfad).status_code != 403, pfad
        db.session.remove()
        db.drop_all()
