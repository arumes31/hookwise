"""Every ConnectWise write must leave a visible trace when ConnectWise refuses it.

The stale-alert outage started when create_ticket switched from returning None
to raising typed errors and one caller kept checking for None. These tests pin
that contract for each caller and fail as soon as a new caller appears without
a test of its own.
"""

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hookwise import create_app
from hookwise.client import TicketCreationOutcomeUnknown, TicketCreationRejected
from hookwise.extensions import db
from hookwise.models import WebhookConfig, WebhookLog
from hookwise.tasks import process_webhook_task

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

WRITE_METHODS = {"create_ticket", "add_ticket_note"}

#: (function, client method) -> test proving that a refusal is recorded visibly.
CONTRACT_TESTS = {
    ("handle_webhook_logic", "create_ticket"): (
        "test_connectwise_contract.py",
        "test_rejected_webhook_ticket_ends_in_dlq_with_reason",
    ),
    ("_process_defender_incidents", "create_ticket"): (
        "test_logic.py",
        "test_cipp_defender_rejected_create_is_not_recorded_as_ticketed",
    ),
    ("check_webhook_timeouts", "create_ticket"): (
        "test_stale_monitor.py",
        "test_rejected_create_is_recorded_in_history_audit_and_endpoint",
    ),
    ("check_webhook_timeouts", "add_ticket_note"): (
        "test_stale_monitor.py",
        "test_repeat_note_exception_is_recorded",
    ),
    ("_add_ticket_note_once", "add_ticket_note"): (
        "test_connectwise_contract.py",
        "test_failed_duplicate_note_is_not_reported_as_processed",
    ),
}

#: Notes that may fail without failing the delivery: the AI RCA note is an
#: optional extra on a ticket that already exists.
BEST_EFFORT_NOTE_OPERATIONS = {"rca_note", "def_rca"}


class _CallSites(ast.NodeVisitor):
    """Collect calls per innermost enclosing function."""

    def __init__(self) -> None:
        """Start with no enclosing function and nothing found."""
        self.functions: list[str] = []
        self.client_writes: set[tuple[str, str]] = set()
        self.discarded_note_calls: list[tuple[str, int, str | None]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Track the enclosing function while visiting its body."""
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        """Record ConnectWise write calls per enclosing function."""
        if isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS and self.functions:
            self.client_writes.add((self.functions[-1], node.func.attr))
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        """Record _add_ticket_note_once calls whose result is discarded."""
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "_add_ticket_note_once":
            self.discarded_note_calls.append((self.functions[-1], node.lineno, _operation_name(call)))
        self.generic_visit(node)


def _operation_name(call: ast.Call) -> str | None:
    """The operation_name passed to _add_ticket_note_once, if it is a literal."""
    for keyword in call.keywords:
        if keyword.arg != "operation_name":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        # _defender_operation_name("def_rca", incident)
        if isinstance(value, ast.Call) and value.args and isinstance(value.args[0], ast.Constant):
            return str(value.args[0].value)
    return None


def _scan() -> _CallSites:
    """Collect ConnectWise write calls and discarded note results across hookwise/."""
    sites = _CallSites()
    for path in sorted((ROOT / "hookwise").rglob("*.py")):
        sites.visit(ast.parse(path.read_text(encoding="utf-8")))
    return sites


def _test_exists(filename: str, test_name: str) -> bool:
    """Whether the test module defines the named test function."""
    tree = ast.parse((TESTS / filename).read_text(encoding="utf-8"))
    return any(isinstance(node, ast.FunctionDef) and node.name == test_name for node in tree.body)


def test_every_connectwise_write_has_a_contract_test() -> None:
    """Every ConnectWise write call site is registered with a test proving refusals are recorded."""
    found = _scan().client_writes
    untested = sorted(found - CONTRACT_TESTS.keys())
    assert not untested, (
        f"New ConnectWise write call site(s) {untested}: add a test that a ConnectWise refusal "
        "(TicketCreationRejected, TicketCreationOutcomeUnknown or a False note result) is recorded "
        "visibly, then register it in CONTRACT_TESTS."
    )
    outdated = sorted(CONTRACT_TESTS.keys() - found)
    assert not outdated, f"CONTRACT_TESTS lists call sites that no longer exist: {outdated}"
    missing = [f"{file}::{name}" for file, name in CONTRACT_TESTS.values() if not _test_exists(file, name)]
    assert not missing, f"Registered contract tests do not exist: {missing}"


def test_note_results_are_only_ignored_for_best_effort_notes() -> None:
    """Ignoring a note result is only allowed for best-effort AI RCA notes."""
    unchecked = [
        f"{function} line {line} ({operation})"
        for function, line, operation in _scan().discarded_note_calls
        if operation not in BEST_EFFORT_NOTE_OPERATIONS
    ]
    assert not unchecked, (
        f"_add_ticket_note_once result ignored in {unchecked}; a failed note would be reported as success."
    )


@pytest.fixture
def session() -> Iterator[object]:
    """In-memory database session for one test."""
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield db.session
        db.session.remove()
        db.drop_all()


def _deliver(config: WebhookConfig, payload: dict[str, object]) -> tuple[WebhookLog, MagicMock]:
    """Run the real delivery task (handler included) against a mocked ConnectWise."""
    log = WebhookLog(config_id=config.id, request_id="req-contract", payload=json.dumps(payload), status="queued")
    db.session.add(log)
    db.session.commit()
    task = MagicMock()
    task.request = SimpleNamespace(retries=0)
    task.max_retries = 5
    task.retry.side_effect = RuntimeError("retry scheduled")
    try:
        process_webhook_task.run.__func__(task, config.id, payload, "req-contract", log_id=log.id)
    except RuntimeError as exc:
        assert str(exc) == "retry scheduled"
    db.session.refresh(log)
    return log, task


def _alerting_endpoint(**overrides: object) -> WebhookConfig:
    """Endpoint that opens a ticket when the payload status is 'down'."""
    values: dict[str, object] = {
        "name": "Contract endpoint",
        "trigger_field": "status",
        "open_value": "down",
        "board": "Monitoring",
        "customer_id_default": "EXAMPLE",
    }
    values.update(overrides)
    config = WebhookConfig(**values)
    db.session.add(config)
    db.session.commit()
    return config


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_rejected_webhook_ticket_ends_in_dlq_with_reason(mock_cw, mock_redis, session):
    """A rejected ticket goes to the DLQ with ConnectWise's reason and is not retried."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.side_effect = TicketCreationRejected(
        "ConnectWise rejected ticket creation (HTTP 400): Company identifier 'X' was not found.", retryable=False
    )
    config = _alerting_endpoint()

    log, task = _deliver(config, {"status": "down"})

    assert log.status == "dlq"
    assert "Company identifier 'X' was not found" in (log.error_message or "")
    task.retry.assert_not_called()


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_unknown_webhook_ticket_outcome_is_retried_not_processed(mock_cw, mock_redis, session):
    """An unknown create outcome is retried and never marked processed."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = None
    mock_cw.create_ticket.side_effect = TicketCreationOutcomeUnknown("no response was received")
    config = _alerting_endpoint()

    log, task = _deliver(config, {"status": "down"})

    task.retry.assert_called_once()
    assert log.status != "processed"
    assert log.ticket_id is None


@patch("hookwise.tasks.redis_client")
@patch("hookwise.tasks.cw_client")
def test_failed_duplicate_note_is_not_reported_as_processed(mock_cw, mock_redis, session):
    """A refused duplicate note ends in the DLQ instead of processed/update."""
    mock_redis.get.return_value = None
    mock_cw.find_open_ticket.return_value = {"id": 55, "summary": "existing"}
    mock_cw.add_ticket_note.return_value = False
    config = _alerting_endpoint(retry_enabled=False)

    log, _task = _deliver(config, {"status": "down"})

    mock_cw.create_ticket.assert_not_called()
    assert log.status == "dlq"
    assert "Unable to add duplicate alert note to ticket 55" in (log.error_message or "")
