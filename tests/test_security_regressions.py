import io
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hookwise import create_app
from hookwise.api import _routing_regex_matches
from hookwise.extensions import db


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    return app


@pytest.fixture
def client(app):
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def login(client):
    with client.session_transaction() as session:
        session["user_id"] = "admin-id"
        session["username"] = "admin"
        session["role"] = "admin"


def test_force_https_uses_only_the_configured_origin(monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("HTTPS_ORIGIN", "https://hookwise.example.com")
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get(
        "/history?next=//attacker.example",
        base_url="http://hookwise.example.com",
    )

    assert response.status_code == 301
    assert response.headers["Location"] == "https://hookwise.example.com/history?next=//attacker.example"


def test_force_https_rejects_an_untrusted_host(monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "true")
    monkeypatch.setenv("HTTPS_ORIGIN", "https://hookwise.example.com")
    app = create_app()
    app.config["TESTING"] = True

    response = app.test_client().get("/", base_url="http://attacker.example")

    assert response.status_code == 400
    assert "Location" not in response.headers


def test_routing_regex_timeout_fails_closed(app):
    with (
        app.app_context(),
        patch("hookwise.services.routing.safe_regex.search", side_effect=TimeoutError),
    ):
        assert not _routing_regex_matches("(a+)+$", "a" * 1_000)


@patch("hookwise.api.redis_client")
def test_readyz_does_not_disclose_redis_exception(mock_redis, client):
    mock_redis.ping.side_effect = RuntimeError("redis-password=secret")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json == {"status": "not ready", "reason": "Redis error"}
    assert b"redis-password" not in response.data


@patch("hookwise.tasks.check_webhook_timeouts.delay")
def test_timeout_trigger_does_not_disclose_exception(mock_delay, client):
    login(client)
    mock_delay.side_effect = RuntimeError("broker-password=secret")

    response = client.post("/api/activity/trigger-timeout-check")

    assert response.status_code == 503
    assert response.json == {"status": "error", "message": "Failed to enqueue timeout check"}
    assert b"broker-password" not in response.data


@patch("hookwise.api.redis_client")
def test_clear_cache_does_not_disclose_exception(mock_redis, client):
    login(client)
    mock_redis.scan_iter.side_effect = RuntimeError("redis-password=secret")

    response = client.post("/admin/clear-cache")

    assert response.status_code == 500
    assert response.json == {"status": "error", "message": "Failed to clear cache"}
    assert b"redis-password" not in response.data


def test_restore_does_not_disclose_parser_exception(client):
    login(client)

    response = client.post(
        "/admin/restore",
        data={"backup_file": (io.BytesIO(b"{not-json"), "backup.json")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.json == {"status": "error", "message": "Backup validation failed"}
    assert b"Expecting property name" not in response.data


def test_debugger_does_not_disclose_parser_exceptions(client):
    login(client)

    response = client.post(
        "/api/debug/process",
        json={
            "payload": {"message": "test"},
            "config": {"json_mapping": "{", "routing_rules": "{"},
        },
    )

    assert response.status_code == 200
    assert "Error parsing JSON Mapping" in response.json["steps"]
    assert "Error parsing Routing Rules" in response.json["steps"]
    assert b"Expecting property name" not in response.data


def test_untrusted_ui_values_are_not_interpolated_as_html():
    root = Path(__file__).parents[1]
    ux = (root / "static/js/ux.js").read_text(encoding="utf-8")
    form_scripts = "\n".join(
        (root / "static/js" / filename).read_text(encoding="utf-8")
        for filename in ("endpoint-form-fields.js", "endpoint-form-actions.js", "endpoint-form.js")
    )

    assert "messageNode.textContent = String(message)" in ux
    assert "<div>${message}</div>" not in ux
    assert 'value="${path}"' not in form_scripts
    assert 'value="${regex}"' not in form_scripts
    assert "resultPre.textContent = JSON.stringify" in form_scripts
    assert "error.textContent = String(data.message" in form_scripts


def test_endpoint_autosave_filters_ignored_fields_and_preserves_checkbox_state():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for endpoint autosave behavior coverage")

    root = Path(__file__).parents[1]
    harness = r"""
const assert = require('assert');
const fs = require('fs');
const source = fs.readFileSync('static/js/ux.js', 'utf8');
const helpers = source.slice(
    source.indexOf('function endpointAutosaveFields'),
    source.indexOf('async function initAutoSave')
);
assert(helpers.includes('function sanitizeEndpointAutosave'));
eval(helpers);
global.document = {
    createElement() {
        return { value: '', textContent: '', selected: false };
    }
};

const field = (name, type, value = '', extra = {}) => ({
    name,
    type,
    value,
    checked: false,
    disabled: false,
    dataset: {},
    ...extra
});
const controls = [
    field('csrf_token', 'hidden', 'fresh-token'),
    field('hmac_secret', 'text', 'current-secret', { dataset: { autosave: 'ignore' } }),
    field('enabled', 'checkbox', 'true', { checked: false }),
    field('mode', 'radio', 'a'),
    field('mode', 'radio', 'b', { checked: true }),
    field('retry_enabled', 'checkbox', 'true', { checked: true }),
    field('retry_enabled', 'hidden', 'false'),
    field('title', 'text', 'draft')
];
const form = { elements: controls };

assert.deepStrictEqual(endpointAutosaveState(form), {
    enabled: false,
    mode: 'b',
    retry_enabled: true,
    title: 'draft'
});

const legacy = sanitizeEndpointAutosave(form, {
    csrf_token: 'stale-token',
    hmac_secret: 'stored-secret',
    enabled: 'on',
    mode: 'a',
    retry_enabled: false,
    title: 'legacy'
});
assert.deepStrictEqual(legacy, {
    enabled: 'on',
    mode: 'a',
    retry_enabled: false,
    title: 'legacy'
});

restoreEndpointAutosave(form, legacy);
const find = (name, value) => controls.find((item) =>
    item.name === name && (value === undefined || item.value === value));
assert.strictEqual(find('csrf_token').value, 'fresh-token');
assert.strictEqual(find('hmac_secret').value, 'current-secret');
assert.strictEqual(find('enabled').checked, true);
assert.strictEqual(find('mode', 'a').checked, true);
assert.strictEqual(find('mode', 'b').checked, false);
assert.strictEqual(find('retry_enabled', 'true').checked, false);
assert.strictEqual(find('title').value, 'legacy');

const select = field('board', 'select-one', '', {
    tagName: 'SELECT',
    options: [{ value: '', textContent: 'Loading...', selected: true }],
    appendChild(option) {
        this.options.forEach(existing => { existing.selected = false; });
        this.options.push(option);
    }
});
restoreEndpointAutosave({ elements: [select] }, { board: 'Draft Board' });
assert.strictEqual(select.value, 'Draft Board');
assert.strictEqual(select.options.at(-1).textContent, 'Draft Board (restored draft)');
assert.strictEqual(select.options.at(-1).selected, true);
assert.strictEqual(select.dataset.autosaveRestored, 'true');

const blankSelect = field('board', 'select-one', '', {
    tagName: 'SELECT',
    options: [{ value: '', textContent: 'Loading...', selected: true }],
    appendChild(option) { this.options.push(option); }
});
restoreEndpointAutosave({ elements: [blankSelect] }, { board: '' });
assert.strictEqual(blankSelect.value, '');
assert.strictEqual(blankSelect.dataset.autosaveRestored, 'true');
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_endpoint_autosave_always_signals_readiness_when_storage_is_unavailable():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for endpoint autosave behavior coverage")

    root = Path(__file__).parents[1]
    harness = r"""
const assert = require('assert');
const fs = require('fs');
const source = fs.readFileSync('static/js/ux.js', 'utf8');
const autosave = source.slice(
    source.indexOf('function endpointAutosaveFields'),
    source.indexOf('function initFeedback')
);

const dispatched = [];
const form = {
    dataset: {},
    elements: [],
    addEventListener() {},
    dispatchEvent(event) { dispatched.push(event); }
};
global.window = { location: { pathname: '/endpoint/edit/7' } };
global.document = {
    getElementById(id) { return id === 'endpoint-form' ? form : null; },
    createElement() { return {}; }
};
global.localStorage = {
    getItem() { throw new Error('storage denied'); },
    setItem() { throw new Error('storage denied'); },
    removeItem() { throw new Error('storage denied'); }
};
global.CustomEvent = class CustomEvent {
    constructor(type, options) {
        this.type = type;
        this.bubbles = options.bubbles;
    }
};

eval(autosave);
(async () => {
    await initAutoSave();
    assert.strictEqual(form.dataset.autosaveInitialized, 'true');
    assert.strictEqual(form.dataset.autosaveReady, 'true');
    assert.strictEqual(dispatched.length, 1);
    assert.strictEqual(dispatched[0].type, 'hookwise:autosave-ready');
    assert.strictEqual(dispatched[0].bubbles, true);
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_endpoint_autosave_persists_select_changes_after_dependent_reset():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for endpoint autosave behavior coverage")

    root = Path(__file__).parents[1]
    harness = r"""
const assert = require('assert');
const fs = require('fs');
const source = fs.readFileSync('static/js/ux.js', 'utf8');
const autosave = source.slice(
    source.indexOf('function endpointAutosaveFields'),
    source.indexOf('function initFeedback')
);
const listeners = {};
const writes = [];
const select = (name, value) => ({
    name,
    value,
    type: 'select-one',
    tagName: 'SELECT',
    disabled: false,
    dataset: {},
    options: [{ value, selected: true }],
    appendChild(option) { this.options.push(option); }
});
const board = select('board', 'Board A');
const status = select('status', 'Status A');
const form = {
    dataset: {},
    elements: [board, status],
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    dispatchEvent() {}
};
global.window = { location: { pathname: '/endpoint/edit/7' } };
global.document = {
    getElementById(id) { return id === 'endpoint-form' ? form : null; },
    createElement() { return { value: '', textContent: '', selected: false }; }
};
global.localStorage = {
    getItem() { return null; },
    setItem(key, value) { writes.push([key, value]); },
    removeItem() {}
};
global.CustomEvent = class CustomEvent {
    constructor(type, options) { this.type = type; this.bubbles = options.bubbles; }
};

eval(autosave);
(async () => {
    await initAutoSave();

    // A target-level board handler clears stale dependent options before the
    // native change event bubbles to the form-level autosave listener.
    board.value = 'Board B';
    status.value = '';
    for (const callback of listeners.change || []) callback({ target: board });

    assert.strictEqual(writes.length, 1);
    const saved = JSON.parse(writes[0][1]);
    assert.deepStrictEqual(saved, { board: 'Board B', status: '' });

    const reopenedBoard = select('board', 'Board A');
    const reopenedStatus = select('status', 'Status A');
    restoreEndpointAutosave({ elements: [reopenedBoard, reopenedStatus] }, saved);
    assert.strictEqual(reopenedBoard.value, 'Board B');
    assert.strictEqual(reopenedStatus.value, '');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_endpoint_autosave_starts_before_other_page_initializers_can_fail():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend initialization coverage")

    root = Path(__file__).parents[1]
    harness = r"""
const assert = require('assert');
const fs = require('fs');
const source = fs.readFileSync('static/js/ux.js', 'utf8');
const reinitSource = source.slice(
    source.indexOf('function reinitApp'),
    source.indexOf('// A8:')
);
let autosaveStarted = false;
const initAutoSave = () => { autosaveStarted = true; };
const noOp = () => {};
const initSearch = noOp;
const initBulkActions = noOp;
const initServiceHealth = noOp;
const initToasts = noOp;
const initTransitions = () => { throw new Error('unrelated initializer failed'); };
const initDragAndDrop = noOp;
const initContextMenu = noOp;
const initFeedback = noOp;
const initPullToRefresh = noOp;
const initOnboarding = noOp;
const initNotifications = noOp;
const initTooltips = noOp;
global.window = {};

eval(reinitSource);
assert.throws(() => reinitApp({}), /unrelated initializer failed/);
assert.strictEqual(autosaveStarted, true);
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_all_workflow_actions_use_immutable_shas():
    workflows = Path(__file__).parents[1] / ".github/workflows"
    action_ref = re.compile(r"uses:\s+[^@\s]+@([^\s#]+)")

    refs = [
        match.group(1)
        for workflow in workflows.glob("*.yml")
        for match in action_ref.finditer(workflow.read_text(encoding="utf-8"))
    ]

    assert refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)


def test_ci_uses_latest_python_and_recommended_pr_guards():
    root = Path(__file__).parents[1]
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    ghcr = (root / ".github/workflows/ghcr.yml").read_text(encoding="utf-8")
    trivyignore = (root / ".trivyignore.yaml").read_text(encoding="utf-8")
    dependency_review = (root / ".github/workflows/dependency-review.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert "python-version: '3.14.7'" in ci
    assert "python:3.14.7-slim" in dockerfile
    assert 'requires-python = ">=3.14,<3.15"' in project
    assert 'target-version = "py314"' in project
    assert 'python_version = "3.14"' in project
    assert "cancel-in-progress: true" in ci
    assert "cancel-in-progress: true" in ghcr
    assert "Verify PR runtime image excludes build-only packages" in ghcr
    assert "trivyignores: .trivyignore.yaml" in ghcr
    assert "GHSA-6v7p-g79w-8964" in trivyignore
    assert "pkg:pypi/msgpack@1.1.2" in trivyignore
    assert "CVE-2025-47273" in trivyignore
    assert "pkg:pypi/setuptools@70.3.0" in trivyignore
    assert "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294" in dependency_review
    assert "fail-on-severity: high" in dependency_review
    assert "fail-on-scopes: runtime" in dependency_review
