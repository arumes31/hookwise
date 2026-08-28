"""Route registration for cached ConnectWise lookup endpoints."""

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint


def register_connectwise_routes(blueprint: Blueprint, handlers: Mapping[str, Callable[..., Any]]) -> None:
    routes = (
        ("/api/cw/boards", "get_cw_boards"),
        ("/api/cw/priorities", "get_cw_priorities"),
        ("/api/cw/statuses/<board_id>", "get_cw_statuses"),
        ("/api/cw/types/<board_id>", "get_cw_types"),
        ("/api/cw/subtypes/<board_id>", "get_cw_subtypes"),
        ("/api/cw/items/<board_id>", "get_cw_items"),
        ("/api/cw/companies", "get_cw_companies"),
    )
    for rule, endpoint in routes:
        blueprint.add_url_rule(rule, endpoint=endpoint, view_func=handlers[endpoint])
