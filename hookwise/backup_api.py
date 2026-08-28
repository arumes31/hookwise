"""Route registration for configuration backup and restore endpoints."""

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint


def register_backup_routes(blueprint: Blueprint, handlers: Mapping[str, Callable[..., Any]]) -> None:
    blueprint.add_url_rule(
        "/admin/backup", endpoint="backup_config", view_func=handlers["backup_config"], methods=["GET"]
    )
    blueprint.add_url_rule(
        "/admin/restore", endpoint="restore_config", view_func=handlers["restore_config"], methods=["POST"]
    )
