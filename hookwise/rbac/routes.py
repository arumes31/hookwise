"""Zuordnung Route -> Recht.

Abweichung vom Entwurf, bewusst: Statt 85 Endpunkte quer ueber acht Dateien mit
``@requires`` zu dekorieren, liegt die Zuordnung zentral hier und wird von einem
``before_request``-Guard durchgesetzt. Gruende:

* Das Repository steht unter Parallelentwicklung -- 85 verstreute Aenderungen
  waeren ein Konfliktfeld, diese eine Datei ist es nicht.
* Die Rechtevergabe ist an einer Stelle lesbar und pruefbar, statt sie aus acht
  Dateien zusammensuchen zu muessen.

``@requires`` funktioniert weiterhin und hat Vorrang; neue Routen koennen also
beides nutzen. Der Vollstaendigkeitscheck beim Start prueft gegen beide Quellen,
sodass eine neue Route nicht ungeprueft durchrutschen kann.
"""

from typing import Dict, FrozenSet, Set

#: Ohne Anmeldung erreichbar -- Login, Health-Sonden, Webhook-Ingest.
#: Der Ingest traegt seine eigene Authentifizierung (Bearer/HMAC pro Endpoint).
OEFFENTLICH: FrozenSet[str] = frozenset(
    {
        "main.login",
        "main.logout",
        "main.health",
        "main.readyz",
        "main.metrics",
        "main.favicon_ico",
        "main.dynamic_webhook",
        "main.maintenance_mode_page",
    }
)

#: Angemeldet, aber ohne besonderes Recht: betrifft ausschliesslich die eigenen
#: Daten oder die eigene Sitzung.
EIGENE_DATEN: FrozenSet[str] = frozenset(
    {
        "main.setup_2fa",
        "main.disable_2fa",
        "main.dashboard_preferences",
        "main.saved_history_searches",
        "main.delete_saved_history_search",
        "main.activity_annotation",
        "main.submit_feedback",
    }
)

#: Alles Uebrige. Fehlt ein Endpunkt hier, meldet der Startup-Check ihn.
ENDPUNKT_RECHTE: Dict[str, str] = {
    # ---- Dashboard -------------------------------------------------------
    "main.index": "dashboard:read",
    "main.dashboard_overview": "dashboard:read",
    "main.dashboard_analytics": "dashboard:read",
    "main.get_stats": "dashboard:read",
    "main.get_stats_history": "dashboard:read",
    "main.activity_stream": "dashboard:read",
    "main.get_activity_history": "dashboard:read",
    # ---- Endpoints lesen -------------------------------------------------
    "main.webhooks": "endpoint:read",
    "main.webhook_detail": "endpoint:read",
    "main.webhook_detail_json": "endpoint:read",
    "main.endpoint_summary": "endpoint:read",
    "main.export_endpoint": "endpoint:read",
    "main.bulk_export_endpoints": "endpoint:read",
    "main.get_cw_boards": "endpoint:read",
    "main.get_cw_companies": "endpoint:read",
    "main.get_cw_items": "endpoint:read",
    "main.get_cw_priorities": "endpoint:read",
    "main.get_cw_statuses": "endpoint:read",
    "main.get_cw_subtypes": "endpoint:read",
    "main.get_cw_types": "endpoint:read",
    # ---- Endpoints pflegen ----------------------------------------------
    "main.new_endpoint": "endpoint:write",
    "main.edit_endpoint": "endpoint:write",
    "main.quick_update_endpoint": "endpoint:write",
    "main.clone_endpoint": "endpoint:write",
    "main.import_endpoint": "endpoint:write",
    "main.bulk_update_endpoints": "endpoint:write",
    "main.reorder_endpoints": "endpoint:write",
    "main.toggle_pin": "endpoint:write",
    "main.toggle_endpoint": "endpoint:write",
    "main.bulk_pause_endpoints": "endpoint:write",
    "main.bulk_resume_endpoints": "endpoint:write",
    # ---- Archivieren -----------------------------------------------------
    "main.archive_endpoint": "endpoint:archive",
    "main.restore_endpoint": "endpoint:archive",
    "main.bulk_archive_endpoints": "endpoint:archive",
    # ---- Testen ----------------------------------------------------------
    "main.test_endpoint": "endpoint:test",
    "main.dry_run_endpoint": "endpoint:test",
    "main.debug_process": "endpoint:test",
    "main.dry_run_llm": "endpoint:test",
    "main.dry_run_llm_status": "endpoint:test",
    # ---- Secrets ---------------------------------------------------------
    "main.get_endpoint_token": "secret:reveal",
    "main.rotate_token": "secret:rotate",
    # ---- History ---------------------------------------------------------
    "main.history": "history:read",
    "main.history_advanced": "history:read",
    "main.history_diagnostics": "history:read",
    "main.history_operations": "history:read",
    "main.retry_history_log": "history:retry",
    "main.replay_webhook": "history:retry",
    "main.replay_with_edits": "history:retry",
    "main.replay_dead_letters": "history:retry",
    "main.delete_log": "history:delete",
    "main.delete_all_logs": "history:delete",
    "main.bulk_delete_logs": "history:delete",
    # ---- TenantMap -------------------------------------------------------
    "main.tenantmap": "tenantmap:read",
    "main.add_mapping": "tenantmap:write",
    "main.edit_mapping": "tenantmap:write",
    "main.delete_mapping": "tenantmap:write",
    # ---- Audit -----------------------------------------------------------
    "main.audit_logs": "audit:read",
    # ---- Einstellungen ---------------------------------------------------
    "main.settings": "settings:read",
    "main.health_services": "settings:read",
    "main.health_llm": "settings:read",
    "main.api_health_llm": "settings:read",
    "main.llm_test_status": "settings:read",
    "main.update_settings": "settings:write",
    "main.clear_cache": "settings:write",
    "main.generate_api_key": "settings:write",
    "main.llm_test": "settings:write",
    "main.backup_config": "settings:write",
    "main.restore_config": "settings:write",
    "main.trigger_timeout_check": "settings:write",
    "main.maintenance_mode": "settings:write",
    # ---- Nutzerverwaltung ------------------------------------------------
    "main.users_list": "user:read",
    "main.roles_list": "user:read",
    "main.permission_matrix": "user:read",
    "main.user_create": "user:manage",
    "main.user_update": "user:manage",
    "main.user_delete": "user:manage",
    "main.user_set_password": "user:manage",
    "main.user_reset_mfa": "user:manage",
    "main.user_roles_set": "user:manage",
    "main.user_reset_entra": "user:manage",
    "main.role_create": "user:manage",
    "main.role_update": "user:manage",
    "main.role_delete": "user:manage",
    "main.identity_settings": "user:read",
    "main.entra_settings_update": "user:manage",
    # ---- Entra-Anmeldung (oeffentlich, aber eigener Flow) -----------------
    "main.entra_login": None,  # type: ignore[dict-item]
    "main.entra_callback": None,  # type: ignore[dict-item]
}

#: Endpunkte, die zwar hier stehen, aber kein Recht brauchen (Wert ``None``).
_OHNE_RECHT: Set[str] = {e for e, r in ENDPUNKT_RECHTE.items() if r is None}


def recht_fuer(endpunkt: str) -> str | None:
    """Benoetigtes Recht, oder ``None`` wenn keins noetig ist."""
    return ENDPUNKT_RECHTE.get(endpunkt)


def ist_frei(endpunkt: str) -> bool:
    return endpunkt in OEFFENTLICH or endpunkt in EIGENE_DATEN or endpunkt in _OHNE_RECHT
