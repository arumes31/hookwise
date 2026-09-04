"""Route registration for administrator settings and maintenance endpoints."""

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint


def register_admin_routes(blueprint: Blueprint, handlers: Mapping[str, Callable[..., Any]]) -> None:
    routes = (
        ("/admin/maintenance", "maintenance_mode", ["GET", "POST"]),
        ("/settings", "settings", ["GET"]),
        ("/settings/update", "update_settings", ["POST"]),
        ("/admin/clear-cache", "clear_cache", ["POST"]),
        ("/admin/generate-api-key", "generate_api_key", ["POST"]),
        ("/admin/llm-test", "llm_test", ["POST"]),
        ("/admin/llm-test/status/<task_id>", "llm_test_status", ["GET"]),
        ("/api/feedback", "submit_feedback", ["POST"]),
    )
    for rule, endpoint, methods in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=handlers[endpoint], methods=methods)
