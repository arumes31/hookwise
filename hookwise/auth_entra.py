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
from typing import Any, Dict, Optional

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
    """Bindung ueber die unveraenderliche oid, sonst einmalig ueber die UPN."""
    oid = str(anspruch.get("oid") or "")
    upn = str(anspruch.get("preferred_username") or anspruch.get("upn") or "").strip()

    if oid:
        gebunden = User.query.filter_by(entra_oid=oid).first()
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


def _automatisch_anlegen(anspruch: Dict[str, Any]) -> Optional[User]:
    """Auto-Provisioning, falls der Laufzeitschalter es erlaubt (ADR-002)."""
    from .rbac.catalog import is_assignable_start_role
    from .user_api import auto_provision_aktiv, auto_provision_rolle

    if not auto_provision_aktiv():
        return None

    from .models import RbacRole, RbacRolePermission, RbacUserRole
    from .rbac.resolver import schema_bereit

    upn = str(anspruch.get("preferred_username") or anspruch.get("upn") or "").strip()
    if not upn:
        return None

    rolle = None
    if schema_bereit():
        rolle = RbacRole.query.filter_by(key=auto_provision_rolle()).first()
        if rolle is None:
            _logger.error("Entra: Startrolle %s existiert nicht", auto_provision_rolle())
            return None
        rechte = frozenset(z.permission for z in RbacRolePermission.query.filter_by(role_id=rolle.id))
        # Zweite Pruefung zur Laufzeit: Die Rolle koennte seit dem Setzen des
        # Schalters privilegierte Rechte bekommen haben.
        if not is_assignable_start_role(rechte):
            _logger.error("Entra: Startrolle %s haelt privilegierte Rechte", rolle.key)
            return None

    nutzer = User(
        username=upn,
        password_hash=generate_password_hash(secrets.token_urlsafe(32)),
        role="viewer",
        auth_source="entra",
        upn=upn,
        is_active=True,
    )
    db.session.add(nutzer)
    db.session.flush()
    if rolle is not None:
        db.session.add(RbacUserRole(user_id=nutzer.id, role_id=rolle.id, granted_by="entra-auto"))
    db.session.commit()
    log_audit("entra_auto_provisioned", None, f"{upn} created with role {auto_provision_rolle()}")
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
        upn = str(anspruch.get("preferred_username") or anspruch.get("upn") or "?")

        # Tenant ausdruecklich pruefen.
        erwartet = _konfiguration()["tenant"]
        if str(anspruch.get("tid") or "") != erwartet:
            return _abweisen("foreign tenant", upn)

        # Die oid ist der unveraenderliche Anker der Identitaet. Ohne sie laesst
        # sich ein Konto weder sicher zuordnen noch binden.
        if not str(anspruch.get("oid") or ""):
            return _abweisen("token without oid", upn)

        # Gruppenfilter, falls gesetzt: bewusst fail-closed. Ein Filter, der bei
        # fehlendem groups-Claim durchwinkt, waere nur die Behauptung eines
        # Zugangsschutzes. Die App-Registrierung muss Gruppen-Claims ausgeben.
        from .user_api import entra_gruppenfilter

        gefordert = entra_gruppenfilter()
        if gefordert:
            gruppen = anspruch.get("groups")
            if not isinstance(gruppen, list) or gefordert not in [str(g) for g in gruppen]:
                return _abweisen(f"not in required group {gefordert}", upn)

        nutzer = _nutzer_finden(anspruch)
        if nutzer is None:
            nutzer = _automatisch_anlegen(anspruch)
        if nutzer is None:
            return _abweisen("no matching HookWise account", upn)
        if not nutzer.aktiv:
            return _abweisen("account disabled", upn)

        # Bindung an das unveraenderliche Paar festschreiben.
        oid = str(anspruch.get("oid") or "")
        if oid and not nutzer.entra_oid:
            nutzer.entra_oid = oid
            nutzer.entra_tid = str(anspruch.get("tid") or "")
        if not nutzer.upn:
            nutzer.upn = upn
        nutzer.last_login_at = datetime.now(timezone.utc)
        db.session.commit()

        # Session-Rotation gegen Session Fixation.
        session.clear()
        session["user_id"] = nutzer.id
        session["username"] = nutzer.username
        session["role"] = nutzer.role
        session["auth_source"] = "entra"

        from .auth import anmeldung_abschliessen

        anmeldung_abschliessen(nutzer)
        log_audit("entra_login", None, f"{upn} signed in via Entra ID")
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
