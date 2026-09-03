    function applyTemplate(type) {
        if (!type || !templates[type]) return;
        const t = templates[type];
        document.getElementById('json_mapping').value = t.json_mapping;
        document.getElementById('trigger_field').value = t.trigger_field;
        document.getElementById('open_value').value = t.open_value;
        document.getElementById('close_value').value = t.close_value;
        showToast('Applied ' + type.replace('_', ' ') + ' template', 'info');
    }

    async function runDebugger() {
        const payloadText = document.getElementById('sample-json').value;
        const resultDiv = document.getElementById('debugger-result');

        if (!payloadText.trim()) {
            return showToast('Please enter a sample JSON payload', 'warning');
        }

        let payload;
        try {
            payload = JSON.parse(payloadText);
        } catch (e) {
            return showToast('Invalid JSON in sample payload', 'danger');
        }

        resultDiv.innerHTML = '<div class="text-center py-2"><span class="spinner-border spinner-border-sm text-info"></span> Simulation running...</div>';

        // Collect current form state
        const form = document.getElementById('endpoint-form');
        const formData = new FormData(form);
        const config = {};
        formData.forEach((value, key) => config[key] = value);

        try {
            const res = await fetch('/api/debug/process', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
                },
                body: JSON.stringify({ payload, config })
            });
            const data = await res.json();

            if (data.status === 'success') {
                resultDiv.replaceChildren();
                const list = document.createElement('div');
                list.className = 'list-group list-group-flush bg-transparent';
                data.steps.forEach(step => {
                    const item = document.createElement('div');
                    item.className = 'list-group-item bg-transparent border-secondary border-opacity-10 text-secondary py-1 px-0';
                    const arrow = document.createElement('span');
                    arrow.className = 'text-info me-2';
                    arrow.innerHTML = '<svg class="hw-icon" width="12" height="12" aria-hidden="true" focusable="false"><use href="#i-arrow-right"></use></svg>';
                    item.append(arrow, document.createTextNode(` ${String(step)}`));
                    list.appendChild(item);
                });

                const resultPanel = document.createElement('div');
                resultPanel.className = 'mt-3 p-2 rounded bg-dark border border-secondary border-opacity-25';
                const resultTitle = document.createElement('div');
                resultTitle.className = 'fw-bold text-success mb-1 small';
                resultTitle.textContent = 'FINAL RESULT:';
                const resultPre = document.createElement('pre');
                resultPre.className = 'mb-0 text-white font-monospace small';
                resultPre.classList.add('hw-t-xs');
                resultPre.textContent = JSON.stringify(data.results, null, 2);
                resultPanel.append(resultTitle, resultPre);
                resultDiv.append(list, resultPanel);
            } else {
                const error = document.createElement('div');
                error.className = 'alert alert-danger py-2 small';
                error.textContent = String(data.message || 'Request failed');
                resultDiv.replaceChildren(error);
            }
        } catch (e) {
            const error = document.createElement('div');
            error.className = 'alert alert-danger py-2 small';
            error.textContent = 'Error communicating with server';
            resultDiv.replaceChildren(error);
        }
    }

    function formatJSON(id) {
        const el = document.getElementById(id);
        try {
            const obj = JSON.parse(el.value);
            el.value = JSON.stringify(obj, null, 2);
        } catch (e) {
            showToast('Invalid JSON in field', 'danger');
        }
    }

    function saveAsDraft() {
        document.getElementById('is_draft').value = 'true';
        document.getElementById('endpoint-form').submit();
    }

    function saveAndAnother() {
        document.getElementById('create_another').value = 'true';
        document.getElementById('endpoint-form').submit();
    }

    async function confirmDelete(id, name) {
        if (await hwConfirm(`Are you sure you want to delete "${name}"? This action cannot be undone.`, { title: 'Delete Endpoint', okText: 'Delete' })) {
            // Add a tiny delay to let the modal finish hiding before navigation
            setTimeout(() => {
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = `/endpoint/delete/${id}`;
                const csrfInput = document.createElement('input');
                csrfInput.type = 'hidden';
                csrfInput.name = 'csrf_token';
                csrfInput.value = document.querySelector('meta[name="csrf-token"]')?.content || '';
                form.appendChild(csrfInput);
                document.body.appendChild(form);
                form.submit();
            }, 300);
        }
    }

    // Das Auge war eine Font-Awesome-Klasse, die nie geladen wurde -- der
    // Klassentausch schaltete also zwischen zwei unsichtbaren Zustaenden um.
    // Jetzt zeigt es auf ein Symbol im Sprite und wird ueber href getauscht.
    function setEyeIcon(icon, name) {
        if (!icon) return;
        const use = icon.querySelector('use');
        if (use) use.setAttribute('href', '#i-' + name);
    }

    var tokenLoaded = false;
    async function toggleTokenVisibility() {
        const input = document.getElementById('bearer-token-display');
        const icon = document.getElementById('token-eye-icon');
        const configId = hookwiseEndpointId;

        if (input.type === 'password') {
            if (!tokenLoaded && configId) {
                try {
                    const res = await fetch(`/endpoint/token/${configId}`);
                    const data = await res.json();
                    if (data.token) {
                        input.value = data.token;
                        tokenLoaded = true;
                    }
                } catch (e) {
                    showToast('Failed to load token', 'danger');
                    return;
                }
            }
            input.type = 'text';
            setEyeIcon(icon, 'eye-slash');
        } else {
            input.type = 'password';
            setEyeIcon(icon, 'eye');
        }
    }

    function copyBearerToken() {
        const input = document.getElementById('bearer-token-display');
        if (input.value === '****************' && !tokenLoaded) {
            showToast('Please reveal the token first', 'warning');
            return;
        }
        navigator.clipboard.writeText(input.value).then(() => {
            showToast('Token copied to clipboard', 'success');
        });
    }

    async function rotateBearerToken(id) {
        if (!await hwConfirm('Are you sure you want to regenerate the bearer token? Existing integrations using the old token will break immediately.', { title: 'Regenerate Token', okText: 'Regenerate' })) return;

        const btn = document.getElementById('rotate-btn');
        const input = document.getElementById('bearer-token-display');
        const originalHtml = btn.innerHTML;

        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Rotating...';

        try {
            const res = await fetch(`/endpoint/rotate-token/${id}`, {
                method: 'POST',
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-Type': 'application/json'
                }
            });
            const data = await res.json();

            if (data.status === 'success') {
                input.value = data.token;
                input.type = 'text';
                tokenLoaded = true;
                const icon = document.getElementById('token-eye-icon');
                setEyeIcon(icon, 'eye-slash');
                showToast('Token regenerated successfully!', 'success');
            } else {
                showToast('Rotation failed', 'danger');
            }
        } catch (e) {
            showToast('Error rotating token', 'danger');
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    }

    document.getElementById('endpoint-form').addEventListener('submit', (e) => {
        const requiredFields = ['name'];
        for (const id of requiredFields) {
            const el = document.getElementById(id);
            if (!el.value.trim()) {
                e.preventDefault();
                showToast(`${id.charAt(0).toUpperCase() + id.slice(1)} is required`, 'danger');
                el.focus();
                return;
            }
        }

        const jsonFields = ['json_mapping', 'routing_rules', 'maintenance_windows'];
        for (const id of jsonFields) {
            const val = document.getElementById(id).value.trim();
            if (val) {
                try {
                    JSON.parse(val);
                } catch (err) {
                    e.preventDefault();
                    showToast(`Invalid JSON in ${id.replace('_', ' ')}`, 'danger');
                    document.getElementById(id).focus();
                    return;
                }
            }
        }
    });

    // ---- Stand der Pflichtfelder in der Aktionsleiste --------------------
    // 5 Felder sind Pflicht; sie verteilen sich seit dem Tab-Umbau ueber
    // mehrere Panes. Ohne Rueckmeldung merkt man das erst beim Absenden --
    // den Tab-Sprung uebernimmt der invalid-Handler weiter unten.
    (function () {
        const anzeige = document.getElementById('hw-req-state');
        if (!anzeige) return;
        const form = anzeige.closest('form');
        if (!form) return;

        // checkVisibility fehlt aelteren Safari/Firefox -- Fallback statt
        // eines Absturzes beim ersten Tastendruck.
        const istSichtbar = (e) => (typeof e.checkVisibility === 'function'
            ? e.checkVisibility()
            : !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length));

        function pflichtfelder() {
            return [...form.querySelectorAll('[required]')].filter(
                (e) => e.type !== 'hidden');
        }

        function aktualisieren() {
            const alle = pflichtfelder();
            const offen = alle.filter((e) => !String(e.value || '').trim());
            const verborgen = offen.filter((e) => !istSichtbar(e));
            if (!offen.length) {
                anzeige.dataset.state = 'fertig';
                anzeige.textContent = 'All ' + alle.length + ' required fields filled';
            } else {
                anzeige.dataset.state = 'offen';
                anzeige.textContent = offen.length + ' of ' + alle.length
                    + ' required fields open'
                    + (verborgen.length ? ' (' + verborgen.length + ' hidden in advanced fields or another tab)' : '');
            }
            // Verborgene Pflichtfelder markieren, damit sie beim Aufklappen
            // sofort ins Auge fallen.
            alle.forEach((e) => {
                const feld = e.closest('.mb-3, .mb-4, .col-md-6, .col-12') || e;
                feld.classList.toggle('hw-req-hidden',
                    !String(e.value || '').trim() && !istSichtbar(e));
            });
        }

        form.addEventListener('input', aktualisieren);
        form.addEventListener('change', aktualisieren);
        aktualisieren();
    })();

    // ---- Pflichtfeld in einem inaktiven Tab -----------------------------
    // Chrome bricht den Submit ab, wenn das erste ungueltige Feld nicht
    // fokussierbar ist ("not focusable") -- mit dem Tab-Layout waere das
    // ein stiller Fehlschlag. Beim invalid-Ereignis wird der Tab des
    // Feldes synchron geoeffnet, dann greift der Browser-Fokus normal.
    (function () {
        const form = document.getElementById('endpoint-form');
        if (!form) return;
        form.addEventListener('invalid', (ev) => {
            const pane = ev.target.closest('.tab-pane');
            if (pane && !pane.classList.contains('active')) {
                const knopf = document.querySelector('.hw-formtabs [data-bs-target="#' + pane.id + '"]');
                if (knopf && window.bootstrap) {
                    bootstrap.Tab.getOrCreateInstance(knopf).show();
                } else {
                    // Ohne Bootstrap von Hand umschalten -- sonst bricht
                    // Chrome den Submit an einem unfokussierbaren Feld ab.
                    document.querySelectorAll('.hw-formtab-inhalt > .tab-pane.active')
                        .forEach((p) => p.classList.remove('active'));
                    document.querySelectorAll('.hw-formtabs .nav-link.active')
                        .forEach((k) => { k.classList.remove('active'); k.setAttribute('aria-selected', 'false'); });
                    pane.classList.add('active');
                    if (knopf) { knopf.classList.add('active'); knopf.setAttribute('aria-selected', 'true'); }
                }
            }
            const kasten = document.getElementById('advanced-fields');
            const schalter = document.getElementById('show-advanced');
            if (kasten && schalter && kasten.classList.contains('d-none')
                && kasten.contains(ev.target) && typeof toggleAdvanced === 'function') {
                schalter.checked = true;
                toggleAdvanced();
            }
        }, true);
    })();
