/* Endpoint card telemetry, filtering, and safe token-suffix search. */
(() => {
    'use strict';

    const STORAGE_KEY = 'hookwise.endpoint-filters.v2';
    const tokenPrefix = 'token:';
    const text = value => (value || '').toString().toLowerCase();
    const safeJson = (value, fallback) => { try { return JSON.parse(value) ?? fallback; } catch (_) { return fallback; } };

    function root() { return document.getElementById('endpoint-dashboard'); }
    function cards() { return [...document.querySelectorAll('.endpoint-card')]; }
    function getState() {
        const params = new URLSearchParams(window.location.search);
        const saved = safeJson(localStorage.getItem(STORAGE_KEY), {});
        return {
            q: params.get('q') ?? saved.q ?? '', board: params.get('board') ?? saved.board ?? '',
            status: params.get('status') ?? saved.status ?? '', quick: params.get('filter') ?? saved.quick ?? '',
        };
    }
    function setSelectOptions(select, values, label) {
        if (!select) return;
        const selected = select.value;
        select.replaceChildren(new Option(label, ''));
        [...values].sort((a, b) => a.localeCompare(b)).forEach(value => select.add(new Option(value, value)));
        select.value = [...select.options].some(option => option.value === selected) ? selected : '';
    }
    function humanAge(days) {
        if (days === null || days === undefined) return '—';
        return days < 1 ? '<1 day' : `${Math.round(days)} days`;
    }
    function setMetric(card, name, value, title = '') {
        const el = card.querySelector(`[data-summary="${name}"]`);
        if (!el) return;
        el.textContent = value;
        el.title = title;
    }
    function hydrateCard(card, summary) {
        const summaryTags = Array.isArray(summary.tags) ? summary.tags : [];
        Object.entries({
            board: summary.board, company: summary.company, status: summary.status,
            health: summary.health, stale: String(summary.is_stale), unhealthy: String(summary.is_unhealthy),
            pinned: String(summary.is_pinned), draft: String(summary.is_draft), inactive: String(summary.activity_count === 0),
            highLatency: String((summary.average_latency || 0) >= 2), tags: summaryTags.join('|'),
            lastFailure: summary.last_failure_at || '',
        }).forEach(([key, value]) => { card.dataset[key] = String(value || ''); });
        setMetric(card, 'token-age', humanAge(summary.token_age_days));
        setMetric(card, 'last-success', summary.last_success_at ? new Date(summary.last_success_at).toLocaleString() : 'Never');
        setMetric(card, 'last-failure', summary.last_failure_at ? new Date(summary.last_failure_at).toLocaleString() : 'None');
        setMetric(card, 'latency', summary.last_response_time == null ? '—' : `${summary.last_response_time}s`);
        setMetric(card, 'queue', String(summary.queue_depth));
        setMetric(card, 'retries', String(summary.retry_count));
        setMetric(card, 'uptime', summary.uptime == null ? '—' : `${summary.uptime}%`);
        const tags = card.querySelector('[data-summary="tags"]');
        if (tags) {
            tags.replaceChildren();
            summaryTags.forEach(tag => { const item = document.createElement('span'); item.className = 'endpoint-tag'; item.textContent = tag; tags.append(item); });
            if (!summaryTags.length) tags.textContent = 'No tags';
        }
    }
    function updateChips(state, count) {
        const chips = document.getElementById('endpoint-filter-chips');
        const result = document.getElementById('endpoint-result-count');
        if (result) result.textContent = `${count} endpoint${count === 1 ? '' : 's'}`;
        if (!chips) return;
        chips.replaceChildren();
        [['q', state.q], ['board', state.board], ['status', state.status], ['filter', state.quick]].filter(([, value]) => value).forEach(([key, value]) => {
            const button = document.createElement('button');
            button.type = 'button'; button.className = 'filter-chip'; button.dataset.removeFilter = key;
            button.textContent = `${key === 'q' ? 'Search' : key}: ${value} ×`;
            chips.append(button);
        });
    }
    async function fetchTokenMatches(query) {
        if (!query.startsWith(tokenPrefix)) return null;
        const suffix = query.slice(tokenPrefix.length).trim();
        if (suffix.length !== 4) return new Set();
        const response = await fetch(`/api/endpoints/summary?token_suffix=${encodeURIComponent(suffix)}`);
        if (!response.ok) throw new Error('Token suffix search unavailable');
        return new Set((await response.json()).token_matches || []);
    }
    async function applyFilters({ tokenMatches = null } = {}) {
        const dashboard = root(); if (!dashboard) return;
        const q = document.getElementById('endpoint-search')?.value.trim() || '';
        const state = {
            q, board: document.getElementById('board-filter')?.value || '',
            status: document.getElementById('status-filter')?.value || '', quick: document.getElementById('endpoint-quick-filter')?.value || '',
        };
        if (q.startsWith(tokenPrefix) && tokenMatches === null) {
            try { tokenMatches = await fetchTokenMatches(q); }
            catch (_) { tokenMatches = new Set(); window.showToast?.('Token suffix search could not be completed.', 'danger'); }
        }
        const term = text(q.startsWith(tokenPrefix) ? '' : q);
        let count = 0;
        cards().forEach(card => {
            const matchesText = !term || [card.dataset.name, card.dataset.id, card.dataset.board, card.dataset.company, card.dataset.tags, `/w/${card.dataset.id}`].some(value => text(value).includes(term));
            const matchBoard = !state.board || card.dataset.board === state.board;
            const matchStatus = !state.status || card.dataset.status === state.status;
            const quick = {
                recent_failures: Boolean(card.dataset.lastFailure), stale: card.dataset.stale === 'true', unhealthy: card.dataset.unhealthy === 'true',
                high_latency: card.dataset.highLatency === 'true', drafts: card.dataset.draft === 'true', pinned: card.dataset.pinned === 'true', inactive: card.dataset.inactive === 'true',
            };
            const matchQuick = !state.quick || quick[state.quick];
            const matchToken = !q.startsWith(tokenPrefix) || Boolean(tokenMatches?.has(card.dataset.id));
            const visible = matchesText && matchBoard && matchStatus && matchQuick && matchToken;
            const column = card.closest('.draggable-card');
            if (column) column.hidden = !visible;
            if (visible) count += 1;
        });
        const empty = document.getElementById('endpoint-filter-empty');
        if (empty) empty.hidden = count !== 0;
        updateChips(state, count);
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
            const params = new URLSearchParams(window.location.search);
            [['q', state.q], ['board', state.board], ['status', state.status], ['filter', state.quick]].forEach(([key, value]) => value ? params.set(key, value) : params.delete(key));
            history.replaceState(null, '', `${location.pathname}${params.toString() ? `?${params}` : ''}`);
        } catch (_) { /* Storage/history are enhancements only. */ }
    }
    async function refreshSummaries() {
        const dashboard = root(); if (!dashboard || dashboard.dataset.loading === 'true') return;
        dashboard.dataset.loading = 'true'; dashboard.classList.add('is-loading');
        try {
            const response = await fetch('/api/endpoints/summary');
            if (!response.ok) throw new Error('Endpoint summary request failed');
            const payload = await response.json();
            const summaries = new Map((payload.endpoints || []).map(item => [item.id, item]));
            cards().forEach(card => { const summary = summaries.get(card.dataset.id); if (summary) hydrateCard(card, summary); });
            setSelectOptions(document.getElementById('board-filter'), new Set((payload.endpoints || []).map(item => item.board).filter(Boolean)), 'All Boards');
            await applyFilters();
        } catch (error) {
            const errorEl = document.getElementById('endpoint-summary-error');
            if (errorEl) { errorEl.hidden = false; errorEl.textContent = 'Endpoint telemetry is temporarily unavailable. You can still use endpoint actions.'; }
        } finally { dashboard.classList.remove('is-loading'); delete dashboard.dataset.loading; }
    }
    function init() {
        const dashboard = root(); if (!dashboard || dashboard.dataset.endpointDashboardInit) return;
        dashboard.dataset.endpointDashboardInit = 'true';
        const state = getState();
        const search = document.getElementById('endpoint-search');
        const board = document.getElementById('board-filter'); const status = document.getElementById('status-filter'); const quick = document.getElementById('endpoint-quick-filter');
        if (search) search.value = state.q; if (board) board.value = state.board; if (status) status.value = state.status; if (quick) quick.value = state.quick;
        let timer;
        const schedule = () => { clearTimeout(timer); timer = setTimeout(() => applyFilters(), 180); };
        [search, board, status, quick].filter(Boolean).forEach(control => control.addEventListener(control === search ? 'input' : 'change', schedule));
        document.getElementById('clear-endpoint-filters')?.addEventListener('click', () => { if (search) search.value = ''; if (board) board.value = ''; if (status) status.value = ''; if (quick) quick.value = ''; applyFilters(); });
        document.getElementById('endpoint-filter-chips')?.addEventListener('click', event => { const key = event.target.closest('[data-remove-filter]')?.dataset.removeFilter; if (!key) return; const control = key === 'q' ? search : key === 'board' ? board : key === 'status' ? status : quick; if (control) control.value = ''; applyFilters(); });
        document.getElementById('refresh-endpoints')?.addEventListener('click', refreshSummaries);
        refreshSummaries();
    }
    document.addEventListener('DOMContentLoaded', init);
    document.addEventListener('htmx:load', init);
    window.endpointDashboard = { applyFilters, refreshSummaries };
})();
