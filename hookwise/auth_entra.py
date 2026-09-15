"""Microsoft Entra ID (OpenID Connect), nach dem wiredraft-Muster.

Authorization Code Flow mit PKCE, single-tenant. Gespeichert wird nur das
stabile ``tid``/``oid``-Paar -- niemals ID-, Access- oder Refresh-Token.

Die Token-Validierung uebernimmt ``msal`` (ADR-003): Signatur gegen die JWKS
des Tenants, ``iss``, ``aud``, ``exp``/``nbf`` und ``nonce``. Die Tenant-Pruefung
(``tid``) ergaenzen wir ausdruecklich, weil sie nicht in jeder Konfiguration
automatisch greift.

Fehlt ``msal`` im Image, bleibt das Modul inaktiv und die Routen werden nicht
registriert -- die Anwendung laeuft dann unveraendert mit lokaler Anmeldung.
"""

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from flask import Blueprint, flash, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from .extensions import db
from .models import User
from .utils import log_audit

_logger = logging.getLogger(__name__)

SESSION_FLOW = "entra_flow"
AUTORITAET = "https://login.microsoftonline.com/{tenant}"


def _konfiguration() -> Dict[str, str]:
    return {
        "tenant": os.environ.get("ENTRA_TENANT_ID", "").strip(),
        "client": os.environ.get("ENTRA_CLIENT_ID", "").strip(),
        "secret_file": os.environ.get("ENTRA_CLIENT_SECRET_FILE", "").strip(),
        "redirect": os.environ.get("ENTRA_REDIRECT_URL", "").strip(),
        "scopes": os.environ.get("ENTRA_SCOPES", "openid profile email").strip(),
    }


def _secret_lesen(pfad: str) -> Optional[str]:
    """Das Client Secret kommt ausschliesslich aus einer gemounteten Datei."""
    if not pfad:
        return None
    try:
        with open(pfad, "r", encoding="utf-8") as datei:
            return datei.read().strip() or None
    except OSError:
        _logger.error("Entra: Secret-Datei %s nicht lesbar", pfad)
        return None


def entra_aktiv() -> bool:
    """Ist Entra eingeschaltet und vollstaendig konfiguriert?"""
    if os.environ.get("ENTRA_ENABLED", "false").strip().lower() != "true":
        return False
    konf = _konfiguration()
    if not all([konf["tenant"], konf["client"], konf["redirect"]]):
        return False
    # Der Pfad allein sagt nichts: Ist die Datei leer oder nicht lesbar, wuerde
    # der Client ohne Credential gebaut und jeder Token-Tausch scheitern --
    # dann ist Entra nicht "aktiv", sondern kaputt.
    if not _secret_lesen(konf["secret_file"]):
        return False
    try:
        import msal  # noqa: F401
    except ImportError:
        return False
    return True


def _client() -> Any:
    import msal

    konf = _konfiguration()
    return msal.ConfidentialClientApplication(
        konf["client"],
        authority=AUTORITAET.format(tenant=konf["tenant"]),
        client_credential=_secret_lesen(konf["secret_file"]),
    )


def _abweisen(grund: str, upn: str = "?") -> Any:
    log_audit("entra_login_denied", None, f"{upn}: {grund}")
    flash("Sign-in was refused. Contact an administrator if this is unexpected.")
    return redirect(url_for("main.login"))


def _nutzer_finden(anspruch: Dict[str, Any]) -> Optional[User]:
    """Bindung ueber ``tid``/``oid``, sonst einmalig ueber die UPN."""
    tid = str(anspruch.get("tid") or "")
    oid = str(anspruch.get("oid") or "")
    upn = str(anspruch.get("preferred_username") or anspruch.get("upn") or "").strip()

    if tid and oid:
        # Object IDs are tenant-scoped. Looking up oid alone would let an
        # account bind across tenants after an administrator changes the
        # single-tenant configuration, despite persisting the tid/oid pair.
        gebunden = User.query.filter_by(entra_tid=tid, entra_oid=oid).first()
        if gebunden is not None:
            return gebunden
    if upn:
        # Erstanmeldung ueber die verifizierte UPN -- aber nur fuer noch
        # ungebundene Konten. Sonst uebernaehme der neue Inhaber einer in Entra
        # wiederverwendeten UPN das Konto des alten, samt dessen Rollen.
        kandidat = User.query.filter(
            db.func.lower(User.upn) == upn.lower(),
            User.auth_source == "entra",
            User.entra_oid.is_(None),
        ).first()
        if kandidat is None:
            kandidat = User.query.filter(
                db.func.lower(User.username) == upn.lower(),
                User.auth_source == "entra",
                User.entra_oid.is_(None),
            ).first()
        return kandidat
    return None


def _app_rolle_ermitteln(anspruch: Mapping[str, Any], mapping: Mapping[str, str]) -> Optional[str]:
    """Uebersetzt Entra App Roles deterministisch in eine Hookwise-Rolle.

    App-Role-Werte sind absichtlich case-sensitive: Sie sind stabile
    Maschinenwerte aus der App Registration, keine Anzeigenamen. Der Claim darf
    weitere Rollen enthalten; nur die zwei konfigurierten Hookwise-Werte zaehlen.
    """
    if set(mapping.values()) != {"viewer", "operator"}:
        raise ValueError("App Role mapping must contain distinct viewer and operator values")
    rollen = anspruch.get("roles")
    if not isinstance(rollen, list) or not all(isinstance(rolle, str) for rolle in rollen):
        return None
    treffer = {mapping[rolle] for rolle in rollen if rolle in mapping}
    if "operator" in treffer:
        return "operator"
    if "viewer" in treffer:
        return "viewer"
    return None


def _entra_rolle_speichern(nutzer: User, rollen_key: str) -> bool:
    """Materialisiert die App Role und ersetzt alte Entra-Zuweisungen."""
    from .models import RbacRole, RbacUserRole
    from .rbac.resolver import schema_bereit

    nutzer.entra_role = rollen_key
    nutzer.entra_role_synced_at = datetime.now(timezone.utc)
    nutzer.role = rollen_key  # Legacy-Fallback bei noch nicht bereitem RBAC-Schema.

    if not schema_bereit():
        return True
    rolle = RbacRole.query.filter_by(key=rollen_key).first()
    if rolle is None:
        _logger.error("Entra: eingebaute Zielrolle %s existiert nicht", rollen_key)
        return False
    RbacUserRole.query.filter_by(user_id=nutzer.id).delete(synchronize_session=False)
    db.session.add(
        RbacUserRole(
            user_id=nutzer.id,
            role_id=rolle.id,
            granted_by="entra-app-role",
            granted_at=datetime.now(timezone.utc),
        )
    )
    return True


def _automatisch_anlegen(anspruch: Dict[str, Any], rollen_key: str) -> Optional[User]:
    """Auto-Provisioning, falls der Laufzeitschalter es erlaubt (ADR-002)."""
    from .user_api import auto_provision_aktiv

    if not auto_provision_aktiv():
        return None

    upn = str(anspruch.get("preferred_username") or anspruch.get("upn") or "").strip()
    if not upn:
        return None

    nutzer = User(
        username=upn,
        password_hash=generate_password_hash(secrets.token_urlsafe(32)),
        role=rollen_key,
        auth_source="entra",
        upn=upn,
        is_active=True,
        entra_role=rollen_key,
        entra_role_synced_at=datetime.now(timezone.utc),
    )
    db.session.add(nutzer)
    db.session.flush()
    log_audit(
        "entra_auto_provisioned",
        None,
        f"{upn} created from App Role with role {rollen_key}",
        commit=False,
    )
    return nutzer


def register_entra_routes(main_bp: Blueprint) -> None:
    """Registriert Login und Callback -- nur wenn Entra einsatzbereit ist."""

    @main_bp.route("/auth/entra/login")
    def entra_login() -> Any:
        if not entra_aktiv():
            return redirect(url_for("main.login"))
        konf = _konfiguration()
        flow = _client().initiate_auth_code_flow(
            scopes=[s for s in konf["scopes"].split() if s not in ("openid", "profile", "email")],
            redirect_uri=konf["redirect"],
        )
        # state, nonce und PKCE-Verifier liegen serverseitig in der Session und
        # sind einmalig verwendbar -- Schutz gegen Replay und CSRF am Callback.
        session[SESSION_FLOW] = flow
        return redirect(flow["auth_uri"])

    @main_bp.route("/auth/entra/callback")
    def entra_callback() -> Any:
        if not entra_aktiv():
            return redirect(url_for("main.login"))

        flow = session.pop(SESSION_FLOW, None)  # einmalig!
        if not flow:
            return _abweisen("no active flow (state reuse or expired session)")

        try:
            ergebnis = _client().acquire_token_by_auth_code_flow(flow, request.args)
        except Exception:
            _logger.exception("Entra: Token-Tausch fehlgeschlagen")
            return _abweisen("token exchange failed")

        if "error" in ergebnis:
            return _abweisen(str(ergebnis.get("error_description") or ergebnis["error"]))

        anspruch = ergebnis.get("id_token_claims") or {}
        token_upn = str(anspruch.get("preferred_username") or anspruch.get("upn") or "").strip()
        upn = token_upn or "?"

        # Tenant ausdruecklich pruefen.
        erwartet = _konfiguration()["tenant"]
        if str(anspruch.get("tid") or "") != erwartet:
            return _abweisen("foreign tenant", upn)

        # Die oid ist der unveraenderliche Anker der Identitaet. Ohne sie laesst
        # sich ein Konto weder sicher zuordnen noch binden.
        if not str(anspruch.get("oid") or ""):
            return _abweisen("token without oid", upn)

        # App Roles sind die einzige Entra-Autorisierungsquelle. Sie begrenzen
        # den Token auf anwendungsspezifische Werte und vermeiden Groups-
        # Overage. Fehlt eine bekannte Rolle, wird nicht auf Viewer gefallen.
        from .user_api import entra_app_rollen

        try:
            rollen_key = _app_rolle_ermitteln(anspruch, entra_app_rollen())
        except ValueError:
            _logger.exception("Entra: App-Role-Mapping ist ungueltig")
            return _abweisen("invalid App Role mapping", upn)
        if rollen_key is None:
            return _abweisen("no recognized Hookwise App Role", upn)

        nutzer = _nutzer_finden(anspruch)
        if nutzer is None:
            nutzer = _automatisch_anlegen(anspruch, rollen_key)
        if nutzer is None:
            return _abweisen("no matching HookWise account", upn)
        if not nutzer.aktiv:
            db.session.rollback()
            return _abweisen("account disabled", upn)

        # Bindung an das unveraenderliche Paar festschreiben.
        oid = str(anspruch.get("oid") or "")
        if oid and not nutzer.entra_oid:
            nutzer.entra_oid = oid
            nutzer.entra_tid = str(anspruch.get("tid") or "")
        # The immutable tid/oid pair identifies the account; the UPN is mutable
        # directory metadata and is refreshed after Microsoft-side renames.
        if token_upn:
            nutzer.upn = token_upn
        if not _entra_rolle_speichern(nutzer, rollen_key):
            db.session.rollback()
            return _abweisen("Hookwise target role unavailable", upn)
        nutzer.last_login_at = datetime.now(timezone.utc)
        db.session.commit()

        # Session-Rotation gegen Session Fixation.
        session.clear()
        session["user_id"] = nutzer.id
        session["username"] = nutzer.username
        session["auth_source"] = "entra"

        from .auth import anmeldung_abschliessen

        anmeldung_abschliessen(nutzer)
        log_audit(
            "entra_login",
            None,
            f"{upn} signed in via Entra ID with App Role {rollen_key}",
        )
        return redirect(url_for("main.index"))


def entra_konfiguration_pruefen() -> Dict[str, Any]:
    """Zustand fuer die Einstellungsseite."""
    konf = _konfiguration()
    try:
        import msal  # noqa: F401

        bibliothek = True
    except ImportError:
        bibliothek = False
    return {
        "enabled": os.environ.get("ENTRA_ENABLED", "false").strip().lower() == "true",
        "library": bibliothek,
        "tenant": bool(konf["tenant"]),
        "client": bool(konf["client"]),
        "redirect": konf["redirect"],
        "secret_readable": _secret_lesen(konf["secret_file"]) is not None,
        "ready": entra_aktiv(),
    }
