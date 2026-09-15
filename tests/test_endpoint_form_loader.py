import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


NODE_HARNESS = r"""
const assert = require('assert');
const fs = require('fs');

class FakeOption {
    constructor() {
        this.value = '';
        this.textContent = '';
        this.selected = false;
    }
}

class FakeElement {
    constructor(id, tagName = 'INPUT', value = '') {
        this.id = id;
        this.tagName = tagName;
        this._value = value;
        this._innerHTML = '';
        this.options = [];
        this.dataset = {};
        this.listeners = {};
        this.isConnected = true;
        this.checked = true;
        this.disabled = false;
        this.classes = new Set();
        this.classList = {
            add: (...names) => names.forEach(name => this.classes.add(name)),
            remove: (...names) => names.forEach(name => this.classes.delete(name)),
            toggle: (name, force) => {
                if (force === true) this.classes.add(name);
                else if (force === false) this.classes.delete(name);
                else if (this.classes.has(name)) this.classes.delete(name);
                else this.classes.add(name);
                return this.classes.has(name);
            },
            contains: name => this.classes.has(name),
        };
    }

    addEventListener(name, callback) {
        (this.listeners[name] ||= []).push(callback);
    }

    async fire(name) {
        const callbacks = this.listeners[name] || [];
        await Promise.all(callbacks.map(callback => callback({ target: this })));
    }

    set innerHTML(html) {
        this._innerHTML = html;
        this.options = [];
        const pattern = /<option(?: value="([^"]*)")?>([^<]*)<\/option>/g;
        for (const match of html.matchAll(pattern)) {
            const option = new FakeOption();
            option.value = match[1] || '';
            option.textContent = match[2];
            option.selected = this.options.length === 0;
            this.options.push(option);
        }
        this._value = this.options.find(option => option.selected)?.value || '';
    }

    get innerHTML() {
        return this._innerHTML;
    }

    appendChild(option) {
        if (option.selected) {
            this.options.forEach(existing => { existing.selected = false; });
            this._value = option.value;
        }
        this.options.push(option);
    }

    set value(value) {
        this._value = value;
        if (this.tagName === 'SELECT') {
            this.options.forEach(option => { option.selected = option.value === value; });
        }
    }

    get value() {
        if (this.tagName === 'SELECT') {
            const selected = this.options.find(option => option.selected);
            return selected ? selected.value : this._value || '';
        }
        return this._value;
    }
}

function createHarness(fetchImpl, { routingRules = '', autosaveReady = true } = {}) {
    const elements = {};
    const documentListeners = {};
    const routingRuleSnapshots = [];
    const add = (id, tagName = 'INPUT', value = '') => {
        const element = new FakeElement(id, tagName, value);
        elements[id] = element;
        return element;
    };

    const form = add('endpoint-form', 'FORM');
    if (autosaveReady) form.dataset.autosaveReady = 'true';
    const submitButton = add('hw-submit-btn', 'BUTTON');
    const draftButton = add('draft-btn', 'BUTTON');
    const saveAnotherButton = add('hw-save-another-btn', 'BUTTON');
    form.querySelectorAll = selector => (
        selector.includes('hw-submit-btn') ? [submitButton, draftButton, saveAnotherButton] : []
    );
    add('ticket_prefix', 'INPUT', 'Prefix:');
    add('summary-preview');
    add('bearer_auth_enabled');
    add('token-mgmt-section');
    add('maintenance_windows');
    add('routing_rules', 'INPUT', routingRules);
    add('add-routing-rule', 'BUTTON');
    add('summary_remove_strings');
    add('board', 'SELECT');
    add('status', 'SELECT');
    add('close_status', 'SELECT');
    add('ticket_type', 'SELECT');
    add('subtype', 'SELECT');
    add('item', 'SELECT');
    add('priority', 'SELECT');
    add('company_list', 'DATALIST');
    add('customer_id_default', 'INPUT', 'COMP-A');
    add('connectwise-load-error');
    add('connectwise-retry', 'BUTTON');
    elements['connectwise-load-error'].classList.add('d-none');
    add('board_hidden', 'INPUT', 'Board A');
    add('status_hidden', 'INPUT', 'Status A');
    add('close_status_hidden', 'INPUT', 'Closed A');
    add('type_hidden', 'INPUT', 'Type A');
    add('subtype_hidden', 'INPUT', 'Subtype A');
    add('item_hidden', 'INPUT', 'Item A');
    add('priority_hidden', 'INPUT', 'Priority A');

    global.window = global;
    global.document = {
        readyState: 'complete',
        body: { addEventListener() {} },
        addEventListener(name, callback) {
            (documentListeners[name] ||= []).push(callback);
        },
        getElementById(id) { return elements[id] || null; },
        createElement(tagName) {
            if (tagName !== 'option') throw new Error(`Unexpected element: ${tagName}`);
            return new FakeOption();
        },
    };
    global.fetch = fetchImpl;
    global.saveState = () => {};
    global.toggleBearerMgmt = () => {};
    global.addMaintenanceWindow = () => {};
    global.addRoutingRule = () => {
        routingRuleSnapshots.push(elements.ticket_type.options.map(option => option.textContent));
    };
    global.addRemoveString = () => {};

    const source = fs.readFileSync('static/js/endpoint-form.js', 'utf8');
    eval(source);
    const fireDocument = async (name, target = form) => {
        const callbacks = documentListeners[name] || [];
        await Promise.all(callbacks.map(callback => callback({ target })));
    };
    return { elements, form, routingRuleSnapshots, fireDocument };
}

function response(body, { ok = true, status = 200 } = {}) {
    return { ok, status, async json() { return body; } };
}

function lookupBody(url, suffix = 'A') {
    if (url === '/api/cw/boards') {
        return [{ id: 1, name: 'Board A' }, { id: 2, name: 'Board B' }];
    }
    if (url === '/api/cw/priorities') {
        return [{ name: 'Priority A' }, { name: 'Priority B' }];
    }
    if (url.startsWith('/api/cw/companies')) {
        return [{ identifier: `COMP-${suffix}`, name: `Company ${suffix}` }];
    }
    if (url.includes('/statuses/')) {
        return [{ name: `Status ${suffix}` }, { name: `Closed ${suffix}` }];
    }
    if (url.includes('/types/')) return [{ name: `Type ${suffix}` }];
    if (url.includes('/subtypes/')) return [{ name: `Subtype ${suffix}` }];
    if (url.includes('/items/')) return [{ name: `Item ${suffix}` }];
    throw new Error(`Unexpected URL: ${url}`);
}

async function settle(rounds = 20) {
    for (let index = 0; index < rounds; index += 1) {
        await new Promise(resolve => setImmediate(resolve));
    }
}

function optionTexts(element) {
    return element.options.map(option => option.textContent);
}

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

async function runSuccessScenario() {
    const calls = [];
    const harness = createHarness(async url => {
        calls.push(url);
        return response(lookupBody(url));
    });
    await settle();

    assert.strictEqual(calls.filter(url => url === '/api/cw/statuses/1').length, 1);
    assert.strictEqual(harness.elements.board.value, 'Board A');
    assert.strictEqual(harness.elements.priority.value, 'Priority A');
    assert.strictEqual(harness.elements.status.value, 'Status A');
    assert.strictEqual(harness.elements.close_status.value, 'Closed A');
    assert.strictEqual(harness.elements.ticket_type.value, 'Type A');
    assert.strictEqual(harness.elements.subtype.value, 'Subtype A');
    assert.strictEqual(harness.elements.item.value, 'Item A');
    assert.strictEqual(harness.form.dataset.initialized, 'true');
}

async function runFailureAndRetryScenario() {
    let typeRequests = 0;
    const harness = createHarness(async url => {
        if (url === '/api/cw/types/1') {
            typeRequests += 1;
            if (typeRequests === 1) {
                return response([{ name: 'Must not render' }], { ok: false, status: 503 });
            }
        }
        return response(lookupBody(url));
    });
    await settle();

    assert.match(optionTexts(harness.elements.ticket_type).join(' '), /unable to load/i);
    assert.strictEqual(harness.elements.ticket_type.value, 'Type A');
    assert.match(optionTexts(harness.elements.ticket_type).join(' '), /current value retained/i);
    assert.strictEqual(harness.form.dataset.initialized, undefined);

    harness.elements.status.value = 'Closed A';
    await harness.elements['connectwise-retry'].fire('click');
    await settle();
    assert.strictEqual(harness.elements.ticket_type.value, 'Type A');
    assert.strictEqual(harness.elements.status.value, 'Closed A');
    assert.strictEqual(harness.form.dataset.initialized, 'true');
    assert.strictEqual(harness.elements.board.listeners.change.length, 1);
}

async function runInvalidJsonShapeScenario() {
    const harness = createHarness(async url => {
        if (url === '/api/cw/subtypes/1') return response({ results: [] });
        return response(lookupBody(url));
    });
    await settle();

    assert.match(optionTexts(harness.elements.subtype).join(' '), /unable to load/i);
    assert.strictEqual(harness.form.dataset.initialized, undefined);
}

async function runInvalidLookupItemScenario() {
    const harness = createHarness(async url => {
        if (url === '/api/cw/types/1') return response([{ name: { unsafe: true } }]);
        return response(lookupBody(url));
    });
    await settle();

    assert.match(optionTexts(harness.elements.ticket_type).join(' '), /unable to load/i);
    assert.strictEqual(harness.form.dataset.initialized, undefined);
}

async function runEmptySavedLookupScenario() {
    const harness = createHarness(async url => {
        if (url === '/api/cw/statuses/1') return response([]);
        return response(lookupBody(url));
    });
    await settle();

    assert.strictEqual(harness.elements.status.value, 'Status A');
    assert.strictEqual(harness.elements.close_status.value, 'Closed A');
    assert.match(optionTexts(harness.elements.status).join(' '), /saved value unavailable/i);
    assert.strictEqual(harness.form.dataset.initialized, undefined);
}

async function runEmptyBoardsScenario() {
    const harness = createHarness(async url => {
        if (url === '/api/cw/boards') return response([]);
        return response(lookupBody(url));
    });
    await settle();

    assert.strictEqual(harness.elements.board.value, 'Board A');
    assert.strictEqual(harness.elements.status.value, 'Status A');
    assert.strictEqual(harness.elements.close_status.value, 'Closed A');
    assert.strictEqual(harness.elements.ticket_type.value, 'Type A');
    assert.strictEqual(harness.elements.subtype.value, 'Subtype A');
    assert.strictEqual(harness.elements.item.value, 'Item A');
    assert.strictEqual(harness.form.dataset.initialized, undefined);
}

async function runRoutingRulesAfterRetryScenario() {
    let typeRequests = 0;
    const harness = createHarness(async url => {
        if (url === '/api/cw/types/1') {
            typeRequests += 1;
            if (typeRequests === 1) return response([], { ok: false, status: 503 });
        }
        return response(lookupBody(url));
    }, { routingRules: JSON.stringify([{ type: 'Type A' }]) });
    await settle();

    assert.strictEqual(harness.routingRuleSnapshots.length, 0);
    await harness.elements['connectwise-retry'].fire('click');
    await settle();
    assert.deepStrictEqual(harness.routingRuleSnapshots, [['-- Use Default --', 'Type A']]);
}

async function runIndependentFailureBannerScenario() {
    const harness = createHarness(async url => {
        if (url === '/api/cw/priorities') return response([], { ok: false, status: 503 });
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return response(lookupBody(url, suffix));
    });
    await settle();

    assert.strictEqual(harness.elements['connectwise-load-error'].classList.contains('d-none'), false);
    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle();
    assert.deepStrictEqual(optionTexts(harness.elements.ticket_type), ['-- Use Default --', 'Type B']);
    assert.strictEqual(harness.elements['connectwise-load-error'].classList.contains('d-none'), false);
}

async function runStaleBoardScenario() {
    const boardARequests = [];
    const harness = createHarness(url => {
        if (/\/(statuses|types|subtypes|items)\/1$/.test(url)) {
            const pending = deferred();
            boardARequests.push({ pending, body: lookupBody(url, 'A') });
            return pending.promise;
        }
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return Promise.resolve(response(lookupBody(url, suffix)));
    });
    await settle();

    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle();
    assert.deepStrictEqual(optionTexts(harness.elements.ticket_type), ['-- Use Default --', 'Type B']);

    boardARequests.forEach(({ pending, body }) => pending.resolve(response(body)));
    await settle();
    assert.strictEqual(harness.elements.board.value, 'Board B');
    assert.deepStrictEqual(optionTexts(harness.elements.status), ['-- Use Default --', 'Status B', 'Closed B']);
    assert.deepStrictEqual(optionTexts(harness.elements.ticket_type), ['-- Use Default --', 'Type B']);
    assert.deepStrictEqual(optionTexts(harness.elements.subtype), ['-- Use Default --', 'Subtype B']);
    assert.deepStrictEqual(optionTexts(harness.elements.item), ['-- Use Default --', 'Item B']);
}

async function runProgressiveLoadingScenario() {
    const companies = deferred();
    const harness = createHarness(url => {
        if (url === '/api/cw/companies') return companies.promise;
        return Promise.resolve(response(lookupBody(url)));
    });
    await settle();

    assert.strictEqual(harness.elements.board.value, 'Board A');
    assert.strictEqual(harness.elements.status.value, 'Status A');
    assert.strictEqual(harness.elements.ticket_type.value, 'Type A');
    assert.strictEqual(harness.elements.item.value, 'Item A');
    assert.strictEqual(harness.form.dataset.initialized, 'loading');

    companies.resolve(response(lookupBody('/api/cw/companies')));
    await settle();
    assert.strictEqual(harness.form.dataset.initialized, 'true');
}

async function runSafeLoadingValuesScenario() {
    const pending = [];
    const harness = createHarness(() => {
        const request = deferred();
        pending.push(request);
        return request.promise;
    });
    await settle(2);

    assert.strictEqual(harness.elements.board.value, 'Board A');
    assert.strictEqual(harness.elements.priority.value, 'Priority A');
    assert.strictEqual(harness.elements.status.value, 'Status A');
    assert.strictEqual(harness.elements.close_status.value, 'Closed A');
    assert.strictEqual(harness.elements.ticket_type.value, 'Type A');
    assert.strictEqual(harness.elements.subtype.value, 'Subtype A');
    assert.strictEqual(harness.elements.item.value, 'Item A');
    assert.match(optionTexts(harness.elements.board).join(' '), /retained while loading/i);
}

async function runWaitForAutosaveScenario() {
    const calls = [];
    const harness = createHarness(async url => {
        calls.push(url);
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return response(lookupBody(url, suffix));
    }, { autosaveReady: false });
    await settle();
    assert.strictEqual(calls.length, 0);

    harness.elements.board.innerHTML = '<option value="Board B">Board B (restored draft)</option>';
    harness.elements.priority.innerHTML = '<option value="Priority B">Priority B (restored draft)</option>';
    harness.elements.status.innerHTML = '<option value="Status B">Status B (restored draft)</option>';
    harness.elements.close_status.innerHTML = '<option value="Closed B">Closed B (restored draft)</option>';
    harness.elements.ticket_type.innerHTML = '<option value="Type B">Type B (restored draft)</option>';
    harness.elements.subtype.innerHTML = '<option value="Subtype B">Subtype B (restored draft)</option>';
    harness.elements.item.innerHTML = '<option value="Item B">Item B (restored draft)</option>';
    harness.form.dataset.autosaveReady = 'true';
    await harness.fireDocument('hookwise:autosave-ready');
    await settle();

    assert.strictEqual(harness.elements.board.value, 'Board B');
    assert.strictEqual(harness.elements.priority.value, 'Priority B');
    assert.strictEqual(harness.elements.status.value, 'Status B');
    assert.strictEqual(harness.elements.ticket_type.value, 'Type B');
}

async function runRestoredBlankAutosaveScenario() {
    const calls = [];
    const harness = createHarness(async url => {
        calls.push(url);
        return response(lookupBody(url));
    }, { autosaveReady: false });
    await settle();

    const restoredSelects = [
        'board', 'priority', 'status', 'close_status', 'ticket_type', 'subtype', 'item'
    ];
    restoredSelects.forEach(id => {
        harness.elements[id].innerHTML = '<option value="">Loading...</option>';
        harness.elements[id].dataset.autosaveRestored = 'true';
    });
    harness.form.dataset.autosaveReady = 'true';
    await harness.fireDocument('hookwise:autosave-ready');
    await settle();

    assert.strictEqual(harness.elements.board.value, '');
    assert.strictEqual(harness.elements.priority.value, '');
    assert.strictEqual(harness.elements.status.value, '');
    assert.strictEqual(harness.elements.close_status.value, '');
    assert.strictEqual(harness.elements.ticket_type.value, '');
    assert.strictEqual(harness.elements.subtype.value, '');
    assert.strictEqual(harness.elements.item.value, '');
    assert.strictEqual(calls.some(url => /\/(statuses|types|subtypes|items)\//.test(url)), false);
}

async function runRetryBoardRaceScenario() {
    let priorityRequests = 0;
    let boardRequests = 0;
    const retryBoards = deferred();
    const harness = createHarness(url => {
        if (url === '/api/cw/boards') {
            boardRequests += 1;
            if (boardRequests === 2) return retryBoards.promise;
        }
        if (url === '/api/cw/priorities') {
            priorityRequests += 1;
            if (priorityRequests === 1) {
                return Promise.resolve(response([], { ok: false, status: 503 }));
            }
        }
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return Promise.resolve(response(lookupBody(url, suffix)));
    });
    await settle();

    await harness.elements['connectwise-retry'].fire('click');
    await settle(2);
    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle();
    assert.strictEqual(harness.elements.board.value, 'Board B');
    assert.deepStrictEqual(optionTexts(harness.elements.ticket_type), ['-- Use Default --', 'Type B']);

    retryBoards.resolve(response(lookupBody('/api/cw/boards')));
    await settle();
    assert.strictEqual(harness.elements.board.value, 'Board B');
    assert.deepStrictEqual(optionTexts(harness.elements.status), ['-- Use Default --', 'Status B', 'Closed B']);
    assert.deepStrictEqual(optionTexts(harness.elements.ticket_type), ['-- Use Default --', 'Type B']);
    assert.deepStrictEqual(optionTexts(harness.elements.subtype), ['-- Use Default --', 'Subtype B']);
    assert.deepStrictEqual(optionTexts(harness.elements.item), ['-- Use Default --', 'Item B']);
}

async function runRoutingRulesBeforeSlowCompaniesScenario() {
    const companies = deferred();
    const harness = createHarness(url => {
        if (url === '/api/cw/companies') return companies.promise;
        return Promise.resolve(response(lookupBody(url)));
    }, { routingRules: JSON.stringify([{ type: 'Type A' }]) });
    await settle();

    assert.deepStrictEqual(harness.routingRuleSnapshots, [['-- Use Default --', 'Type A']]);
    assert.strictEqual(harness.elements['add-routing-rule'].disabled, false);
    companies.resolve(response(lookupBody('/api/cw/companies')));
    await settle();
    assert.strictEqual(harness.routingRuleSnapshots.length, 1);
}

async function runRoutingRulesAfterBoardRecoveryScenario() {
    const harness = createHarness(url => {
        if (url === '/api/cw/types/1') {
            return Promise.resolve(response([], { ok: false, status: 503 }));
        }
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return Promise.resolve(response(lookupBody(url, suffix)));
    }, { routingRules: JSON.stringify([{ type: 'Type B' }]) });
    await settle();

    assert.strictEqual(harness.routingRuleSnapshots.length, 0);
    assert.strictEqual(harness.elements['add-routing-rule'].disabled, true);
    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle();
    assert.deepStrictEqual(harness.routingRuleSnapshots, [['-- Use Default --', 'Type B']]);
    assert.strictEqual(harness.elements['add-routing-rule'].disabled, false);
}

async function runBoardChangeSubmitGuardScenario() {
    let typeRequestsForBoardB = 0;
    const harness = createHarness(url => {
        if (url === '/api/cw/types/2') {
            typeRequestsForBoardB += 1;
            if (typeRequestsForBoardB === 1) {
                return Promise.resolve(response([], { ok: false, status: 503 }));
            }
        }
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return Promise.resolve(response(lookupBody(url, suffix)));
    });
    await settle();

    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle();
    for (const id of ['hw-submit-btn', 'draft-btn', 'hw-save-another-btn']) {
        assert.strictEqual(harness.elements[id].disabled, true);
    }

    await harness.elements['connectwise-retry'].fire('click');
    await settle();
    for (const id of ['hw-submit-btn', 'draft-btn', 'hw-save-another-btn']) {
        assert.strictEqual(harness.elements[id].disabled, false);
    }
    assert.deepStrictEqual(optionTexts(harness.elements.ticket_type), ['-- Use Default --', 'Type B']);
}

async function runBoardChangeRoutingGuardScenario() {
    const boardBTypes = deferred();
    const harness = createHarness(url => {
        if (url === '/api/cw/types/2') return boardBTypes.promise;
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return Promise.resolve(response(lookupBody(url, suffix)));
    });
    await settle();

    assert.strictEqual(harness.elements['add-routing-rule'].disabled, false);
    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle(2);
    assert.strictEqual(harness.elements['add-routing-rule'].disabled, true);
    assert.strictEqual(harness.elements.status.value, '');

    boardBTypes.resolve(response(lookupBody('/api/cw/types/2', 'B')));
    await settle();
    assert.strictEqual(harness.elements['add-routing-rule'].disabled, false);
}

async function runMissingBoardRecoveryScenario() {
    const harness = createHarness(url => {
        if (url === '/api/cw/boards') {
            return Promise.resolve(response([{ id: 2, name: 'Board B' }]));
        }
        const suffix = url.endsWith('/2') ? 'B' : 'A';
        return Promise.resolve(response(lookupBody(url, suffix)));
    });
    await settle();
    assert.strictEqual(harness.elements['connectwise-load-error'].classList.contains('d-none'), false);

    harness.elements.board.value = 'Board B';
    await harness.elements.board.fire('change');
    await settle();
    assert.strictEqual(harness.elements['connectwise-load-error'].classList.contains('d-none'), true);
}

async function runMissingBoardDefaultRecoveryScenario() {
    const harness = createHarness(url => {
        if (url === '/api/cw/boards') return Promise.resolve(response([]));
        return Promise.resolve(response(lookupBody(url)));
    });
    await settle();
    assert.strictEqual(harness.elements['connectwise-load-error'].classList.contains('d-none'), false);

    harness.elements.board.value = '';
    await harness.elements.board.fire('change');
    await settle();
    assert.strictEqual(harness.elements['connectwise-load-error'].classList.contains('d-none'), true);
}

const scenarios = {
    success: runSuccessScenario,
    retry: runFailureAndRetryScenario,
    shape: runInvalidJsonShapeScenario,
    itemshape: runInvalidLookupItemScenario,
    empty: runEmptySavedLookupScenario,
    emptyboards: runEmptyBoardsScenario,
    routingretry: runRoutingRulesAfterRetryScenario,
    independentfailure: runIndependentFailureBannerScenario,
    stale: runStaleBoardScenario,
    progressive: runProgressiveLoadingScenario,
    safeloading: runSafeLoadingValuesScenario,
    autosavewait: runWaitForAutosaveScenario,
    autosaveblank: runRestoredBlankAutosaveScenario,
    retryboardrace: runRetryBoardRaceScenario,
    routingslowcompany: runRoutingRulesBeforeSlowCompaniesScenario,
    routingboardrecovery: runRoutingRulesAfterBoardRecoveryScenario,
    boardchangesubmit: runBoardChangeSubmitGuardScenario,
    boardchangerouting: runBoardChangeRoutingGuardScenario,
    missingboardrecovery: runMissingBoardRecoveryScenario,
    missingboarddefault: runMissingBoardDefaultRecoveryScenario,
};

scenarios[process.argv[1]]().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
"""


def _run_node_scenario(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for endpoint form loader coverage")

    result = subprocess.run(
        [node, "-e", NODE_HARNESS, scenario],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_endpoint_form_awaits_lookups_and_preserves_saved_values():
    _run_node_scenario("success")


def test_endpoint_form_shows_failed_lookup_and_can_retry():
    _run_node_scenario("retry")


def test_endpoint_form_rejects_non_array_lookup_payloads():
    _run_node_scenario("shape")


def test_endpoint_form_rejects_malformed_lookup_items():
    _run_node_scenario("itemshape")


def test_endpoint_form_preserves_saved_values_when_lookup_is_empty():
    _run_node_scenario("empty")


def test_endpoint_form_preserves_saved_values_when_boards_are_empty():
    _run_node_scenario("emptyboards")


def test_endpoint_form_builds_routing_rules_only_after_successful_retry():
    _run_node_scenario("routingretry")


def test_successful_board_change_does_not_hide_other_lookup_failures():
    _run_node_scenario("independentfailure")


def test_endpoint_form_ignores_stale_board_responses():
    _run_node_scenario("stale")


def test_endpoint_form_applies_independent_lookups_progressively():
    _run_node_scenario("progressive")


def test_endpoint_form_retains_values_while_lookups_are_loading():
    _run_node_scenario("safeloading")


def test_endpoint_form_waits_for_autosave_restoration():
    _run_node_scenario("autosavewait")


def test_endpoint_form_preserves_explicit_blank_autosave_values():
    _run_node_scenario("autosaveblank")


def test_retry_cannot_overwrite_a_newer_board_selection():
    _run_node_scenario("retryboardrace")


def test_routing_rules_do_not_wait_for_unrelated_company_lookup():
    _run_node_scenario("routingslowcompany")


def test_routing_rules_hydrate_after_successful_board_recovery():
    _run_node_scenario("routingboardrecovery")


def test_add_routing_rule_is_disabled_until_saved_rules_are_hydrated():
    template = (ROOT / "templates/form.html").read_text(encoding="utf-8")
    button = re.search(r"<button\b[^>]*\bid=\"add-routing-rule\"[^>]*>", template, re.DOTALL)

    assert button is not None
    assert " disabled" in button.group(0)


def test_board_change_blocks_all_save_paths_until_lookup_recovers():
    _run_node_scenario("boardchangesubmit")


def test_board_change_blocks_new_routing_rules_until_lookup_recovers():
    _run_node_scenario("boardchangerouting")


def test_selecting_an_available_board_clears_a_missing_board_failure():
    _run_node_scenario("missingboardrecovery")


def test_selecting_default_board_clears_a_missing_board_failure():
    _run_node_scenario("missingboarddefault")
