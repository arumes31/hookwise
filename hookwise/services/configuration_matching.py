"""Pure, conservative matching of webhook identifiers to ConnectWise configurations."""

import ipaddress
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

MAX_TEXT_LENGTH = 100_000
MAX_TEXT_TOKENS = 2_000
MAX_CANDIDATES_PER_KIND = 16
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_NODES = 1_000
MAX_IDENTITY_LENGTH = 128
MAX_CONFIGURATIONS = 128

_TEXT_TOKEN_RE = re.compile(r"[^\s<>'\"]+")
_TRAILING_PUNCTUATION = ".,;!?)}"
_IDENTITY_RE = re.compile(r"[\w .':@/()+#&-]+", re.UNICODE)
_MAC_RE = re.compile(r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{12}|[0-9a-f]{4}(?:\.[0-9a-f]{4}){2}")
_GENERIC_NAMES = frozenset(
    {"-", "computer", "device", "endpoint", "host", "localhost", "n/a", "none", "null", "server", "unknown"}
)

_COMMON_KEYS = {
    "ip": "ip_addresses",
    "ipaddress": "ip_addresses",
    "hostip": "ip_addresses",
    "targetip": "ip_addresses",
    "asset": "ip_addresses",
    "target": "ip_addresses",
    "mac": "mac_addresses",
    "macaddress": "mac_addresses",
    "serial": "serial_numbers",
    "serialnumber": "serial_numbers",
    "assettag": "tag_numbers",
    "tagnumber": "tag_numbers",
    "deviceid": "device_identifiers",
    "deviceidentifier": "device_identifiers",
    "hostname": "names",
    "computername": "names",
    "devicename": "names",
    "configurationname": "names",
}
_MAPPED_KEYS = {
    "configuration_id": "configuration_ids",
    "configuration_device_id": "device_identifiers",
    "configuration_serial": "serial_numbers",
    "configuration_mac": "mac_addresses",
    "configuration_tag": "tag_numbers",
    "configuration_ip": "ip_addresses",
    "configuration_name": "names",
}
_TEXT_LABEL_PATTERNS = (
    (
        "device_identifiers",
        re.compile(r"^\s*(?:[-*]\s*)?(?:device id|device identifier)\s*[:=]\s*(.{1,128})\s*$", re.I),
    ),
    ("serial_numbers", re.compile(r"^\s*(?:[-*]\s*)?(?:serial|serial number|s/n)\s*[:=]\s*(.{1,128})\s*$", re.I)),
    ("mac_addresses", re.compile(r"^\s*(?:[-*]\s*)?(?:mac|mac address)\s*[:=]\s*(.{1,128})\s*$", re.I)),
    ("tag_numbers", re.compile(r"^\s*(?:[-*]\s*)?(?:asset tag|tag number)\s*[:=]\s*(.{1,128})\s*$", re.I)),
    ("names", re.compile(r"^\s*(?:[-*]\s*)?(?:hostname|host name|configuration name)\s*[:=]\s*(.{1,128})\s*$", re.I)),
)


@dataclass(frozen=True, slots=True)
class ConfigurationHints:
    """Normalized, immutable identifiers collected from one ticket intent."""

    configuration_ids: tuple[int, ...] = ()
    device_identifiers: tuple[str, ...] = ()
    serial_numbers: tuple[str, ...] = ()
    mac_addresses: tuple[str, ...] = ()
    tag_numbers: tuple[str, ...] = ()
    ip_addresses: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    @property
    def has_identifiers(self) -> bool:
        return any(
            (
                self.configuration_ids,
                self.device_identifiers,
                self.serial_numbers,
                self.mac_addresses,
                self.tag_numbers,
                self.ip_addresses,
                self.names,
            )
        )


MatchStatus = Literal["no_identifiers", "no_match", "ambiguous", "conflict", "matched"]
MatchType = Literal["configuration_id", "device_identifier", "serial", "mac", "tag", "ip", "name"]


@dataclass(frozen=True, slots=True)
class ConfigurationMatchResult:
    """A deterministic selection outcome safe to pass to the association layer."""

    status: MatchStatus
    configuration_id: int | None = None
    match_type: MatchType | None = None


def _canonical_ip(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_LENGTH:
        return None
    candidate = value.strip().rstrip(_TRAILING_PUNCTUATION)
    if not candidate:
        return None
    if "://" in candidate:
        try:
            parsed = urlsplit(candidate)
            if parsed.scheme.casefold() not in {"http", "https"}:
                return None
            _ = parsed.port
            candidate = parsed.hostname or ""
        except ValueError:
            return None
    else:
        candidate = re.sub(r"/(?:tcp|udp)$", "", candidate, flags=re.IGNORECASE)
        bracketed = re.fullmatch(r"\[([^\]]+)](?::(\d{1,5}))?", candidate)
        if bracketed:
            candidate = bracketed.group(1)
            port = bracketed.group(2)
            if port and not 1 <= int(port) <= 65_535:
                return None
        elif candidate.count(":") == 1:
            host, port = candidate.rsplit(":", 1)
            if not port.isdecimal() or not 1 <= int(port) <= 65_535:
                return None
            candidate = host
    if "%" in candidate:
        return None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        return None
    return address.compressed


def _text_ips(text: Any) -> tuple[str, ...]:
    if not isinstance(text, str):
        return ()
    found: dict[str, None] = {}
    for index, token in enumerate(_TEXT_TOKEN_RE.findall(text[:MAX_TEXT_LENGTH])):
        if index >= MAX_TEXT_TOKENS or len(found) >= MAX_CANDIDATES_PER_KIND:
            break
        candidate = _canonical_ip(token)
        if candidate is not None:
            found.setdefault(candidate, None)
    return tuple(found)


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and value.strip().isdecimal():
        candidate = int(value.strip())
    else:
        return None
    return candidate if 0 < candidate <= 9_223_372_036_854_775_807 else None


def _canonical_mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if not _MAC_RE.fullmatch(candidate):
        return None
    canonical = re.sub(r"[:-]|\.", "", candidate)
    if canonical in {"000000000000", "ffffffffffff"}:
        return None
    return canonical


def _canonical_identity(value: Any, *, is_name: bool = False) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    candidate = str(value).strip().casefold()
    if not candidate or len(candidate) > MAX_IDENTITY_LENGTH or not _IDENTITY_RE.fullmatch(candidate):
        return None
    if candidate in _GENERIC_NAMES or (is_name and _canonical_ip(candidate) is not None):
        return None
    return candidate


def _bounded_scalars(value: Any) -> Iterable[Any]:
    if isinstance(value, list | tuple):
        yield from value[:MAX_CANDIDATES_PER_KIND]
    else:
        yield value


def _normalized_values(field: str, value: Any) -> tuple[int | str, ...]:
    normalized: dict[int | str, None] = {}
    for scalar in _bounded_scalars(value):
        candidates: tuple[int | str, ...]
        if field == "configuration_ids":
            id_candidate = _positive_id(scalar)
            candidates = () if id_candidate is None else (id_candidate,)
        elif field == "ip_addresses":
            candidates = _text_ips(scalar)
        elif field == "mac_addresses":
            mac_candidate = _canonical_mac(scalar)
            candidates = () if mac_candidate is None else (mac_candidate,)
        else:
            identity_candidate = _canonical_identity(scalar, is_name=field == "names")
            candidates = () if identity_candidate is None else (identity_candidate,)
        for normalized_candidate in candidates:
            normalized.setdefault(normalized_candidate, None)
            if len(normalized) >= MAX_CANDIDATES_PER_KIND:
                return tuple(normalized)
    return tuple(normalized)


def _discover_payload_values(payload: Mapping[str, Any]) -> dict[str, list[int | str]]:
    discovered: dict[str, list[int | str]] = {field: [] for field in ConfigurationHints.__dataclass_fields__}
    queued: deque[tuple[Any, int]] = deque([(payload, 0)])
    seen_containers: set[int] = set()
    visited = 0
    while queued and visited < MAX_PAYLOAD_NODES:
        value, depth = queued.popleft()
        if isinstance(value, Mapping):
            if id(value) in seen_containers:
                continue
            seen_containers.add(id(value))
            for key, child in value.items():
                visited += 1
                if visited > MAX_PAYLOAD_NODES:
                    break
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                field = _COMMON_KEYS.get(normalized_key)
                if field and len(discovered[field]) < MAX_CANDIDATES_PER_KIND:
                    for candidate in _normalized_values(field, child):
                        if candidate not in discovered[field]:
                            discovered[field].append(candidate)
                        if len(discovered[field]) >= MAX_CANDIDATES_PER_KIND:
                            break
                if depth < MAX_PAYLOAD_DEPTH and isinstance(child, Mapping | list | tuple):
                    queued.append((child, depth + 1))
        elif isinstance(value, list | tuple):
            if id(value) in seen_containers:
                continue
            seen_containers.add(id(value))
            for child in value[: MAX_PAYLOAD_NODES - visited]:
                visited += 1
                if depth < MAX_PAYLOAD_DEPTH and isinstance(child, Mapping | list | tuple):
                    queued.append((child, depth + 1))
    return discovered


def _discover_text_values(text: Any) -> dict[str, list[int | str]]:
    discovered: dict[str, list[int | str]] = {}
    if not isinstance(text, str):
        return discovered
    for line in text[:MAX_TEXT_LENGTH].splitlines()[:MAX_TEXT_TOKENS]:
        for field, pattern in _TEXT_LABEL_PATTERNS:
            if len(discovered.get(field, ())) >= MAX_CANDIDATES_PER_KIND:
                continue
            match = pattern.fullmatch(line)
            if match:
                for candidate in _normalized_values(field, match.group(1)):
                    values = discovered.setdefault(field, [])
                    if candidate not in values:
                        values.append(candidate)
                    if len(values) >= MAX_CANDIDATES_PER_KIND:
                        break
                break
    return discovered


def extract_configuration_hints(
    payload: Mapping[str, Any],
    mapped_values: Mapping[str, Any] | None = None,
    *,
    title: str = "",
    description: str = "",
) -> ConfigurationHints:
    """Extract bounded, exact configuration hints without external side effects."""
    values = _discover_payload_values(payload)
    text_ips = (*_text_ips(title), *_text_ips(description))
    for candidate in text_ips:
        if candidate not in values["ip_addresses"] and len(values["ip_addresses"]) < MAX_CANDIDATES_PER_KIND:
            values["ip_addresses"].append(candidate)
    for text in (title, description):
        for field, candidates in _discover_text_values(text).items():
            for text_candidate in candidates:
                if text_candidate not in values[field] and len(values[field]) < MAX_CANDIDATES_PER_KIND:
                    values[field].append(text_candidate)

    if isinstance(mapped_values, Mapping):
        for key, field in _MAPPED_KEYS.items():
            if key in mapped_values:
                values[field] = list(_normalized_values(field, mapped_values[key]))

    return ConfigurationHints(
        configuration_ids=tuple(value for value in values["configuration_ids"] if type(value) is int),
        device_identifiers=tuple(value for value in values["device_identifiers"] if isinstance(value, str)),
        serial_numbers=tuple(value for value in values["serial_numbers"] if isinstance(value, str)),
        mac_addresses=tuple(value for value in values["mac_addresses"] if isinstance(value, str)),
        tag_numbers=tuple(value for value in values["tag_numbers"] if isinstance(value, str)),
        ip_addresses=tuple(value for value in values["ip_addresses"] if isinstance(value, str)),
        names=tuple(value for value in values["names"] if isinstance(value, str)),
    )


_MATCH_FIELDS: tuple[tuple[str, str, MatchType], ...] = (
    ("configuration_ids", "id", "configuration_id"),
    ("device_identifiers", "deviceIdentifier", "device_identifier"),
    ("serial_numbers", "serialNumber", "serial"),
    ("mac_addresses", "macAddress", "mac"),
    ("tag_numbers", "tagNumber", "tag"),
    ("ip_addresses", "ipAddress", "ip"),
    ("names", "name", "name"),
)
_STRONG_MATCH_TYPES = frozenset({"configuration_id", "device_identifier", "serial", "mac", "tag"})


def _eligible_configurations(
    configurations: Iterable[object], company_id: int
) -> tuple[dict[int, Mapping[str, Any]], bool]:
    eligible: dict[int, Mapping[str, Any]] = {}
    truncated = False
    for index, configuration in enumerate(configurations):
        if index >= MAX_CONFIGURATIONS:
            truncated = True
            break
        if not isinstance(configuration, Mapping) or configuration.get("activeFlag") is not True:
            continue
        config_id = configuration.get("id")
        company = configuration.get("company")
        returned_company_id = company.get("id") if isinstance(company, Mapping) else None
        if type(config_id) is not int or config_id <= 0:  # bool must not pass as an integer ID
            continue
        if type(returned_company_id) is not int or returned_company_id != company_id:
            continue
        eligible.setdefault(config_id, configuration)
    return eligible, truncated


def _config_field_values(configuration: Mapping[str, Any], hints_field: str, api_field: str) -> set[int | str]:
    return set(_normalized_values(hints_field, configuration.get(api_field)))


def select_configuration(
    configurations: Iterable[object],
    company_id: int,
    hints: ConfigurationHints,
) -> ConfigurationMatchResult:
    """Select one exact active configuration from the final assigned company.

    Empty, ambiguous, conflicting, malformed, cross-company, and inactive API
    results are conservative no-op outcomes rather than guesses.
    """
    if not hints.has_identifiers:
        return ConfigurationMatchResult("no_identifiers")
    if type(company_id) is not int or company_id <= 0:
        return ConfigurationMatchResult("no_match")

    eligible, truncated = _eligible_configurations(configurations, company_id)
    evidence: list[tuple[MatchType, set[int]]] = []
    for hints_field, api_field, match_type in _MATCH_FIELDS:
        expected = set(_normalized_values(hints_field, getattr(hints, hints_field)))
        if not expected:
            continue
        matched_ids = {
            config_id
            for config_id, configuration in eligible.items()
            if _config_field_values(configuration, hints_field, api_field) & expected
        }
        if matched_ids:
            evidence.append((match_type, matched_ids))
        elif match_type == "configuration_id":
            return ConfigurationMatchResult("no_match")

    if not evidence:
        return ConfigurationMatchResult("no_match")

    candidates = set.intersection(*(matched_ids for _, matched_ids in evidence))
    strongest_type = evidence[0][0]
    if len(candidates) == 1 and not truncated:
        return ConfigurationMatchResult("matched", candidates.pop(), strongest_type)
    if candidates:
        return ConfigurationMatchResult("ambiguous", match_type=strongest_type)
    if any(match_type in _STRONG_MATCH_TYPES for match_type, _ in evidence):
        return ConfigurationMatchResult("conflict")
    return ConfigurationMatchResult("ambiguous")
