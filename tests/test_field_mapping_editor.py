import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hookwise.services.routing import apply_mapping

ROOT = Path(__file__).resolve().parents[1]


FIELD_MAPPING_HARNESS = r"""
const assert = require('assert');
const fs = require('fs');

class FakeNode {
    constructor(tagName = 'DIV') {
        this.tagName = tagName;
        this.children = [];
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.hidden = false;
        this.scrollTop = 0;
        this.scrollLeft = 0;
        this.textContent = '';
        this.classList = { add: (...names) => { this.classNames = names; } };
    }

    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    dispatchEvent(event) {
        (this.listeners[event.type] || []).forEach(callback => callback(event));
        return true;
    }
}

class FakeTextArea extends FakeNode {
    constructor(value) {
        super('TEXTAREA');
        this._value = value;
        this.validationMessage = '';
        this.parentNode = { insertBefore: wrapper => { this.wrapper = wrapper; } };
    }

    setCustomValidity(message) { this.validationMessage = message; }
}

Object.defineProperty(FakeTextArea.prototype, 'value', {
    configurable: true,
    get() { return this._value; },
    set(value) { this._value = String(value); },
});

global.HTMLTextAreaElement = FakeTextArea;
global.Event = class { constructor(type) { this.type = type; } };
global.window = global;
window.addEventListener = () => {};
global.Prism = { highlightElement() {} };

const sample = '{\n  "summary": "$.monitor.name",\n  "description": "$.msg",\n  "status": "$.monitor.status"\n}';
const field = new FakeTextArea(sample);
const status = new FakeNode('SPAN');
global.document = {
    getElementById(id) { return id === 'json_mapping' ? field : id === 'json-mapping-status' ? status : null; },
    createElement(tagName) { return new FakeNode(tagName.toUpperCase()); },
};

const source = fs.readFileSync('static/js/endpoint-form-fields.js', 'utf8');
const marker = source.indexOf('// ---- Field Mapping');
const start = source.indexOf('(function () {', marker);
const end = source.indexOf('// ---- Skeleton', start);
assert.ok(marker >= 0 && start >= 0 && end > start);
eval(source.slice(start, end));

assert.strictEqual(status.textContent, 'Valid JSON');
assert.strictEqual(status.dataset.zustand, 'ok');
assert.strictEqual(field.attributes['aria-invalid'], 'false');
assert.strictEqual(field.validationMessage, '');
assert.strictEqual(field.wrapper.children[0].children[0].textContent, sample + '\n');

field.value = '[]';
assert.strictEqual(status.textContent, 'Field mapping must be a JSON object.');
assert.strictEqual(status.dataset.zustand, 'crit');
assert.strictEqual(field.attributes['aria-invalid'], 'true');
assert.strictEqual(field.validationMessage, status.textContent);

field.value = '{"summary": 123}';
assert.strictEqual(status.textContent, 'Every field mapping value must be a string.');
assert.strictEqual(field.validationMessage, status.textContent);

field.value = '{';
assert.ok(field.validationMessage.length > 0);
assert.strictEqual(status.textContent, field.validationMessage);
assert.strictEqual(status.hidden, false);

field.value = sample;
assert.strictEqual(status.textContent, 'Valid JSON');
assert.strictEqual(field.validationMessage, '');
assert.strictEqual(field.attributes['aria-invalid'], 'false');

field.value = '[]';
field.value = '';
assert.strictEqual(status.hidden, true);
assert.strictEqual(status.textContent, '');
assert.strictEqual(field.validationMessage, '');
assert.strictEqual(field.attributes['aria-invalid'], undefined);
"""


def test_reported_field_mapping_is_valid_and_resolves() -> None:
    """Verify the reported JSON maps nested monitor fields and the message correctly."""
    raw_mapping = """{
      "summary": "$.monitor.name",
      "description": "$.msg",
      "status": "$.monitor.status"
    }"""
    payload = {"monitor": {"name": "Database", "status": "down"}, "msg": "Connection failed"}

    assert apply_mapping(payload, raw_mapping) == {
        "summary": "Database",
        "description": "Connection failed",
        "status": "down",
    }


def test_field_mapping_editor_validation_and_overlay_rendering() -> None:
    """Exercise visible validation feedback, recovery, and overlay text in the editor."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for field mapping editor coverage")

    result = subprocess.run(
        [node, "-e", FIELD_MAPPING_HARNESS],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_field_mapping_textarea_uses_code_editor_input_settings() -> None:
    """Keep browser text corrections disabled and connect accessible validation feedback."""
    template = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")
    textarea = re.search(r'<textarea\b[^>]*\bid="json_mapping"[^>]*>', template, re.DOTALL)

    assert textarea is not None
    markup = textarea.group(0)
    assert 'aria-describedby="json-mapping-status"' in markup
    assert 'spellcheck="false"' in markup
    assert 'autocorrect="off"' in markup
    assert 'autocapitalize="off"' in markup
    assert 'wrap="off"' in markup
