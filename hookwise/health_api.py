"""Route registration for health and LLM infrastructure endpoints."""

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint


def register_health_routes(blueprint: Blueprint, handlers: Mapping[str, Callable[..., Any]]) -> None:
    routes = (
        ("/health/llm", "health_llm", ["GET"]),
        ("/api/health/llm", "api_health_llm", ["GET"]),
        ("/readyz", "readyz", ["GET"]),
        ("/health", "health", ["GET"]),
        ("/health/services", "health_services", ["GET"]),
    )
    for rule, endpoint, methods in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=handlers[endpoint], methods=methods)
