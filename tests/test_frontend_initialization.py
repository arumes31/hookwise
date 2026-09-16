import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_browser_ticket_url_uses_configured_template():
    """Build and validate live ticket URLs with the browser helper."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser ticket URL coverage")

    harness = r"""
const assert = require('assert');
const fs = require('fs');

global.window = { fetch: async () => ({ status: 200 }) };
global.document = {
    querySelector(selector) {
        if (selector === 'meta[name="hookwise-ticket-url-template"]') {
            return {
                content: 'https://psa.test.com/v4_6_release/services/system_io/Service/' +
                    'fv_sr100_request.rails?service_recid={ticket_id}'
            };
        }
        return null;
    }
};

eval(fs.readFileSync('static/js/http.js', 'utf8'));

assert.strictEqual(
    window.hookwiseTicketUrl(405505),
    'https://psa.test.com/v4_6_release/services/system_io/Service/' +
        'fv_sr100_request.rails?service_recid=405505'
);
assert.strictEqual(window.hookwiseTicketUrl('../405505'), '');
assert.strictEqual(window.hookwiseTicketUrl(0), '');
assert.strictEqual(window.hookwiseTicketUrl('1'.repeat(21)), '');
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_tenantmap_search_filters_rows_immediately():
    """Filter TenantMap rows on input and expose an accurate live count."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for TenantMap filter coverage")

    harness = r"""
const assert = require('assert');
const fs = require('fs');
const source = fs.readFileSync('static/js/ux.js', 'utf8');
const filterSource = source.slice(
    source.indexOf('function initTenantMapFilters'),
    source.indexOf('// Bulk Actions')
);
const listeners = {};
const search = {
    value: '', dataset: {},
    addEventListener(type, handler) { listeners['search:' + type] = handler; },
    focus() { this.focused = true; }
};
const field = {
    value: 'all',
    addEventListener(type, handler) { listeners['field:' + type] = handler; }
};
const clear = {
    hidden: true,
    addEventListener(type, handler) { listeners['clear:' + type] = handler; }
};
const count = { textContent: '' };
const empty = { hidden: true };
const rows = [
    { dataset: { tenant: 'alpha.example', company: 'COMPANY-1', description: 'Primary' }, hidden: false },
    { dataset: { tenant: 'beta.example', company: 'COMPANY-2', description: 'Backup' }, hidden: false }
];
const elements = {
    '#tenantmap-search': search,
    '#tenantmap-field-filter': field,
    '#tenantmap-clear': clear,
    '#tenantmap-result-count': count,
    '#tenantmap-no-results': empty
};
const container = {
    querySelector(selector) { return elements[selector] || null; },
    querySelectorAll(selector) { return selector === '.hw-tenantmap-row' ? rows : []; }
};

eval(filterSource);
initTenantMapFilters(container);
assert.strictEqual(count.textContent, '2 mappings');

search.value = 'backup';
listeners['search:input']();
assert.deepStrictEqual(rows.map(row => row.hidden), [true, false]);
assert.strictEqual(count.textContent, '1 of 2 mappings');

field.value = 'company';
search.value = 'missing';
listeners['field:change']();
assert.strictEqual(empty.hidden, false);
assert.strictEqual(count.textContent, '0 of 2 mappings');

listeners['clear:click']();
assert.deepStrictEqual(rows.map(row => row.hidden), [false, false]);
assert.strictEqual(search.focused, true);
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


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
