"""Entra-ID-Anmeldung gegen einen gemockten Provider.

Der Schwerpunkt liegt auf den Negativpfaden: fremder Tenant, unbekannte UPN,
deaktiviertes Konto, wiederverwendeter state. Dort entscheidet sich, ob die
Anbindung traegt -- der Erfolgsfall ist der einfache Teil.
"""

from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.extensions import db
from hookwise.models import AuditLog, RbacRole, RbacUserRole, User
from hookwise.rbac.schema_bridge import seed_builtin_roles

TENANT = "11111111-2222-3333-4444-555555555555"
FREMD = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def app(monkeypatch, tmp_path):
    geheim = tmp_path / "entra-secret"
    geheim.write_text("test-client-secret", encoding="utf-8")
    monkeypatch.setenv("ENTRA_ENABLED", "true")
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_CLIENT_ID", "client-id")
    monkeypatch.setenv("ENTRA_CLIENT_SECRET_FILE", str(geheim))
    monkeypatch.setenv("ENTRA_REDIRECT_URL", "https://example.test/auth/entra/callback")
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        app.config["RBAC_SCHEMA_OK"] = True
        seed_builtin_roles()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _entra_nutzer(upn="kollege@example.test", aktiv=True, oid=None):
    u = User(
        username=upn,
        password_hash="x",
        role="user",
        auth_source="entra",
        upn=upn,
        is_active=aktiv,
        entra_oid=oid,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _flow_setzen(client):
    with client.session_transaction() as sess:
        sess["entra_flow"] = {"state": "s1", "nonce": "n1"}


def _antwort(tid=TENANT, oid="oid-1", upn="kollege@example.test", fehler=None, gruppen=None):
    if fehler:
        return {"error": fehler, "error_description": fehler}
    anspruch = {"tid": tid, "oid": oid, "preferred_username": upn}
    if gruppen is not None:
        anspruch["groups"] = gruppen
    return {"id_token_claims": anspruch}


class _mit_token:
    """Ersetzt Token-Tausch und Bibliotheks-Pruefung; alles dazwischen laeuft echt.

    ``entra_aktiv`` wird ueberbrueckt, damit der Flow unabhaengig von der
    Umgebung laeuft; dass die Konfigurationspruefung selbst greift, pruefen
    ``test_ohne_bibliothek_bleibt_entra_inaktiv`` und
    ``test_ohne_lesbares_secret_bleibt_entra_inaktiv``.
    """

    def __init__(self, antwort):
        self._patches = [
            patch("hookwise.auth_entra.entra_aktiv", return_value=True),
            patch(
                "hookwise.auth_entra._client",
                **{"return_value.acquire_token_by_auth_code_flow.return_value": antwort},
            ),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.stop()
        return False


# ------------------------------------------------------------- Erfolgsfall --
def test_erfolgreiche_anmeldung_bindet_an_oid(app, client):
    with app.app_context():
        nutzer = _entra_nutzer()
        nutzer_id = nutzer.id
    _flow_setzen(client)

    with _mit_token(_antwort()):
        antwort = client.get("/auth/entra/callback?code=c&state=s1", follow_redirects=False)

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("user_id") == nutzer_id
        assert sess.get("auth_source") == "entra"
    with app.app_context():
        frisch = User.query.get(nutzer_id)
        assert frisch.entra_oid == "oid-1"
        assert frisch.entra_tid == TENANT
        assert frisch.last_login_at is not None


def test_bindung_ueberlebt_namensaenderung(app, client):
    """Nach der Bindung zaehlt die oid, nicht mehr die UPN."""
    with app.app_context():
        nutzer = _entra_nutzer(upn="alt@example.test", oid="oid-1")
        nutzer_id = nutzer.id
    _flow_setzen(client)

    with _mit_token(_antwort(upn="ganz.neu@example.test")):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("user_id") == nutzer_id


# ------------------------------------------------------------ Negativpfade --
def test_fremder_tenant_wird_abgewiesen(app, client):
    with app.app_context():
        _entra_nutzer()
    _flow_setzen(client)

    with _mit_token(_antwort(tid=FREMD)):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess
    with app.app_context():
        eintraege = AuditLog.query.filter_by(action="entra_login_denied").all()
        assert len(eintraege) == 1
        assert "foreign tenant" in eintraege[0].details


def test_unbekannte_upn_wird_abgewiesen(app, client):
    """Vorabanlage ist Pflicht, solange Auto-Provisioning aus ist."""
    _flow_setzen(client)

    with _mit_token(_antwort(upn="niemand@example.test")):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess
    with app.app_context():
        assert AuditLog.query.filter_by(action="entra_login_denied").count() == 1
        assert User.query.filter_by(upn="niemand@example.test").first() is None


def test_deaktiviertes_konto_wird_abgewiesen(app, client):
    with app.app_context():
        _entra_nutzer(aktiv=False)
    _flow_setzen(client)

    with _mit_token(_antwort()):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_state_ist_einmalig(app, client):
    """Ein zweiter Callback mit demselben Flow muss scheitern (Replay)."""
    with app.app_context():
        _entra_nutzer()
    _flow_setzen(client)

    with _mit_token(_antwort()):
        erste = client.get("/auth/entra/callback?code=c&state=s1")
        assert erste.status_code == 302

    # Session leeren, aber denselben Code erneut senden: ohne Flow kein Login.
    with client.session_transaction() as sess:
        sess.clear()
    with _mit_token(_antwort()):
        zweite = client.get("/auth/entra/callback?code=c&state=s1")

    assert zweite.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_fehler_vom_provider_wird_abgewiesen(app, client):
    with app.app_context():
        _entra_nutzer()
    _flow_setzen(client)

    with _mit_token(_antwort(fehler="invalid_grant")):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


# --------------------------------------------------------- Provisionierung --
def test_auto_provisioning_legt_konto_mit_startrolle_an(app, client):
    _flow_setzen(client)

    with (
        patch("hookwise.user_api.auto_provision_aktiv", return_value=True),
        patch("hookwise.user_api.auto_provision_rolle", return_value="viewer"),
        _mit_token(_antwort(upn="neu@example.test")),
    ):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with app.app_context():
        neu = User.query.filter_by(upn="neu@example.test").first()
        assert neu is not None and neu.auth_source == "entra"
        viewer = RbacRole.query.filter_by(key="viewer").first()
        assert RbacUserRole.query.filter_by(user_id=neu.id, role_id=viewer.id).first() is not None
        assert AuditLog.query.filter_by(action="entra_auto_provisioned").count() == 1


def test_auto_provisioning_lehnt_privilegierte_startrolle_ab(app, client):
    """Die Startrolle darf keinen Administrator erzeugen -- auch nicht, wenn
    der Schalter auf eine privilegierte Rolle zeigt."""
    _flow_setzen(client)

    with (
        patch("hookwise.user_api.auto_provision_aktiv", return_value=True),
        patch("hookwise.user_api.auto_provision_rolle", return_value="admin"),
        _mit_token(_antwort(upn="moechtegern@example.test")),
    ):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess
    with app.app_context():
        assert User.query.filter_by(upn="moechtegern@example.test").first() is None


def test_ohne_aktiven_flow_kein_login(app, client):
    """Direkter Aufruf des Callbacks ohne vorherigen Login-Start."""
    with app.app_context():
        _entra_nutzer()

    with _mit_token(_antwort()):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_ohne_bibliothek_bleibt_entra_inaktiv(app, client, monkeypatch):
    """Fehlt msal, sind die Entra-Routen wirkungslos -- die lokale Anmeldung
    laeuft unveraendert weiter.

    ``sys.modules[...] = None`` laesst den Import scheitern, unabhaengig davon
    ob das Paket installiert ist; sonst pruefte der Test nur die Umgebung.
    """
    import sys

    monkeypatch.setitem(sys.modules, "msal", None)

    from hookwise.auth_entra import entra_aktiv

    with app.app_context():
        assert entra_aktiv() is False

    antwort = client.get("/auth/entra/login", follow_redirects=False)
    assert antwort.status_code == 302
    assert "/login" in antwort.headers["Location"]

    _flow_setzen(client)
    callback = client.get("/auth/entra/callback?code=c&state=s1")
    assert callback.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_ohne_lesbares_secret_bleibt_entra_inaktiv(app, monkeypatch, tmp_path):
    """Ein Pfad allein genuegt nicht -- ohne Inhalt gibt es kein Credential.

    msal wird gestellt, damit allein das Secret ueber das Ergebnis entscheidet
    und der Test nicht davon abhaengt, ob das Paket im Image liegt.
    """
    import sys
    import types

    monkeypatch.setitem(sys.modules, "msal", types.ModuleType("msal"))

    from hookwise.auth_entra import entra_aktiv

    with app.app_context():
        assert entra_aktiv() is True

        leer = tmp_path / "leer"
        leer.write_text("", encoding="utf-8")
        monkeypatch.setenv("ENTRA_CLIENT_SECRET_FILE", str(leer))
        assert entra_aktiv() is False

        monkeypatch.setenv("ENTRA_CLIENT_SECRET_FILE", str(tmp_path / "gibt-es-nicht"))
        assert entra_aktiv() is False


def test_gebundenes_konto_nicht_ueber_upn_uebernehmbar(app, client):
    """Wird eine UPN in Entra neu vergeben, darf der neue Inhaber nicht das
    Konto des alten erben. Nach der Bindung zaehlt allein die oid."""
    with app.app_context():
        _entra_nutzer(upn="alt@example.test", oid="oid-1")
    _flow_setzen(client)

    with _mit_token(_antwort(oid="oid-2", upn="alt@example.test")):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess
    with app.app_context():
        assert User.query.filter_by(upn="alt@example.test").first().entra_oid == "oid-1"


def test_token_ohne_oid_wird_abgewiesen(app, client):
    """Ohne den unveraenderlichen Anker gibt es keine sichere Zuordnung."""
    with app.app_context():
        _entra_nutzer()
    _flow_setzen(client)

    with _mit_token(_antwort(oid=None)):
        antwort = client.get("/auth/entra/callback?code=c&state=s1")

    assert antwort.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_gruppenfilter_wird_durchgesetzt(app, client):
    """Ist ein Filter gesetzt, muss der Token die Gruppe fuehren -- und ein
    fehlender groups-Claim weist ab, statt durchzuwinken."""
    with app.app_context():
        _entra_nutzer()

    with patch("hookwise.user_api.entra_gruppenfilter", return_value="gruppe-1"):
        _flow_setzen(client)
        with _mit_token(_antwort()):  # ohne groups-Claim
            ohne = client.get("/auth/entra/callback?code=c&state=s1")
        assert ohne.status_code == 302
        with client.session_transaction() as sess:
            assert "user_id" not in sess

        _flow_setzen(client)
        with _mit_token(_antwort(gruppen=["gruppe-9"])):
            fremd = client.get("/auth/entra/callback?code=c&state=s1")
        assert fremd.status_code == 302
        with client.session_transaction() as sess:
            assert "user_id" not in sess

        _flow_setzen(client)
        with _mit_token(_antwort(gruppen=["gruppe-1", "gruppe-9"])):
            passend = client.get("/auth/entra/callback?code=c&state=s1")
        assert passend.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("user_id")

    with app.app_context():
        assert AuditLog.query.filter_by(action="entra_login_denied").count() == 2
