var hookwiseBaseUrl = document.querySelector('meta[name="hookwise-base-url"]')?.content || '';
var hookwiseEndpointId = document.querySelector('meta[name="hookwise-endpoint-id"]')?.content || '';

    function showSetupGuide(id) {
        const modal = new bootstrap.Modal(document.getElementById('setupGuideModal'));
        const url = `${hookwiseBaseUrl}/w/${id}`;
        // Get token if visible/known (hidden field?)
        const tokenDisplay = document.getElementById('bearer-token-display');
        const token = (tokenDisplay && tokenDisplay.value !== '****************') ? tokenDisplay.value : 'YOUR_BEARER_TOKEN';

        const payload = {
            "title": "Test Alert from Setup Guide",
            "message": "This is a test notification to verify HookWise connectivity.",
            "status": "firing",
            "severity": "critical"
        };
        const jsonPayload = JSON.stringify(payload, null, 2);

        // Curl
        const curl = `curl -X POST "${url}" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${token}" \\
  -d '${JSON.stringify(payload)}'`;
        document.getElementById('code-curl').textContent = curl;

        // Python
        const python = `import requests

url = "${url}"
headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer ${token}"
}
payload = ${jsonPayload}

response = requests.post(url, json=payload, headers=headers)
print(response.status_code, response.text)`;
        document.getElementById('code-python').textContent = python;

        // PowerShell
        const ps = `$url = "${url}"
$headers = @{
    "Content-Type" = "application/json"
    "Authorization" = "Bearer ${token}"
}
$body = '${JSON.stringify(payload).replace(/'/g, "''")}'

Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $body`;
        document.getElementById('code-powershell').textContent = ps;

        modal.show();
    }

    var undoStack = [];
    var redoStack = [];
    var isUndoing = false;

    function saveState() {
        if (isUndoing) return;
        const form = document.getElementById('endpoint-form');
        // Das verzoegerte Autosave kann nach einer hx-boost-Navigation feuern,
        // wenn das Formular bereits aus dem DOM ist -- dann still aussteigen
        // statt an FormData(null) zu scheitern.
        if (!form || !form.isConnected) return;
        const formData = new FormData(form);
        const state = {};
        formData.forEach((value, key) => state[key] = value);

        const stateStr = JSON.stringify(state);
        if (undoStack.length > 0 && undoStack[undoStack.length - 1] === stateStr) return;

        undoStack.push(stateStr);
        if (undoStack.length > 50) undoStack.shift();

        document.getElementById('undo-btn').disabled = undoStack.length < 2;
        redoStack.length = 0; // Clear redo on new action
        document.getElementById('redo-btn').disabled = true;

        // Show save indicator
        const indicator = document.getElementById('save-indicator');
        if (indicator) {
            indicator.classList.add('visible');
            clearTimeout(window.saveIndicatorTimeout);
            window.saveIndicatorTimeout = setTimeout(() => {
                indicator.classList.remove('visible');
            }, 2000);
        }
    }

    function undo() {
        if (undoStack.length < 2) return;
        isUndoing = true;
        const currentState = undoStack.pop();
        redoStack.push(currentState);
        const prevState = JSON.parse(undoStack[undoStack.length - 1]);
        applyState(prevState);
        document.getElementById('redo-btn').disabled = false;
        document.getElementById('undo-btn').disabled = undoStack.length < 2;
        isUndoing = false;
    }

    function redo() {
        if (!redoStack.length) return;
        isUndoing = true;
        const nextStateStr = redoStack.pop();
        undoStack.push(nextStateStr);
        applyState(JSON.parse(nextStateStr));
        document.getElementById('redo-btn').disabled = redoStack.length === 0;
        document.getElementById('undo-btn').disabled = false;
        isUndoing = false;
    }

    function applyState(state) {
        const form = document.getElementById('endpoint-form');
        Object.keys(state).forEach(key => {
            const el = form.elements[key];
            if (el && el.type !== 'file') el.value = state[key];
        });
        if (window.updatePreview) window.updatePreview();
        // Also sync maintenance windows if they changed
        const mwList = document.getElementById('maintenance-list');
        if (mwList) {
            mwList.innerHTML = '';
            if (state.maintenance_windows) {
                try {
                    const windows = JSON.parse(state.maintenance_windows);
                    windows.forEach(w => addMaintenanceWindow(w));
                } catch (e) { }
            }
        }

        const rulesList = document.getElementById('rules-container');
        if (rulesList) {
            rulesList.innerHTML = '';
            if (state.routing_rules) {
                try {
                    const rules = JSON.parse(state.routing_rules);
                    rules.forEach(r => addRoutingRule(r));
                } catch (e) { }
            }
        }
    }

    function toggleAdvanced() {
        const show = document.getElementById('show-advanced').checked;
        document.getElementById('advanced-fields').classList.toggle('d-none', !show);
    }

    function toggleBearerMgmt() {
        const checkbox = document.getElementById('bearer_auth_enabled');
        const section = document.getElementById('token-mgmt-section');
        if (section) {
            if (checkbox.checked) {
                section.classList.remove('d-none');
            } else {
                section.classList.add('d-none');
            }
        }
    }

    function toggleAI() {
        const rca = document.getElementById('ai_rca_enabled').checked;
        document.getElementById('ai-settings').classList.toggle('d-none', !rca);
    }

    function insertAtCursor(event, id, text) {
        if (event) event.preventDefault();
        const el = document.getElementById(id);
        if (!el) return;

        const start = el.selectionStart;
        const end = el.selectionEnd;
        const val = el.value;

        el.value = val.substring(0, start) + text + val.substring(end);
        el.selectionStart = el.selectionEnd = start + text.length;
        el.focus();

        // Trigger input event for autosave/preview
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return false;
    }

    function addMaintenanceWindow(w = {}) {
        const list = document.getElementById('maintenance-list');
        const count = document.querySelectorAll('.maintenance-entry').length;
        const div = document.createElement('div');
        div.className = 'p-3 mb-3 rounded border border-secondary border-opacity-25 bg-dark bg-opacity-25 maintenance-entry';

        const allowedTypes = new Set(['once', 'daily', 'weekly']);
        const type = allowedTypes.has(w.type) ? w.type : 'once';
        const start = String(w.start || '');
        const end = String(w.end || '');
        const days = Array.isArray(w.days) ? w.days : [];

        div.innerHTML = `
            <div class="row g-2 mb-3 align-items-center">
                <div class="col-md-4">
                    <select class="form-select form-select-sm mw-type bg-dark border-secondary text-info fw-bold" onchange="updateMWFields(this)">
                        <option value="once">One-time</option>
                        <option value="daily">Daily</option>
                        <option value="weekly">Weekly</option>
                    </select>
                </div>
                <div class="col-md-8 text-end">
                    <button type="button" class="btn btn-sm btn-link text-danger p-0 fw-bold text-decoration-none" onclick="this.closest('.maintenance-entry').remove(); syncMaintenance();">
                        <svg class="hw-icon me-1" width="14" height="14" aria-hidden="true" focusable="false"><use href="#i-x-lg"></use></svg>Remove Window
                    </button>
                </div>
            </div>

            <div class="mw-fields">
                <!-- Days for Weekly -->
                <div class="mw-days-container mb-3 d-none">
                    <label class="small text-secondary fw-bold text-uppercase ls-1 d-block mb-2">Days of Week (UTC)</label>
                    <div class="d-flex flex-wrap gap-2">
                        ${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => `
                            <div>
                                <input type="checkbox" class="btn-check mw-day" id="mw-${count}-${d}" value="${d}" onchange="syncMaintenance()">
                                <label class="btn btn-sm btn-outline-secondary px-2 py-1 hw-t-xs" for="mw-${count}-${d}">${d}</label>
                            </div>
                        `).join('')}
                    </div>
                </div>

                <div class="row g-2">
                    <div class="col-6">
                        <label class="small text-secondary fw-bold text-uppercase ls-1 d-block mb-1">Start (UTC)</label>
                        <input type="time"
                               class="form-control form-control-sm mw-start bg-dark border-secondary text-light" 
                               onchange="syncMaintenance()">
                    </div>
                    <div class="col-6">
                        <label class="small text-secondary fw-bold text-uppercase ls-1 d-block mb-1">End (UTC)</label>
                        <input type="time"
                               class="form-control form-control-sm mw-end bg-dark border-secondary text-light" 
                               onchange="syncMaintenance()">
                    </div>
                </div>
            </div>
        `;
        const typeSelect = div.querySelector('.mw-type');
        const daysContainer = div.querySelector('.mw-days-container');
        const startInput = div.querySelector('.mw-start');
        const endInput = div.querySelector('.mw-end');
        typeSelect.value = type;
        daysContainer.classList.toggle('d-none', type !== 'weekly');
        startInput.type = type === 'once' ? 'datetime-local' : 'time';
        endInput.type = type === 'once' ? 'datetime-local' : 'time';
        startInput.value = start;
        endInput.value = end;
        div.querySelectorAll('.mw-day').forEach(day => { day.checked = days.includes(day.value); });
        list.appendChild(div);
    }

    function updateMWFields(select) {
        const entry = select.closest('.maintenance-entry');
        const type = select.value;
        const daysContainer = entry.querySelector('.mw-days-container');
        const startInput = entry.querySelector('.mw-start');
        const endInput = entry.querySelector('.mw-end');

        if (type === 'once') {
            daysContainer.classList.add('d-none');
            startInput.type = 'datetime-local';
            endInput.type = 'datetime-local';
        } else {
            daysContainer.classList.toggle('d-none', type !== 'weekly');
            startInput.type = 'time';
            endInput.type = 'time';
        }
        syncMaintenance();
    }

    function syncMaintenance() {
        const windows = [];
        document.querySelectorAll('.maintenance-entry').forEach(entry => {
            const type = entry.querySelector('.mw-type').value;
            const start = entry.querySelector('.mw-start').value;
            const end = entry.querySelector('.mw-end').value;
            const days = Array.from(entry.querySelectorAll('.mw-day:checked')).map(cb => cb.value);

            if (start && end) {
                const w = { type, start, end };
                if (type === 'weekly') w.days = days;
                windows.push(w);
            }
        });
        document.getElementById('maintenance_windows').value = JSON.stringify(windows);
    }
    function syncRemoveStrings() {
        const container = document.getElementById('remove-strings-container');
        const inputs = container.querySelectorAll('.remove-string-input');
        // Map to values, preserving entirely blank strings if they specifically want space formatting, 
        // but typically we'll just ignore empty boxes. We use filter to discard empty inputs.
        const values = Array.from(inputs).map(i => i.value).filter(v => v !== "");
        document.getElementById('summary_remove_strings').value = values.join(',');
    }

    function addRemoveString(value = '') {
        const container = document.getElementById('remove-strings-container');
        const div = document.createElement('div');
        div.className = 'd-flex gap-2 mb-2 remove-string-entry';


        div.innerHTML = `
            <input type="text" class="form-control bg-dark border-secondary text-light small remove-string-input" 
                placeholder="e.g. *All Tenants (AllTenants): " maxlength="500"
                oninput="syncRemoveStrings()">
            <button type="button" class="btn btn-outline-danger btn-sm" onclick="this.closest('.remove-string-entry').remove(); syncRemoveStrings();">
                <svg class="hw-icon" width="14" height="14" aria-hidden="true" focusable="false"><use href="#i-x-lg"></use></svg>
            </button>
        `;
        div.querySelector('.remove-string-input').value = String(value);
        container.appendChild(div);
    }

    function addRoutingRule(rule = {}) {
        const container = document.getElementById('rules-container');
        const count = container.querySelectorAll('.rule-entry').length;
        const div = document.createElement('div');
        div.className = 'p-3 mb-3 rounded border border-secondary border-opacity-25 bg-dark bg-opacity-25 rule-entry animate-fade-in';

        const path = rule.path || '';
        const regex = rule.regex || '';
        const overrides = rule.overrides || {};

        div.innerHTML = `
            <div class="row g-2 mb-2 align-items-center">
                <div class="col-md-5">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">PATH</span>
                        <input type="text" class="form-control bg-dark border-secondary text-info font-monospace rule-path" 
                               placeholder="$.field" oninput="syncRoutingRules()">
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">REGEX</span>
                        <input type="text" class="form-control bg-dark border-secondary text-warning font-monospace rule-regex" 
                               placeholder=".*" oninput="validateRegex(this); syncRoutingRules()">
                        <span class="input-group-text bg-dark border-secondary regex-status">
                            <svg class="hw-icon text-success" width="14" height="14" aria-hidden="true" focusable="false"><use href="#i-check-circle-fill"></use></svg>
                        </span>
                    </div>
                </div>
                <div class="col-md-1 text-end">
                    <button type="button" class="btn btn-sm btn-link text-danger p-0 fw-bold text-decoration-none" 
                            onclick="this.closest('.rule-entry').remove(); syncRoutingRules(); document.getElementById('routing_rules').dispatchEvent(new Event('input', { bubbles: true }));">
                        <svg class="hw-icon me-1" width="14" height="14" aria-hidden="true" focusable="false"><use href="#i-x-lg"></use></svg>Delete Rule
                    </button>
                </div>
            </div>
            <div class="row g-2">
                <div class="col-md-2">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">BOARD</span>
                        <select class="form-select bg-dark border-secondary text-light rule-board" onchange="syncRoutingRules()">
                            <option value="">-- No Change --</option>
                        </select>
                    </div>
                </div>
                <div class="col-md-2">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">PRIORITY</span>
                        <select class="form-select bg-dark border-secondary text-light rule-priority" onchange="syncRoutingRules()">
                            <option value="">-- No Change --</option>
                        </select>
                    </div>
                </div>
                 <div class="col-md-2">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">STATUS</span>
                        <input type="text" class="form-control bg-dark border-secondary text-light rule-status" 
                               placeholder="New Status" oninput="syncRoutingRules()">
                    </div>
                </div>
                <div class="col-md-2">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">TYPE</span>
                        <select class="form-select bg-dark border-secondary text-light rule-type" onchange="syncRoutingRules()">
                            <option value="">-- No Change --</option>
                        </select>
                    </div>
                </div>
                <div class="col-md-2">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">SUBTYPE</span>
                        <select class="form-select bg-dark border-secondary text-light rule-subtype" onchange="syncRoutingRules()">
                            <option value="">-- No Change --</option>
                        </select>
                    </div>
                </div>
                <div class="col-md-2">
                    <div class="input-group input-group-sm">
                        <span class="input-group-text bg-dark border-secondary text-secondary hw-t-xs">ITEM</span>
                        <select class="form-select bg-dark border-secondary text-light rule-item" onchange="syncRoutingRules()">
                            <option value="">-- No Change --</option>
                        </select>
                    </div>
                </div>
            </div>
            <div class="row mt-2">
                <div class="col-md-12">
                    <div class="form-check form-switch template-card d-inline-block py-1 pe-3">
                        <input class="form-check-input rule-drop" type="checkbox" role="switch" id="drop-${count}" onchange="syncRoutingRules()">
                        <label class="form-check-label small text-warning fw-bold" for="drop-${count}">Drop Webhook</label>
                    </div>
                </div>
            </div>
        `;
        div.querySelector('.rule-path').value = String(path);
        div.querySelector('.rule-regex').value = String(regex);
        div.querySelector('.rule-status').value = String(overrides.status || '');
        div.querySelector('.rule-drop').checked = Boolean(overrides.drop);

        container.appendChild(div);

        // Populate selects from the main board/priority lists if they exist
        const boardOptions = document.getElementById('board').innerHTML;
        const priorityOptions = document.getElementById('priority').innerHTML;
        const typeOptions = document.getElementById('ticket_type').innerHTML;
        const subtypeOptions = document.getElementById('subtype').innerHTML;
        const itemOptions = document.getElementById('item').innerHTML;

        const bSelect = div.querySelector('.rule-board');
        const pSelect = div.querySelector('.rule-priority');
        const tSelect = div.querySelector('.rule-type');
        const sSelect = div.querySelector('.rule-subtype');
        const iSelect = div.querySelector('.rule-item');

        const appendOptions = (selectElem, optionsHTML, overrideVal) => {
            if (optionsHTML) {
                // Ensure options don't carry over the 'selected' attribute from the main form
                selectElem.innerHTML = '<option value="">-- No Change --</option>' + optionsHTML.replace(/selected(="[^"]*")?/gi, '');
                if (overrideVal) selectElem.value = overrideVal;
            }
        };

        appendOptions(bSelect, boardOptions, overrides.board);
        appendOptions(pSelect, priorityOptions, overrides.priority);
        appendOptions(tSelect, typeOptions, overrides.ticket_type);
        appendOptions(sSelect, subtypeOptions, overrides.subtype);
        appendOptions(iSelect, itemOptions, overrides.item);

        validateRegex(div.querySelector('.rule-regex'));
    }

    function validateRegex(input) {
        const val = input.value;
        const statusIcon = input.nextElementSibling;
        try {
            if (val) new RegExp(val);
            statusIcon.innerHTML = '<svg class="hw-icon text-success" width="14" height="14" aria-hidden="true" focusable="false"><use href="#i-check-circle-fill"></use></svg>';
            input.classList.remove('is-invalid');
            input.classList.add('is-valid');
        } catch (e) {
            statusIcon.innerHTML = '<svg class="hw-icon text-danger" width="14" height="14" aria-hidden="true" focusable="false"><use href="#i-exclamation-triangle-fill"></use></svg><span class="visually-hidden">Invalid Regex</span>';
            input.classList.remove('is-valid');
            input.classList.add('is-invalid');
        }
    }

    function syncRoutingRules() {
        const rules = [];
        document.querySelectorAll('.rule-entry').forEach(entry => {
            const path = entry.querySelector('.rule-path').value.trim();
            const regex = entry.querySelector('.rule-regex').value.trim();
            const board = entry.querySelector('.rule-board').value;
            const priority = entry.querySelector('.rule-priority').value;
            const status = entry.querySelector('.rule-status').value.trim();
            const ticket_type = entry.querySelector('.rule-type').value;
            const subtype = entry.querySelector('.rule-subtype').value;
            const item = entry.querySelector('.rule-item').value;
            const drop = entry.querySelector('.rule-drop').checked;

            if (path && regex) {
                const overrides = {};
                if (board) overrides.board = board;
                if (priority) overrides.priority = priority;
                if (status) overrides.status = status;
                if (ticket_type) overrides.ticket_type = ticket_type;
                if (subtype) overrides.subtype = subtype;
                if (item) overrides.item = item;
                if (drop) overrides.drop = drop;

                rules.push({ path, regex, overrides });
            }
        });
        document.getElementById('routing_rules').value = JSON.stringify(rules);
    }

    var templates = {
        uptime_kuma: {
            json_mapping: '{\n  "summary": "$.monitor.name",\n  "description": "$.msg",\n  "status": "$.monitor.status"\n}',
            trigger_field: 'heartbeat.status',
            open_value: '0',
            close_value: '1'
        },
        grafana: {
            json_mapping: '{\n  "summary": "$.title",\n  "description": "$.message",\n  "severity": "$.ruleName"\n}',
            trigger_field: 'state',
            open_value: 'alerting',
            close_value: 'ok'
        },
        zabbix: {
            json_mapping: '{\n  "summary": "$.event_name",\n  "description": "$.event_nseverity",\n  "priority": "$.event_severity"\n}',
            trigger_field: 'event_value',
            open_value: '1',
            close_value: '0'
        },
        cipp: {
            json_mapping: '{\n  "summary": "$.TaskInfo.Name",\n  "description": "$.TaskInfo.Results",\n  "ticket_type": "$.TaskInfo.Command"\n}',
            trigger_field: 'TaskInfo.TaskState',
            open_value: 'Pending',
            close_value: 'Resolved'
        }
    };
function applyEndpointTemplate(templateKey) {
    const dataElement = document.getElementById('endpoint-template-data');
    if (!dataElement || !templateKey) return;
    let templates;
    try {
        templates = JSON.parse(dataElement.textContent || '{}');
    } catch (_error) {
        return;
    }
    const preset = templates[templateKey];
    if (!preset) return;
    Object.entries(preset).forEach(([name, value]) => {
        if (name === 'label') return;
        const field = document.querySelector(`[name="${CSS.escape(name)}"]`);
        if (!field) return;
        if (field.type === 'checkbox') field.checked = Boolean(value);
        else field.value = String(value);
        field.dispatchEvent(new Event('change', { bubbles: true }));
    });
}

document.addEventListener('DOMContentLoaded', () => {
    const selector = document.getElementById('endpoint-template');
    if (selector) selector.addEventListener('change', () => applyEndpointTemplate(selector.value));
});

// ---- Field Mapping als Code-Editor (Prism-Overlay, ohne neue Abhaengigkeit).
// Die Textarea bleibt das echte Formularfeld (Name, Submit, Autosave,
// insertAtCursor unveraendert); dahinter liegt ein deckungsgleiches,
// hervorgehobenes <pre>. Programmatische Schreiber (Apply Template, Format)
// laufen ueber einen umgeleiteten value-Setter mit.
(function () {
    const feld = document.getElementById('json_mapping');
    if (!feld || feld.dataset.codeInit) return;
    feld.dataset.codeInit = '1';
    const huelle = document.createElement('div');
    huelle.className = 'hw-code';
    feld.parentNode.insertBefore(huelle, feld);
    const glanz = document.createElement('pre');
    glanz.className = 'hw-code-glanz';
    glanz.setAttribute('aria-hidden', 'true');
    const code = document.createElement('code');
    // Das lokale Prism-Bundle enthaelt kein json-Grammar -- js hebt
    // Strings/Zahlen/Interpunktion identisch hervor.
    code.className = 'language-js';
    glanz.appendChild(code);
    huelle.appendChild(glanz);
    huelle.appendChild(feld);
    feld.classList.add('hw-code-feld');
    const status = document.getElementById('json-mapping-status');
    const rendern = () => {
        const wert = basis.get.call(feld);
        code.textContent = wert + '\n';
        if (window.Prism) Prism.highlightElement(code);
        if (status) {
            if (!wert.trim()) {
                status.hidden = true;
            } else {
                status.hidden = false;
                try {
                    JSON.parse(wert);
                    status.textContent = 'Valid JSON';
                    status.dataset.zustand = 'ok';
                } catch (err) {
                    status.textContent = 'Invalid JSON';
                    status.dataset.zustand = 'crit';
                }
            }
        }
    };
    const basis = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    Object.defineProperty(feld, 'value', {
        configurable: true,
        get() { return basis.get.call(this); },
        set(v) {
            basis.set.call(this, v);
            rendern();
            // Programmatische Schreiber (Apply Template, Format) sollen wie
            // Nutzereingaben zaehlen: Autosave/Undo haengen am input-Event.
            this.dispatchEvent(new Event('input', { bubbles: true }));
        },
    });
    feld.addEventListener('input', rendern);
    feld.addEventListener('scroll', () => {
        glanz.scrollTop = feld.scrollTop;
        glanz.scrollLeft = feld.scrollLeft;
    });
    rendern();
    // prism.js laedt defer im Head und ist beim ersten rendern() noch nicht
    // da -- einmal nachziehen, sobald alles geladen ist.
    if (!window.Prism) window.addEventListener('load', rendern, { once: true });
})();

// ---- Skeleton-Schimmer statt "Loading..." in den CW-Selects ----------------
(function () {
    ['board', 'priority'].forEach((id) => {
        const sel = document.getElementById(id);
        if (!sel || sel.dataset.ladeInit) return;
        sel.dataset.ladeInit = '1';
        const pruefen = () => {
            // Exakt nur der Platzhalter -- 'Error loading boards' darf den
            // Schimmer NICHT halten, sonst pulsiert er bei CW-Fehlern ewig.
            const laedt = sel.options.length === 1 && /^loading\.\.\.$/i.test((sel.options[0].text || '').trim());
            sel.classList.toggle('hw-lade', laedt);
        };
        new MutationObserver(pruefen).observe(sel, { childList: true, subtree: true });
        pruefen();
    });
})();
