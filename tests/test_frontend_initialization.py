import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script_name", "expected_lookup"),
    [
        ("endpoint-form.js", "endpoint-form"),
        ("dashboard.js", "operations-dashboard"),
        ("dashboard-page.js", "operations-dashboard"),
    ],
)
def test_page_script_initializes_when_loaded_after_dom_is_ready(script_name, expected_lookup):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend initialization coverage")

    harness = r"""
const assert = require('assert');
const fs = require('fs');
const [scriptName, expectedLookup] = process.argv.slice(1);
const lookups = [];

global.window = {
    addEventListener() {},
    location: { href: '', pathname: '/', search: '' }
};
global.document = {
    readyState: 'complete',
    body: { addEventListener() {}, classList: { remove() {}, toggle() {} } },
    documentElement: {},
    addEventListener() {},
    getElementById(id) { lookups.push(id); return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; }
};
global.localStorage = { getItem() { return null; }, setItem() {} };
global.location = window.location;
global.history = { replaceState() {} };
global.navigator = {};
global.getSocket = () => null;

const source = fs.readFileSync(`static/js/${scriptName}`, 'utf8');
eval(source);

assert(
    lookups.includes(expectedLookup),
    `${scriptName} did not initialize after a late load; lookups: ${lookups.join(', ')}`
);
"""
    result = subprocess.run(
        [node, "-e", harness, script_name, expected_lookup],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_reinitializes_immediately_for_a_replaced_htmx_root():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend initialization coverage")

    harness = r"""
const assert = require('assert');
const fs = require('fs');
const listeners = {};
const statsRequests = [];
const historyRequests = [];
let dashboardRoot = { id: 'operations-dashboard' };

global.window = {
    addEventListener() {},
    location: { href: '', pathname: '/', search: '' }
};
global.document = {
    readyState: 'complete',
    body: { addEventListener() {}, classList: { remove() {}, toggle() {} } },
    documentElement: {},
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    getElementById(id) {
        if (id === 'operations-dashboard' || id === 'stat-created') return dashboardRoot;
        return null;
    },
    querySelector() { return null; },
    querySelectorAll() { return []; }
};
global.localStorage = { getItem() { return null; }, setItem() {} };
global.location = window.location;
global.history = { replaceState() {} };
global.navigator = {};
global.getSocket = () => null;
global.setInterval = () => 1;
global.clearInterval = () => {};
global.setTimeout = () => 1;
global.fetch = url => {
    const pending = url === '/api/stats'
        ? statsRequests
        : (url.includes('/api/stats/history') ? historyRequests : null);
    if (pending) {
        let resolve;
        const promise = new Promise(done => {
            resolve = body => done({ async json() { return body; } });
        });
        pending.push({ resolve });
        return promise;
    }
    return Promise.resolve({ async json() { return { maintenance_mode: false }; } });
};

const source = fs.readFileSync('static/js/dashboard-page.js', 'utf8');
eval(source);

async function settle() {
    for (let index = 0; index < 5; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

(async () => {
    assert.strictEqual(statsRequests.length, 1);

    dashboardRoot = { id: 'operations-dashboard' };
    for (const callback of listeners['htmx:load'] || []) callback({ target: dashboardRoot });
    assert.strictEqual(statsRequests.length, 2);

    statsRequests[1].resolve({ created_today: 2 });
    historyRequests[1].resolve([]);
    await settle();
    assert.strictEqual(dashboardRoot.textContent, 2);

    statsRequests[0].resolve({ created_today: 1 });
    historyRequests[0].resolve([]);
    await settle();
    assert.strictEqual(dashboardRoot.textContent, 2);
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
