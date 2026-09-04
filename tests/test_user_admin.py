"""Nutzerverwaltung: Loeschen, Passwort, MFA und die Entra-Grenzen.

Die Regeln, die hier stehen, sind die aus der Oberflaeche sichtbaren:
Entra-Konten haben kein lokales Passwort, gebundene UPNs sind eingefroren,
und der letzte Verwalter kann sich nicht selbst aussperren.
"""

from unittest.mock import patch

from werkzeug.security import check_password_hash

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import RbacRole, RbacUserRole, User
from hookwise.rbac.schema_bridge import seed_builtin_roles


def _app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    # Fest verdrahtet, damit die Erwartungen nicht vom Umgebungs-Default abhaengen.
    app.config["RBAC_ENFORCE"] = "on"
    return app


def _nutzer(username, rolle_key, **extra):
    nutzer = User(
        username=username,
        password_hash="x",
        role=rolle_key,
        auth_source=extra.pop("auth_source", "local"),
        is_active=True,
        **extra,
    )
    db.session.add(nutzer)
    db.session.flush()
    rolle = RbacRole.query.filter_by(key=rolle_key).first()
    if rolle is not None:
        db.session.add(RbacUserRole(user_id=nutzer.id, role_id=rolle.id))
    db.session.commit()
    return nutzer


def _vorbereiten(app):
    db.create_all()
    app.config["RBAC_SCHEMA_OK"] = True
    seed_builtin_roles()
    return _nutzer("chef", "admin")


def _anmelden(client, nutzer):
    with client.session_transaction() as sess:
        sess["user_id"] = nutzer.id
        sess["username"] = nutzer.username
        sess["role"] = nutzer.role


# ------------------------------------------------------------------ Entra --
def test_entra_konto_ohne_konfiguration_wird_abgelehnt():
    """Ein Konto, das sich nie anmelden koennte, entsteht gar nicht erst."""
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        client = app.test_client()
        _anmelden(client, chef)
        antwort = client.post(
            "/api/users",
            json={"username": "neu@firma.test", "auth_source": "entra", "upn": "neu@firma.test"},
        )
        assert antwort.status_code == 409
        assert "not configured" in antwort.json["message"]
        assert User.query.filter_by(upn="neu@firma.test").first() is None
        db.session.remove()
        db.drop_all()


def test_entra_konto_mit_konfiguration_moeglich():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        client = app.test_client()
        _anmelden(client, chef)
        with patch("hookwise.user_api.entra_aktiv", return_value=True):
            antwort = client.post(
                "/api/users",
                json={"username": "neu@firma.test", "auth_source": "entra", "upn": "neu@firma.test"},
            )
        assert antwort.status_code == 201
        db.session.remove()
        db.drop_all()


def test_gebundener_upn_ist_eingefroren():
    """Nach der Bindung pflegt Microsoft den UPN -- Handaenderung abgelehnt."""
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        gebunden = _nutzer(
            "extern", "viewer", auth_source="entra", upn="alt@firma.test", entra_tid="t1", entra_oid="o1"
        )
        client = app.test_client()
        _anmelden(client, chef)
        antwort = client.patch(f"/api/users/{gebunden.id}", json={"upn": "neu@firma.test"})
        assert antwort.status_code == 409
        assert User.query.get(gebunden.id).upn == "alt@firma.test"

        # Bindung loesen gibt den UPN wieder frei.
        assert client.delete(f"/api/users/{gebunden.id}/entra-binding").status_code == 200
        antwort = client.patch(f"/api/users/{gebunden.id}", json={"upn": "neu@firma.test"})
        assert antwort.status_code == 200
        assert User.query.get(gebunden.id).upn == "neu@firma.test"
        db.session.remove()
        db.drop_all()


# --------------------------------------------------------------- Passwort --
def test_passwort_setzen_fuer_lokales_konto():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        ziel = _nutzer("kollege", "viewer")
        client = app.test_client()
        _anmelden(client, chef)
        assert client.post(f"/api/users/{ziel.id}/password", json={"password": "kurz"}).status_code == 400
        antwort = client.post(f"/api/users/{ziel.id}/password", json={"password": "Neu-und-lang-1"})
        assert antwort.status_code == 200
        assert check_password_hash(User.query.get(ziel.id).password_hash, "Neu-und-lang-1")
        db.session.remove()
        db.drop_all()


def test_passwort_fuer_entra_konto_abgelehnt():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        ziel = _nutzer("extern", "viewer", auth_source="entra", upn="e@firma.test")
        client = app.test_client()
        _anmelden(client, chef)
        antwort = client.post(f"/api/users/{ziel.id}/password", json={"password": "Neu-und-lang-1"})
        assert antwort.status_code == 409
        db.session.remove()
        db.drop_all()


# -------------------------------------------------------------------- MFA --
def test_mfa_reset_loescht_geheimnis_und_schalter():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        ziel = _nutzer("kollege", "viewer", otp_secret="ABCDEFGH", is_2fa_enabled=True)
        client = app.test_client()
        _anmelden(client, chef)
        assert client.delete(f"/api/users/{ziel.id}/mfa").status_code == 200
        frisch = User.query.get(ziel.id)
        assert frisch.otp_secret is None and frisch.is_2fa_enabled is False
        db.session.remove()
        db.drop_all()


def test_mfa_reset_ohne_mfa_ist_ein_fehler():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        ziel = _nutzer("kollege", "viewer")
        client = app.test_client()
        _anmelden(client, chef)
        assert client.delete(f"/api/users/{ziel.id}/mfa").status_code == 409
        db.session.remove()
        db.drop_all()


# ---------------------------------------------------------------- Loeschen --
def test_loeschen_entfernt_konto_und_zuweisungen():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        ziel = _nutzer("kollege", "operator")
        client = app.test_client()
        _anmelden(client, chef)
        assert client.delete(f"/api/users/{ziel.id}").status_code == 200
        assert User.query.get(ziel.id) is None
        assert RbacUserRole.query.filter_by(user_id=ziel.id).count() == 0
        db.session.remove()
        db.drop_all()


def test_eigenes_konto_nicht_loeschbar():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        client = app.test_client()
        _anmelden(client, chef)
        assert client.delete(f"/api/users/{chef.id}").status_code == 409
        assert User.query.get(chef.id) is not None
        db.session.remove()
        db.drop_all()


def test_letzter_verwalter_nicht_loeschbar():
    """Zwei Schichten: ohne ``user:manage`` blockt schon der Guard (403);
    die Aussperr-Invariante selbst greift beim Selbst-Loeschen (409, oben).
    Sobald ein zweiter Verwalter existiert, ist das Loeschen erlaubt."""
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        beobachter = _nutzer("beobachter", "viewer")
        client = app.test_client()
        _anmelden(client, beobachter)
        assert client.delete(f"/api/users/{chef.id}").status_code == 403
        assert User.query.get(chef.id) is not None

        # Mit einem zweiten Verwalter geht es dann.
        zweiter = _nutzer("vize", "admin")
        _anmelden(client, zweiter)
        assert client.delete(f"/api/users/{chef.id}").status_code == 200
        db.session.remove()
        db.drop_all()


# ------------------------------------------------------------- Umbenennen --
def test_umbenennen_prueft_eindeutigkeit():
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        ziel = _nutzer("kollege", "viewer")
        client = app.test_client()
        _anmelden(client, chef)
        assert client.patch(f"/api/users/{ziel.id}", json={"username": "chef"}).status_code == 409
        assert client.patch(f"/api/users/{ziel.id}", json={"username": " "}).status_code == 400
        assert client.patch(f"/api/users/{ziel.id}", json={"username": "kollegin"}).status_code == 200
        assert User.query.get(ziel.id).username == "kollegin"
        db.session.remove()
        db.drop_all()


# ------------------------------------------------------- Anmeldung gesperrt --
def test_deaktiviertes_konto_kann_sich_nicht_anmelden():
    """Sperren heisst sperren: kein Passwort-Login, kein 2FA-Schritt."""
    from werkzeug.security import generate_password_hash

    app = _app()
    with app.app_context():
        _vorbereiten(app)
        gesperrt = _nutzer("gesperrt", "viewer")
        gesperrt.password_hash = generate_password_hash("Ein-gutes-Passwort-1")
        gesperrt.is_active = False
        db.session.commit()

        client = app.test_client()
        antwort = client.post(
            "/login",
            data={"username": "gesperrt", "password": "Ein-gutes-Passwort-1"},
            follow_redirects=False,
        )
        assert antwort.status_code == 200  # bleibt auf der Anmeldeseite
        with client.session_transaction() as sess:
            assert "user_id" not in sess
            assert "pending_user_id" not in sess
        db.session.remove()
        db.drop_all()


def test_geloeschtes_konto_verliert_laufende_sitzung():
    """Nach dem Loeschen darf die offene Sitzung nicht weiter Adminrechte
    aus der Legacy-Rolle ziehen.

    Nachgestellt wird eine echte Anmeldung (``sitzung_setzen`` haelt fest, zu
    welchem Konto die Sitzung gehoert) -- genau daran erkennt die Aufloesung
    spaeter, dass das Konto fort ist.
    """
    from flask import session as sitzung

    from hookwise.rbac.resolver import bump_epoch, current_permissions, sitzung_setzen

    app = _app()
    with app.app_context():
        _vorbereiten(app)
        opfer = _nutzer("faellt-weg", "admin")
        opfer_id = opfer.id

        with app.test_request_context():
            sitzung["user_id"] = opfer_id
            sitzung["username"] = "faellt-weg"
            sitzung["role"] = "admin"
            assert "user:manage" in sitzung_setzen(opfer)

            gemerkt = dict(sitzung)

        RbacUserRole.query.filter_by(user_id=opfer_id).delete(synchronize_session=False)
        db.session.delete(User.query.get(opfer_id))
        db.session.commit()
        bump_epoch()

        with app.test_request_context():
            sitzung.update(gemerkt)
            assert current_permissions() == frozenset()
            assert "user_id" not in sitzung  # die Sitzung ist gekappt
        db.session.remove()
        db.drop_all()


def test_health_services_verlangt_anmeldung():
    """Die Route deklariert settings:read -- anonym darf sie nichts liefern."""
    app = _app()
    with app.app_context():
        chef = _vorbereiten(app)
        anonym = app.test_client()
        antwort = anonym.get("/health/services")
        assert antwort.status_code in (302, 401, 403), antwort.status_code

        angemeldet = app.test_client()
        _anmelden(angemeldet, chef)
        assert angemeldet.get("/health/services").status_code in (200, 503)

        # /health und /readyz bleiben offen -- daran haengen die Healthchecks.
        assert anonym.get("/health").status_code in (200, 503)
        assert anonym.get("/readyz").status_code in (200, 503)
        db.session.remove()
        db.drop_all()
