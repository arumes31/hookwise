"""Rechteprüfung an der Route.

Jede Route deklariert ihr Recht ueber ``@requires`` oder wird ausdruecklich als
oeffentlich markiert. Ein Startup-Check laeuft ueber ``app.url_map`` und meldet
Routen ohne Deklaration -- das ist die Absicherung gegen den haeufigsten
RBAC-Fehler: die eine vergessene Route.

``RBAC_ENFORCE`` steuert die Schaerfe:
  ``off``  wie vor der Einfuehrung, keine Pruefung
  ``log``  prueft und protokolliert, blockiert aber nicht (Rollout-Phase)
  ``on``   erzwingt
"""

import logging
from functools import wraps
from typing import Any, Callable, Dict, List, Set

from flask import current_app, jsonify, render_template, request, session

from .resolver import current_permissions

_logger = logging.getLogger(__name__)

#: endpoint -> benoetigtes Recht. Wird von @requires gefuellt.
ROUTE_PERMISSIONS: Dict[str, str] = {}

#: Endpunkte ohne Rechteprüfung (Login, Health, Webhook-Ingest, statische Dateien).
PUBLIC_ENDPOINTS: Set[str] = set()


def public_endpoint(f: Callable[..., Any]) -> Callable[..., Any]:
    """Markiert eine Route ausdruecklich als rechtefrei."""
    f._hw_public = True  # type: ignore[attr-defined]
    return f


def routen_recht_fehlt() -> str | None:
    """Harter Eigen-Check fuer Routen, die ihre Grenze schon immer selbst
    durchgesetzt haben (Secrets, Replay): prueft das Registry-Recht der
    laufenden Route gegen die echten Rechte -- bewusst unabhaengig vom
    ``RBAC_ENFORCE``-Modus, damit diese Routen auch in der Rollout-Phase
    nie offener sind als vor der RBAC-Einfuehrung."""
    from .routes import ENDPUNKT_RECHTE

    noetig = ENDPUNKT_RECHTE.get(request.endpoint or "")
    if noetig and noetig not in current_permissions():
        return noetig
    return None


def _verweigern(permission: str) -> Any:
    """Antwort auf eine fehlende Berechtigung, passend zum Client."""
    from ..utils import log_audit

    try:
        log_audit(
            "perm_denied",
            None,
            f"{session.get('username', '?')} lacks {permission} for {request.path}",
        )
    except Exception:  # pragma: no cover
        pass

    akzeptiert_json = request.accept_mimetypes.best == "application/json"
    if request.path.startswith("/api/") or akzeptiert_json or request.method != "GET":
        return jsonify({"status": "error", "message": "Insufficient permissions", "required": permission}), 403
    # Seitenaufrufe bekommen eine echte 403-Seite statt einer Umleitung: eine
    # Umleitung waere von der Login-Umleitung nicht zu unterscheiden -- weder
    # fuer Nutzer noch fuer Tests.
    return render_template("403.html", required=permission), 403


def requires(permission: str) -> Callable[..., Any]:
    """Bindet eine Route an ein Recht aus dem Katalog."""
    from .catalog import ALL_PERMISSIONS

    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unbekannte Permission: {permission}")

    def dekorator(f: Callable[..., Any]) -> Callable[..., Any]:
        f._hw_permission = permission  # type: ignore[attr-defined]

        @wraps(f)
        def innen(*args: Any, **kwargs: Any) -> Any:
            modus = current_app.config.get("RBAC_ENFORCE", "log")
            if modus == "off":
                return f(*args, **kwargs)

            if permission in current_permissions():
                return f(*args, **kwargs)

            if modus == "log":
                _logger.warning(
                    "RBAC(log): %s fehlt %s fuer %s -- nicht blockiert",
                    session.get("username", "?"),
                    permission,
                    request.path,
                )
                return f(*args, **kwargs)
            return _verweigern(permission)

        innen._hw_permission = permission  # type: ignore[attr-defined]
        return innen

    return dekorator


def verify_route_coverage(app: Any, streng: bool = False) -> List[str]:
    """Meldet Routen ohne Rechtedeklaration.

    ``streng=True`` bricht den Start ab -- so kann eine neue Route nicht
    ungeprueft in Produktion gelangen.
    """
    from .routes import ENDPUNKT_RECHTE, ist_frei

    offen: List[str] = []
    bekannt = set()
    for regel in app.url_map.iter_rules():
        endpunkt = regel.endpoint
        if endpunkt == "static":
            continue
        bekannt.add(endpunkt)
        sicht = app.view_functions.get(endpunkt)
        if sicht is None:
            continue
        if getattr(sicht, "_hw_public", False) or endpunkt in PUBLIC_ENDPOINTS or ist_frei(endpunkt):
            continue
        # Decorator hat Vorrang vor der zentralen Zuordnung.
        recht = getattr(sicht, "_hw_permission", None) or ENDPUNKT_RECHTE.get(endpunkt)
        if recht:
            ROUTE_PERMISSIONS[endpunkt] = recht
            continue
        offen.append(endpunkt)

    # Karteileichen melden: Eintraege fuer Routen, die es nicht mehr gibt.
    verwaist = sorted(e for e in ENDPUNKT_RECHTE if e not in bekannt)
    if verwaist:
        _logger.warning("RBAC: Zuordnung fuer unbekannte Routen: %s", ", ".join(verwaist))

    if offen:
        meldung = "Routen ohne Rechtedeklaration: " + ", ".join(sorted(offen))
        if streng:
            raise RuntimeError(meldung)
        _logger.warning("RBAC: %s", meldung)
    return sorted(offen)


def install_guard(app: Any) -> None:
    """Setzt die zentrale Zuordnung als before_request durch.

    Zweite Verteidigungslinie neben @requires: Auch eine Route ohne Decorator
    wird geprueft, solange sie in der Zuordnung steht.
    """
    from flask import request as _request

    from .routes import ist_frei, recht_fuer

    @app.before_request
    def _rbac_guard() -> Any:  # pragma: no cover - ueber Integrationstests geprueft
        modus = app.config.get("RBAC_ENFORCE", "log")
        if modus == "off":
            return None
        endpunkt = _request.endpoint
        if not endpunkt or endpunkt == "static" or ist_frei(endpunkt):
            return None
        if "user_id" not in session:
            return None  # auth_required kuemmert sich um die Anmeldung
        sicht = app.view_functions.get(endpunkt)
        if getattr(sicht, "_hw_permission", None):
            return None  # der Decorator prueft bereits
        recht = recht_fuer(endpunkt)
        if not recht or recht in current_permissions():
            return None
        if modus == "log":
            _logger.warning(
                "RBAC(log): %s fehlt %s fuer %s -- nicht blockiert",
                session.get("username", "?"),
                recht,
                _request.path,
            )
            return None
        return _verweigern(recht)
