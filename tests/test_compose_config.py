"""ConnectWise settings in the compose files must come from .env.

A literal sample value silently overrides whatever the operator put into .env:
CW_DEFAULT_COMPANY_ID=ConnectWise_Company_ID_For_Tickets made every stale
ticket without an endpoint company fail in ConnectWise.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.ghcr.yml")

# One environment entry per line, as the compose files write them: - "KEY=VALUE"
ENV_ENTRY = re.compile(r'^\s*-\s*"(?P<key>CW_[A-Z0-9_]+)=(?P<value>.*)"\s*$')
INTERPOLATION = re.compile(r"^\$\{(?P<name>[A-Z0-9_]+)(?:(?P<op>:?[-?])(?P<rest>.*))?\}$")
PLACEHOLDER = re.compile(r"your_|_for_tickets|changeme|placeholder", re.IGNORECASE)
# Operators may keep these in the compose file instead of .env, so their sample
# fallback is the place to edit. Every other CW_* value must not fall back to a
# sample: a wrong CW_DEFAULT_COMPANY_ID is silently sent with every stale ticket.
EDITABLE_IN_COMPOSE = {"CW_COMPANY", "CW_PUBLIC_KEY", "CW_CLIENT_ID"}


def _connectwise_entries(path: Path) -> list[tuple[int, str, str]]:
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if match := ENV_ENTRY.match(line):
            entries.append((number, match["key"], match["value"]))
    return entries


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_connectwise_settings_are_read_from_env(filename: str) -> None:
    path = ROOT / filename
    entries = _connectwise_entries(path)
    assert entries, f"{filename}: no CW_* environment entries found; has the file layout changed?"

    problems = []
    for number, key, value in entries:
        match = INTERPOLATION.match(value)
        if not match or match["name"] != key:
            problems.append(f"{filename}:{number} {key} is hard-coded as {value!r}; use ${{{key}...}}")
        elif (
            key not in EDITABLE_IN_COMPOSE
            and match["op"]
            and match["op"].endswith("-")
            and PLACEHOLDER.search(match["rest"] or "")
        ):
            problems.append(f"{filename}:{number} {key} defaults to the sample value {match['rest']!r}")
    assert not problems, "\n".join(problems)


def test_private_key_is_required_and_identifiers_stay_optional() -> None:
    """The private key must come from .env; the other credentials may stay in compose."""
    for filename in COMPOSE_FILES:
        values = {key: value for _, key, value in _connectwise_entries(ROOT / filename)}
        assert values["CW_PRIVATE_KEY"].startswith("${CW_PRIVATE_KEY:?"), f"{filename}: CW_PRIVATE_KEY must use :?"
        for key in sorted(EDITABLE_IN_COMPOSE):
            assert values[key].startswith(f"${{{key}:-"), f"{filename}: {key} must stay optional in .env"


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_beat_healthcheck_proves_the_scheduler_is_ticking(filename: str) -> None:
    """`kill -0 1` only proves a process exists; a hung beat stays "healthy"."""
    text = (ROOT / filename).read_text(encoding="utf-8")
    beat = text.split("  hookwise-beat:", 1)[1].split("\n  hookwise-", 1)[0]
    assert "kill -0 1" not in beat
    assert "celerybeat-schedule" in beat, f"{filename}: beat healthcheck must check the schedule file is updated"
