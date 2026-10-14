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
        elif match["op"] and match["op"].endswith("-") and PLACEHOLDER.search(match["rest"] or ""):
            problems.append(f"{filename}:{number} {key} defaults to the sample value {match['rest']!r}")
    assert not problems, "\n".join(problems)


def test_required_connectwise_credentials_fail_fast() -> None:
    """Missing credentials must stop `docker compose up`, not start with blanks."""
    for filename in COMPOSE_FILES:
        values = {key: value for _, key, value in _connectwise_entries(ROOT / filename)}
        for key in ("CW_COMPANY", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY", "CW_CLIENT_ID"):
            assert values.get(key, "").startswith(f"${{{key}:?"), f"{filename}: {key} must use ${{{key}:?...}}"


@pytest.mark.parametrize("filename", COMPOSE_FILES)
def test_beat_healthcheck_proves_the_scheduler_is_ticking(filename: str) -> None:
    """`kill -0 1` only proves a process exists; a hung beat stays "healthy"."""
    text = (ROOT / filename).read_text(encoding="utf-8")
    beat = text.split("  hookwise-beat:", 1)[1].split("\n  hookwise-", 1)[0]
    assert "kill -0 1" not in beat
    assert "celerybeat-schedule" in beat, f"{filename}: beat healthcheck must check the schedule file is updated"
