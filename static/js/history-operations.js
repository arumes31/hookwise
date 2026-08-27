/* Saved searches, advanced history filtering, and operations controls. */
(() => {
    'use strict';

    const fields = {
        'date-from': 'date_from', 'date-to': 'date_to', 'endpoint-filter': 'endpoint_id',
        'status-filter': 'status', 'history-search': 'search', 'history-ticket': 'ticket',
        'history-request-id': 'request_id', 'history-error-type': 'error_type',
        'history-http-status': 'http_status', 'history-min-processing': 'min_processing',
        'history-max-processing': 'max_processing', 'history-min-retry': 'min_retry',
        'history-max-retry': 'max_retry',
    };
    let diagnostic = null;

    const root = () => document.getElementById('history-operations');
    const notice = (message, kind = 'info') => window.showToast?.(message, kind);
    const value = id => document.getElementById(id)?.value.trim() || '';
    const collect = () => {
        const values = {};
        Object.entries(fields).forEach(([id, key]) => { const current = value(id); if (current) values[key] = current; });
        if (document.getElementById('history-dlq-only')?.checked) values.dlq_only = 'true';
        return values;
    };
    async function api(url, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            options.headers = { ...(options.headers || {}), 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || '' };
        }
        const response = await fetch(url, options);
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status})`);
        return body;
    }
    function setValues(values) {
        Object.entries(fields).forEach(([id, key]) => { const el = document.getElementById(id); if (el) el.value = values[key] || ''; });
        const dlq = document.getElementById('history-dlq-only'); if (dlq) dlq.checked = values.dlq_only === 'true' || values.dlq_only === true;
    }
    async function applyFilters() {
        const values = collect(); const params = new URLSearchParams(values);
        try {
            const result = await api(`/api/history/advanced?${params}`);
            document.getElementById('history-advanced-count').textContent = `${result.total} matching request${result.total === 1 ? '' : 's'} · page ${result.page} of ${result.pages || 1}`;
            history.replaceState(null, '', `${location.pathname}?${params}`);
            window.location.assign(`${location.pathname}?${params}`);
        } catch (error) { notice(error.message, 'danger'); }
    }
    async function loadSaved() {
        const select = document.getElementById('history-saved-searches'); if (!select) return;
        select.replaceChildren(new Option('Saved searches', ''));
        try { (await api('/api/history/saved-searches')).forEach(item => { const option = new Option(item.name, item.id); option.dataset.filters = JSON.stringify(item.filters); select.add(option); }); }
        catch (_) { select.disabled = true; }
    }
    async function saveSearch() {
        const name = window.prompt('Name this history search:'); if (!name) return;
        try {
            const saved = await api('/api/history/saved-searches', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, filters: collect() }) });
            notice('History search saved.', 'success');
            await loadSaved();
            document.getElementById('history-saved-searches').value = saved.id;
            document.getElementById('history-delete-search').disabled = false;
        } catch (error) { notice(error.message, 'danger'); }
    }
    async function deleteSearch() {
        const select = document.getElementById('history-saved-searches');
        const id = select?.value;
        if (!id || !await window.hwConfirm?.('Delete this saved history search?', { title: 'Delete saved search', okText: 'Delete' })) return;
        try {
            await api(`/api/history/saved-searches/${encodeURIComponent(id)}`, { method: 'DELETE' });
            await loadSaved();
            document.getElementById('history-delete-search').disabled = true;
            notice('Saved search deleted.', 'success');
        } catch (error) { notice(error.message, 'danger'); }
    }
    async function retry(id) {
        try { await api(`/api/history/${encodeURIComponent(id)}/retry`, { method: 'POST' }); notice('Retry queued.', 'success'); }
        catch (error) { notice(error.message, 'danger'); }
    }
    function replaceList(id, items, format) {
        const target = document.getElementById(id); if (!target) return;
        target.replaceChildren();
        (items || []).forEach(item => { const row = document.createElement('li'); row.textContent = format(item); target.append(row); });
        if (!(items || []).length) target.textContent = 'None recorded.';
    }
    async function showDiagnostics(id) {
        try {
            diagnostic = await api(`/api/history/${encodeURIComponent(id)}/diagnostics`);
            const log = diagnostic.log || {};
            document.getElementById('diagnostics-summary').textContent = `Request ${log.request_id || id} · correlation ${log.correlation_id || 'not recorded'} · received ${log.received_at || log.created_at || 'unknown'}`;
            replaceList('diagnostics-timeline', diagnostic.timeline, item => `${item.event}: ${new Date(item.at).toLocaleString()}`);
            replaceList('diagnostics-retries', diagnostic.retry_attempts, item => `Attempt ${item.attempt_number}: ${item.status} · ${item.retry_interval_seconds || 0}s interval`);
            document.getElementById('diagnostics-errors').textContent = JSON.stringify(diagnostic.error_chain || [], null, 2);
            document.getElementById('diagnostics-retry').dataset.logId = id;
            document.getElementById('diagnostics-replay-edit').dataset.logId = id;
            bootstrap.Modal.getOrCreateInstance(document.getElementById('diagnosticsModal')).show();
        } catch (error) { notice(error.message, 'danger'); }
    }
    function downloadDiagnostics() {
        if (!diagnostic) return;
        const blob = new Blob([JSON.stringify(diagnostic, null, 2)], { type: 'application/json' });
        const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'hookwise-diagnostics.json'; link.click(); URL.revokeObjectURL(link.href);
    }
    async function replayWithEdits(id) {
        const raw = window.prompt('Paste replacement JSON to replay. Original request bodies are not exposed here.', '{}');
        if (raw === null) return;
        let payload; try { payload = JSON.parse(raw); } catch (_) { return notice('Replacement JSON is invalid.', 'danger'); }
        if (!payload || typeof payload !== 'object') return notice('Replacement JSON must be an object or array.', 'danger');
        try { await api(`/api/history/${encodeURIComponent(id)}/replay-edits`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload }) }); notice('Edited replay queued.', 'success'); }
        catch (error) { notice(error.message, 'danger'); }
    }
    async function replayDlq() {
        const ids = [...document.querySelectorAll('tr[data-history-status="dlq"] .log-check:checked')].map(box => box.dataset.id);
        if (!ids.length) return notice('Select dead-lettered requests first.');
        if (!await window.hwConfirm?.(`Replay ${ids.length} dead-lettered request(s)?`, { title: 'Replay dead letters', okText: 'Replay' })) return;
        try { const result = await api('/api/history/dlq/replay', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) }); notice(`${result.queued.length} replay(s) queued.`, 'success'); }
        catch (error) { notice(error.message, 'danger'); }
    }
    async function loadOperations() {
        try {
            const data = await api('/api/history/operations');
            const alert = document.getElementById('history-retry-alert');
            if (alert) { alert.hidden = !data.retry_exhausted_last_7d; alert.textContent = `${data.retry_exhausted_last_7d} retry exhaustion${data.retry_exhausted_last_7d === 1 ? '' : 's'} in 7d`; }
            const replay = document.getElementById('history-replay-dlq'); if (replay) replay.hidden = data.dead_letter_queue === 0;
            const limits = (data.endpoint_rate_limits || []).map(limit => `${limit.name}: ${limit.rate_limit_per_minute}/min`).join(' · ');
            const quota = data.quota_available ? `ConnectWise quota: ${JSON.stringify(data.connectwise_quota)}` : 'ConnectWise quota unavailable';
            document.getElementById('history-rate-limits').textContent = `${limits || 'No endpoint rate limits'} · ${quota}`;
        } catch (error) { document.getElementById('history-rate-limits').textContent = `Operations unavailable: ${error.message}`; }
    }
    function init() {
        if (!root() || root().dataset.initialized) return; root().dataset.initialized = 'true';
        setValues(Object.fromEntries(new URLSearchParams(location.search)));
        document.getElementById('history-apply-advanced')?.addEventListener('click', applyFilters);
        document.getElementById('history-clear-advanced')?.addEventListener('click', () => { setValues({}); applyFilters(); });
        document.getElementById('history-save-search')?.addEventListener('click', saveSearch);
        document.getElementById('history-delete-search')?.addEventListener('click', deleteSearch);
        document.getElementById('history-saved-searches')?.addEventListener('change', event => {
            const option = event.target.selectedOptions[0];
            document.getElementById('history-delete-search').disabled = !event.target.value;
            if (option?.dataset.filters) { setValues(JSON.parse(option.dataset.filters)); applyFilters(); }
        });
        document.addEventListener('click', event => { const retryButton = event.target.closest('.history-retry'); const diagnosticsButton = event.target.closest('.history-diagnostics'); if (retryButton) retry(retryButton.dataset.logId); if (diagnosticsButton) showDiagnostics(diagnosticsButton.dataset.logId); });
        document.getElementById('history-replay-dlq')?.addEventListener('click', replayDlq);
        document.getElementById('diagnostics-download')?.addEventListener('click', downloadDiagnostics);
        document.getElementById('diagnostics-retry')?.addEventListener('click', event => retry(event.currentTarget.dataset.logId));
        document.getElementById('diagnostics-replay-edit')?.addEventListener('click', event => replayWithEdits(event.currentTarget.dataset.logId));
        loadSaved(); loadOperations();
    }
    document.addEventListener('DOMContentLoaded', init);
    document.addEventListener('htmx:load', init);
})();
