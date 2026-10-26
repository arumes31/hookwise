(() => {
    const initialize = () => {
        const targets = document.getElementById('forwarding-targets');
        const add = document.getElementById('add-forwarding-target');
        const score = document.getElementById('score_enabled');
        const severity = document.getElementById('severity_enabled');
        const status = document.getElementById('status_enabled');
        if (!targets || !add || targets.dataset.initialized) return;
        targets.dataset.initialized = 'true';

        const updateTargets = () => {
            const rows = targets.querySelectorAll('[data-target-row]');
            rows.forEach((row, index) => {
                row.querySelector('[name="target_url"]').setAttribute('aria-label', `Ziel-URL ${index + 1}`);
                const label = row.querySelector('[data-target-label]');
                if (label) label.textContent = `Ziel ${index + 1}:`;
                const remove = row.querySelector('[data-remove-target]');
                remove.disabled = rows.length === 1;
                remove.setAttribute('aria-label', `Ziel-URL ${index + 1} entfernen`);
            });
            add.disabled = rows.length >= 10;
        };
        add.addEventListener('click', () => {
            if (targets.children.length >= 10) return;
            const row = targets.firstElementChild.cloneNode(true);
            row.querySelector('[name="target_slot"]').value = 'new';
            for (const control of row.querySelectorAll('[data-target-summary], [data-edit-target], [data-cancel-target]')) {
                control.remove();
            }
            const input = row.querySelector('[name="target_url"]');
            input.value = '';
            input.hidden = false;
            input.required = true;
            row.querySelector('[data-target-error]').hidden = true;
            row.querySelector('[data-target-error]').textContent = '';
            targets.append(row);
            updateTargets();
            input.focus();
        });
        targets.addEventListener('click', async (event) => {
            if (!(event.target instanceof Element)) return;
            const row = event.target.closest('[data-target-row]');
            if (!row) return;
            const remove = event.target.closest('[data-remove-target]');
            if (remove) {
                if (targets.children.length <= 1) return;
                row.remove();
                updateTargets();
                add.focus();
                return;
            }
            const input = row.querySelector('[name="target_url"]');
            const edit = row.querySelector('[data-edit-target]');
            const cancel = row.querySelector('[data-cancel-target]');
            const summary = row.querySelector('[data-target-summary]');
            const error = row.querySelector('[data-target-error]');
            if (event.target.closest('[data-cancel-target]')) {
                input.value = '';
                input.hidden = true;
                input.required = false;
                cancel.hidden = true;
                summary.hidden = false;
                edit.hidden = false;
                edit.focus();
                return;
            }
            if (!event.target.closest('[data-edit-target]')) return;
            error.hidden = true;
            edit.disabled = true;
            try {
                if (edit.dataset.revealUrl) {
                    const csrf = targets.closest('form').querySelector('[name="csrf_token"]').value;
                    const response = await fetch(edit.dataset.revealUrl, {
                        method: 'POST',
                        credentials: 'same-origin',
                        cache: 'no-store',
                        headers: { 'X-CSRFToken': csrf, 'Accept': 'application/json' },
                    });
                    if (!response.ok) throw new Error('reveal-failed');
                    const data = await response.json();
                    if (typeof data.url !== 'string') throw new Error('invalid-response');
                    input.value = data.url;
                }
                input.hidden = false;
                input.required = true;
                summary.hidden = true;
                edit.hidden = true;
                cancel.hidden = false;
                input.focus();
            } catch (_error) {
                // Do not reflect a server/proxy error that may contain secrets.
                error.textContent = 'Ziel-URL konnte nicht geladen werden. Bitte neu anmelden oder erneut versuchen.';
                error.hidden = false;
            } finally {
                edit.disabled = false;
            }
        });
        score?.addEventListener('change', () => {
            for (const [fieldId, inputId] of [['score-path-field', 'score_path'], ['score-minimum-field', 'minimum_score']]) {
                document.getElementById(fieldId).hidden = !score.checked;
                const input = document.getElementById(inputId);
                input.disabled = !score.checked;
                input.required = score.checked;
            }
            if (score.checked && !document.getElementById('score_path').value) {
                document.getElementById('score_path').value = '$.score';
            }
        });
        severity?.addEventListener('change', () => {
            for (const [fieldId, inputId] of [['severity-path-field', 'severity_path'], ['severity-minimum-field', 'minimum_severity']]) {
                document.getElementById(fieldId).hidden = !severity.checked;
                const input = document.getElementById(inputId);
                input.disabled = !severity.checked;
                input.required = severity.checked;
            }
            if (severity.checked && !document.getElementById('severity_path').value) {
                document.getElementById('severity_path').value = '$.Results[*].Severity';
            }
        });
        status?.addEventListener('change', () => {
            document.getElementById('status-path-field').hidden = !status.checked;
            const input = document.getElementById('status_path');
            input.disabled = !status.checked;
            input.required = status.checked;
            if (status.checked && !input.value) input.value = '$.Results[*].Status';
        });
        const format = document.getElementById('payload_format');
        const routingKey = document.getElementById('pagerduty_routing_key');
        const clearKey = document.getElementById('clear_pagerduty_routing_key');
        const updatePayloadFormat = () => {
            if (!format || !routingKey) return;
            const pagerduty = format.value === 'pagerduty';
            document.getElementById('pagerduty-key-field').hidden = !pagerduty;
            document.getElementById('pagerduty-target-help').hidden = !pagerduty;
            routingKey.disabled = !pagerduty;
            routingKey.required = pagerduty && (routingKey.dataset.hasKey !== 'true' || Boolean(clearKey?.checked));
            if (clearKey) clearKey.disabled = !pagerduty;
            for (const section of targets.closest('form').querySelectorAll('[data-hookwise-auth]')) {
                section.hidden = pagerduty;
                for (const input of section.querySelectorAll('input')) input.disabled = pagerduty;
            }
        };
        format?.addEventListener('change', updatePayloadFormat);
        clearKey?.addEventListener('change', updatePayloadFormat);
        updatePayloadFormat();
        updateTargets();
    };

    // Forms can arrive through hx-boost after DOMContentLoaded. Bind each new
    // form once, even if its script is evaluated again after a navigation.
    if (!document.documentElement.dataset.forwardingFormListeners) {
        document.documentElement.dataset.forwardingFormListeners = 'true';
        document.addEventListener('DOMContentLoaded', initialize);
        document.addEventListener('htmx:afterSwap', initialize);
    }
    initialize();
})();
