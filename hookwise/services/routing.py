"""Pure, resource-bounded webhook mapping and routing decisions."""

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

import regex as safe_regex

from ..utils import resolve_jsonpath, resolve_monitor_name

ROUTING_REGEX_TIMEOUT_SECONDS = 0.05
_TOKEN_RE = re.compile(r"(\$\S+|[^\s]+)")
_OVERRIDABLE_FIELDS = (
    "summary",
    "description",
    "customer_id",
    "ticket_type",
    "subtype",
    "item",
    "priority",
    "board",
    "status",
    "severity",
    "impact",
    "configuration_id",
    "configuration_device_id",
    "configuration_serial",
    "configuration_mac",
    "configuration_tag",
    "configuration_ip",
    "configuration_name",
)


#: Upper bound for tokens in one space-separated mapping expression.
MAX_MAPPING_EXPRESSION_TOKENS = 256


def routing_regex_matches(pattern: str, value: str) -> bool:
    """Evaluate an administrator-defined expression with strict resource bounds."""
    if len(pattern) > 1_000 or len(value) > 100_000:
        return False
    try:
        return (
            safe_regex.search(
                pattern,
                value,
                safe_regex.IGNORECASE,
                timeout=ROUTING_REGEX_TIMEOUT_SECONDS,
            )
            is not None
        )
    except safe_regex.error, TimeoutError:
        return False


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except TypeError, ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except TypeError, ValueError:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def apply_mapping(data: dict[str, Any], raw_mapping: str | None) -> dict[str, str]:
    """Resolve supported mapped fields, including mixed literal/JSONPath templates."""
    mapping = _json_object(raw_mapping)
    mapped: dict[str, str] = {}
    for field in _OVERRIDABLE_FIELDS:
        expression = mapping.get(field)
        if not isinstance(expression, str):
            continue
        if " " not in expression:
            value = resolve_jsonpath(data, expression)
            if value is not None:
                mapped[field] = str(value)
            continue

        tokens = _TOKEN_RE.findall(expression)
        if len(tokens) > MAX_MAPPING_EXPRESSION_TOKENS:
            # A mapping expression is a short label template; an oversized one
            # is skipped rather than evaluated token by token.
            continue
        resolved: list[tuple[str, bool]] = []
        for token in tokens:
            if token.startswith("$"):
                value = resolve_jsonpath(data, token)
                resolved.append((str(value).strip() if value is not None else "", True))
            else:
                resolved.append((token, False))
        if not any(value and variable for value, variable in resolved):
            continue
        # Literal tokens are kept only next to populated variables somewhere on
        # either side. Count populated variables once instead of rescanning the
        # prefix and suffix for every literal token.
        populated_total = sum(1 for value, variable in resolved if variable and value)
        populated_before = 0
        parts: list[str] = []
        for value, variable in resolved:
            if variable:
                if value:
                    parts.append(value)
                    populated_before += 1
                continue
            if populated_before or populated_total - populated_before:
                parts.append(value)
        if parts:
            mapped[field] = " ".join(parts)
    return mapped


def apply_rules(data: dict[str, Any], raw_rules: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply matching routing overrides in declaration order."""
    overrides: dict[str, Any] = {}
    matches: list[dict[str, Any]] = []
    for rule in _json_list(raw_rules):
        path = rule.get("path")
        pattern = rule.get("regex")
        if not isinstance(path, str) or not isinstance(pattern, str):
            continue
        value = str(resolve_jsonpath(data, path))
        if not routing_regex_matches(pattern, value):
            continue
        rule_overrides = rule.get("overrides", {})
        if not isinstance(rule_overrides, dict):
            rule_overrides = {}
        overrides.update(rule_overrides)
        matches.append({"path": path, "regex": pattern, "overrides": rule_overrides})
        if rule.get("stop_processing"):
            break
    return overrides, matches


@dataclass(frozen=True)
class RoutingDecision:
    alert_type: str
    action: str
    summary: str
    values: dict[str, Any]
    matched_rules: list[dict[str, Any]]
    steps: list[dict[str, Any]]


def evaluate_routing(data: dict[str, Any], config: Mapping[str, Any]) -> RoutingDecision:
    """Return the common dry-run/production routing decision without side effects."""
    mapped = apply_mapping(data, config.get("json_mapping"))
    overrides, matches = apply_rules(data, config.get("routing_rules"))
    values: dict[str, Any] = {**mapped, **overrides}

    trigger_field = str(config.get("trigger_field") or "heartbeat.status")
    actual = str(resolve_jsonpath(data, trigger_field))
    opens = {part.strip() for part in str(config.get("open_value") or "0").split(",") if part.strip()}
    closes = {part.strip() for part in str(config.get("close_value") or "1").split(",") if part.strip()}
    alert_type = "DOWN" if actual in opens else "UP" if actual in closes else "GENERIC"
    action = "create_ticket" if alert_type == "DOWN" else "close_ticket" if alert_type == "UP" else "add_note_or_skip"

    prefix = str(config.get("ticket_prefix") or "Alert:").strip()
    subject = values.get("summary") or resolve_monitor_name(data)
    summary = f"{prefix} {subject}".strip()
    steps: list[dict[str, Any]] = [
        {"step": "JSONPath Mapping", "resolved": mapped},
        {"step": "Routing Rules", "matched": matches},
        {
            "step": "Trigger Evaluation",
            "trigger_field": trigger_field,
            "actual_value": actual,
            "alert_type": alert_type,
        },
    ]
    return RoutingDecision(alert_type, action, summary, values, matches, steps)
