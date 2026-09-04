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
        ("dashboard-page.js", "stat-created"),
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
