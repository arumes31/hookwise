"""Route registration for health and LLM infrastructure endpoints."""

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint

from .utils import auth_required

#: /health und /readyz bleiben offen -- daran haengen die Container-Healthchecks.
#: /health/services legt Dienstzustaende offen und verlangt darum eine Anmeldung;
#: das Recht (settings:read) prueft danach der RBAC-Guard. Ohne diese Klammer
#: waere die Route anonym erreichbar, weil der Guard nicht angemeldete Aufrufe
#: bewusst an auth_required weiterreicht.
ANMELDEPFLICHTIG = frozenset({"health_services"})


def register_health_routes(blueprint: Blueprint, handlers: Mapping[str, Callable[..., Any]]) -> None:
    routes = (
        ("/health/llm", "health_llm", ["GET"]),
        ("/api/health/llm", "api_health_llm", ["GET"]),
        ("/readyz", "readyz", ["GET"]),
        ("/health", "health", ["GET"]),
        ("/health/services", "health_services", ["GET"]),
    )
    for rule, endpoint, methods in routes:
        sicht = handlers[endpoint]
        if endpoint in ANMELDEPFLICHTIG:
            sicht = auth_required(sicht)
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=sicht, methods=methods)
