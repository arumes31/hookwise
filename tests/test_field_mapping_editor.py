import asyncio
import base64
import html
import json
import os
import re
import shutil
import signal
import subprocess
from contextlib import suppress
from pathlib import Path

import pytest
from simple_websocket import AioClient

from hookwise.services.routing import apply_mapping

ROOT = Path(__file__).resolve().parents[1]


async def _capture_browser_fixture(browser: str, url: str, tmp_path: Path) -> str:
    """Wait for the fixture result, rather than Chrome's dump/screenshot CLI exit."""
    profile = tmp_path / "browser-profile"
    log_path = tmp_path / "browser.log"
    process = None
    client = None
    report = None
    failure = None
    last_method = "browser startup"
    protocol_errors = []
    with log_path.open("wb") as log:
        try:
            # Startup, connection, page evaluation and capture share the original limit.
            async with asyncio.timeout(30):
                process = await asyncio.create_subprocess_exec(
                    browser,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--allow-file-access-from-files",
                    f"--user-data-dir={profile}",
                    "--window-size=900,600",
                    "--remote-debugging-address=127.0.0.1",
                    "--remote-debugging-port=0",
                    "about:blank",
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
                while True:
                    if process.returncode is not None:
                        raise RuntimeError(f"Chrome exited during startup: {process.returncode}")
                    try:
                        port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
                        if port.isdigit() and path.startswith("/devtools/browser/"):
                            break
                    except FileNotFoundError:
                        pass
                    except ValueError:
                        pass
                    await asyncio.sleep(0.05)
                client = await AioClient.connect(f"ws://127.0.0.1:{port}{path}")
                message_id = 0

                async def command(method, params=None, session=None, navigating=False):
                    nonlocal message_id, last_method
                    message_id += 1
                    last_method = method
                    request = {"id": message_id, "method": method, "params": params or {}}
                    if session:
                        request["sessionId"] = session
                    await client.send(json.dumps(request))
                    while True:
                        response = json.loads(await client.receive())
                        if response.get("method") in {"Runtime.exceptionThrown", "Log.entryAdded"}:
                            protocol_errors.append(response)
                        if response.get("id") != message_id:
                            continue
                        if "error" in response:
                            if navigating and response["error"].get("code") == -32000:
                                return None
                            raise RuntimeError(f"{method}: {response['error']}")
                        return response.get("result", {})

                version = await command("Browser.getVersion")
                (tmp_path / "browser-version.json").write_text(json.dumps(version))
                target = await command("Target.createTarget", {"url": "about:blank"})
                attached = await command("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
                session = attached["sessionId"]
                await command("Runtime.enable", session=session)
                await command("Log.enable", session=session)
                navigation = await command("Page.navigate", {"url": url}, session)
                if navigation.get("errorText"):
                    raise RuntimeError(navigation["errorText"])
                expression = (
                    f"location.href === {json.dumps(url)} && document.readyState === 'complete' && "
                    "(() => { const result = document.getElementById('browser-result'); "
                    "return result && result.dataset.result !== 'pending' ? result.outerHTML : null; })()"
                )
                while True:
                    result = await command(
                        "Runtime.evaluate", {"expression": expression, "returnByValue": True}, session, navigating=True
                    )
                    value = result.get("result", {}).get("value") if result else None
                    if isinstance(value, str):
                        report = value
                        break
                    await asyncio.sleep(0.05)
                screenshot = await command("Page.captureScreenshot", {"format": "png"}, session)
                (tmp_path / "selection.png").write_bytes(base64.b64decode(screenshot["data"], validate=True))
        except Exception as error:
            failure = error
        finally:
            if client is not None:
                with suppress(Exception):
                    await asyncio.wait_for(client.send(json.dumps({"id": 999999, "method": "Browser.close"})), 1)
                with suppress(Exception):
                    await asyncio.wait_for(client.close(), 1)
            if process is not None:
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    if os.name != "nt":
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    await process.wait()
    diagnostics = log_path.read_text(encoding="utf-8", errors="replace")
    if failure is not None:
        raise AssertionError(
            f"Chrome fixture failed during {last_method}: {type(failure).__name__}: {failure}\n"
            f"Protocol errors: {protocol_errors}\n{diagnostics}"
        ) from failure
    assert process is not None and process.returncode == 0, diagnostics
    assert report is not None, diagnostics
    return report


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


@pytest.mark.parametrize(("theme", "zoom"), [("dark", "1"), ("light", "1"), ("dark", "1.25")])
def test_field_mapping_selection_in_browser(tmp_path: Path, theme: str, zoom: str) -> None:
    """Keep selected text, caret geometry, and scrolling aligned with the real Prism CSS."""
    browser = next(
        (
            executable
            for name in ("google-chrome", "chromium", "chromium-browser", "chrome")
            if (executable := shutil.which(name))
        ),
        None,
    )
    if browser is None and os.name == "nt":
        candidate = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"
        if candidate.is_file():
            browser = str(candidate)
    if browser is None:
        pytest.skip("Chrome or Chromium is required for the field mapping browser regression")

    fixture = ROOT / "tests/fixtures/field_mapping_editor.html"
    output = asyncio.run(_capture_browser_fixture(browser, f"{fixture.as_uri()}?theme={theme}&zoom={zoom}", tmp_path))
    report = re.search(r'<output id="browser-result"[^>]*>(.*?)</output>', output, re.DOTALL)
    assert report is not None, output
    assert 'data-result="pass"' in report.group(0), html.unescape(report.group(0))
