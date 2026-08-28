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
                    arrow.textContent = '→';
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
                resultPre.style.fontSize = '0.7rem';
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
            icon.classList.replace('fa-eye', 'fa-eye-slash');
        } else {
            input.type = 'password';
            icon.classList.replace('fa-eye-slash', 'fa-eye');
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
                if (icon) icon.classList.replace('fa-eye', 'fa-eye-slash');
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

