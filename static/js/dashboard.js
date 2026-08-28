(() => {
    'use strict';

    const storageKey = 'hookwise.dashboard.layout.v1';
    const DEFAULT_REFRESH_INTERVAL = 30;
    const state = { range: '24h', hidden: [], order: [], compact: false, interval: DEFAULT_REFRESH_INTERVAL, timer: null, points: [], zoom: 0, pan: 0 };
    let saveTimer = null;
    let activeRoot = null;
    let refreshController = null;
    const number = new Intl.NumberFormat();

    const byId = id => document.getElementById(id);
    const local = {
        get() { try { return JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch (_) { return {}; } },
        set(value) { try { localStorage.setItem(storageKey, JSON.stringify(value)); } catch (_) {} }
    };
    const fmt = value => typeof value === 'number' ? number.format(value) : '—';
    const duration = value => `${Number(value || 0).toFixed(value < 1 ? 3 : 2)} s`;

    function query() {
        const params = new URLSearchParams({ range: state.range, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' });
        if (state.range === 'custom') {
            const from = byId('dashboard-from')?.value;
            const to = byId('dashboard-to')?.value;
            if (from && to) { params.set('from', new Date(from).toISOString()); params.set('to', new Date(to).toISOString()); }
        }
        return params.toString();
    }

    async function getJson(path, signal) {
        const response = await fetch(`${path}?${query()}`, { headers: { Accept: 'application/json' }, credentials: 'same-origin', signal });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || 'Dashboard data could not be loaded.');
        return body;
    }

    function showError(message, outage) {
        const error = byId('dashboard-error');
        const banner = byId('dashboard-outage');
        if (error) {
            error.textContent = message;
            error.classList.toggle('d-none', outage);
        }
        banner?.classList.toggle('d-none', !outage);
        const outageMessage = byId('dashboard-outage-message');
        if (outage && outageMessage) outageMessage.textContent = message;
    }

    function clearError() { byId('dashboard-error')?.classList.add('d-none'); byId('dashboard-outage')?.classList.add('d-none'); }

    function renderKpis(data) {
        const kpis = data.kpis || {};
        document.querySelectorAll('.dashboard-kpi').forEach(card => {
            const key = card.dataset.kpi;
            const value = kpis[key];
            const output = card.querySelector('.dashboard-kpi-value');
            output.textContent = key === 'success_rate' ? `${Number(value || 0).toFixed(1)}%` : key === 'average_latency' ? duration(value) : fmt(value);
            const delta = data.deltas && data.deltas[key];
            const deltaText = delta === null || delta === undefined ? 'No prior comparison' : `${delta > 0 ? '↑' : delta < 0 ? '↓' : '→'} ${Math.abs(delta).toFixed(1)}% vs previous period`;
            const detail = card.querySelector('.dashboard-kpi-delta');
            detail.textContent = deltaText;
            detail.className = `dashboard-kpi-delta ${delta > 0 && ['failed_events', 'dead_letter_queue', 'average_latency'].includes(key) ? 'is-negative' : delta < 0 && ['failed_events', 'dead_letter_queue', 'average_latency'].includes(key) ? 'is-positive' : ''}`;
            card.classList.remove('is-loading');
            card.hidden = state.hidden.includes(key);
        });
        byId('dashboard-updated').dateTime = data.updated_at;
        byId('dashboard-updated').textContent = new Date(data.updated_at).toLocaleString();
        state.kpis = data.kpis;
        state.filters = data.filters || {};
    }

    function visiblePoints() {
        const points = state.points;
        if (!points.length) return points;
        const count = Math.max(3, Math.ceil(points.length / (state.zoom + 1)));
        const maxStart = Math.max(0, points.length - count);
        const start = Math.min(maxStart, Math.max(0, state.pan));
        return points.slice(start, start + count);
    }

    function renderSvg(points) {
        const host = byId('dashboard-chart');
        const enabled = [...document.querySelectorAll('#dashboard-legend input:checked')].map(input => input.value);
        if (!points.length || !enabled.length) { host.replaceChildren(); return; }
        const width = 920, height = 260, pad = 28;
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('preserveAspectRatio', 'none');
        const colors = { volume: '#38bdf8', successful: '#10b981', failed: '#f43f5e', failure_rate: '#fbbf24', p95: '#c084fc' };
        const max = Math.max(1, ...enabled.flatMap(key => points.map(point => Number(point[key] || 0))));
        if (enabled.includes('successful') || enabled.includes('failed')) {
            points.forEach((point, index) => {
                const x = pad + index * ((width - pad * 2) / Math.max(1, points.length - 1));
                const barWidth = Math.max(3, (width - pad * 2) / Math.max(1, points.length) * .42);
                let y = height - pad;
                [['successful', '#10b981'], ['failed', '#f43f5e']].forEach(([key, color]) => {
                    if (!enabled.includes(key)) return;
                    const barHeight = Number(point[key] || 0) / max * (height - pad * 2); y -= barHeight;
                    const rect = document.createElementNS(ns, 'rect'); rect.setAttribute('x', x - barWidth / 2); rect.setAttribute('y', y); rect.setAttribute('width', barWidth); rect.setAttribute('height', barHeight); rect.setAttribute('fill', color); rect.setAttribute('opacity', '.55');
                    const title = document.createElementNS(ns, 'title'); title.textContent = `${point.label}: ${key} ${point[key]}`; rect.append(title); svg.append(rect);
                });
            });
        }
        enabled.forEach(key => {
            if (key === 'successful' || key === 'failed') return;
            const path = document.createElementNS(ns, 'polyline');
            const series = points.map((point, index) => `${pad + index * ((width - pad * 2) / Math.max(1, points.length - 1))},${height - pad - Number(point[key] || 0) / max * (height - pad * 2)}`).join(' ');
            path.setAttribute('points', series); path.setAttribute('fill', 'none'); path.setAttribute('stroke', colors[key]); path.setAttribute('stroke-width', key === 'failure_rate' ? '2' : '3'); svg.append(path);
        });
        points.forEach((point, index) => {
            const x = pad + index * ((width - pad * 2) / Math.max(1, points.length - 1));
            const dot = document.createElementNS(ns, 'circle'); dot.setAttribute('cx', x); dot.setAttribute('cy', height - pad); dot.setAttribute('r', point.anomaly ? '5' : '2'); dot.setAttribute('fill', point.anomaly ? '#fbbf24' : '#94a3b8');
            const title = document.createElementNS(ns, 'title'); title.textContent = `${point.label}: ${point.volume} events, ${point.failed} failed (${point.failure_rate}%); p95 ${point.p95}s${point.busiest ? '; busiest period' : ''}${point.highest_failure ? '; highest failure period' : ''}${point.anomaly ? '; anomaly detected' : ''}`; dot.append(title); svg.append(dot);
        });
        host.replaceChildren(svg);
    }

    function renderAnalytics(data) {
        state.points = data.points || [];
        state.pan = 0;
        byId('dashboard-timezone').textContent = data.timezone || 'UTC';
        const points = visiblePoints();
        byId('dashboard-chart-empty').classList.toggle('d-none', points.length > 0);
        renderSvg(points);
        const table = byId('dashboard-chart-table'); table.replaceChildren();
        points.forEach(point => {
            const row = document.createElement('tr');
            const heading = document.createElement('th');
            heading.scope = 'row';
            heading.textContent = `${point.label}${point.anomaly ? ' ⚠' : ''}`;
            const cells = [
                fmt(point.volume), fmt(point.successful), fmt(point.failed), `${point.failure_rate}%`,
                duration(point.p50), duration(point.p95), duration(point.p99)
            ].map(value => {
                const cell = document.createElement('td');
                cell.textContent = value;
                return cell;
            });
            row.append(heading, ...cells);
            table.append(row);
        });
        const activity = byId('dashboard-endpoint-activity'); activity.replaceChildren();
        const endpointRows = data.endpoint_activity || [];
        const endpointMax = Math.max(1, ...endpointRows.map(endpoint => endpoint.processed));
        endpointRows.forEach(endpoint => {
            const item = document.createElement('span'); item.className = 'dashboard-endpoint-summary'; item.style.setProperty('--activity-width', `${endpoint.processed / endpointMax * 100}%`); item.title = `${endpoint.processed} events; ${endpoint.failed} failed`; item.textContent = `${endpoint.name}: ${endpoint.processed} / ${endpoint.failed} failed`; activity.append(item);
        });
    }

    function applyEndpointFilter(key) {
        const cards = document.querySelectorAll('.endpoint-card');
        const ids = new Set(state.filters && state.filters[key] || []);
        cards.forEach(card => {
            const matches = ids.has(card.dataset.id);
            const wrapper = card.closest('.draggable-card, .col-xl-6, .col-md-6, .col-12'); if (wrapper) wrapper.style.display = matches ? '' : 'none';
        });
        byId('endpoint-list').scrollIntoView({ behavior: 'smooth', block: 'start' });
        const search = byId('endpoint-search'); if (search) search.placeholder = `Dashboard filter: ${key.replaceAll('_', ' ')} (clear search/filter to reset)`;
    }

    function applyKpiOrder() {
        const grid = byId('dashboard-kpis');
        const cards = [...grid.querySelectorAll('.dashboard-kpi')];
        if (!state.order.length) state.order = cards.map(card => card.dataset.kpi);
        cards.sort((left, right) => state.order.indexOf(left.dataset.kpi) - state.order.indexOf(right.dataset.kpi)).forEach(card => grid.append(card));
    }
    function preferencePayload() {
        return { layout: state.order, hidden: state.hidden, compact: state.compact, interval: state.interval,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', activity_buffer_size: 200,
            browser_notifications_enabled: false, sound_notifications_enabled: false };
    }
    function persist() {
        local.set({ hidden: state.hidden, order: state.order, compact: state.compact, interval: state.interval });
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => fetch('/api/dashboard/preferences', {
            method: 'PATCH', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify(preferencePayload())
        }).catch(() => {}), 250);
    }
    async function loadPreferences() {
        try {
            const response = await fetch('/api/dashboard/preferences', { credentials: 'same-origin', headers: { Accept: 'application/json' } });
            if (!response.ok) return;
            const saved = await response.json();
            Object.assign(state, { hidden: saved.hidden || [], order: saved.layout || [], compact: Boolean(saved.compact), interval: Number(saved.interval ?? DEFAULT_REFRESH_INTERVAL) });
            local.set({ hidden: state.hidden, order: state.order, compact: state.compact, interval: state.interval });
        } catch (_) {}
    }
    function renderToggleMenu() {
        const target = byId('dashboard-kpi-toggles'); target.replaceChildren();
        document.querySelectorAll('.dashboard-kpi').forEach(card => {
            const label = document.createElement('label'); const input = document.createElement('input'); input.type = 'checkbox'; input.checked = !state.hidden.includes(card.dataset.kpi); input.addEventListener('change', () => { state.hidden = input.checked ? state.hidden.filter(key => key !== card.dataset.kpi) : [...state.hidden, card.dataset.kpi]; card.hidden = !input.checked; persist(); });
            label.append(input, ` ${card.querySelector('.dashboard-kpi-label').textContent}`); target.append(label);
        });
    }

    function isActive(root) {
        return Boolean(root?.isConnected && root === activeRoot && root === byId('operations-dashboard'));
    }
    function cleanup(root = activeRoot) {
        if (root !== activeRoot) return;
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
        refreshController?.abort();
        refreshController = null;
        activeRoot = null;
        document.body.classList.remove('dashboard-compact');
    }
    function schedule() {
        if (state.timer) clearInterval(state.timer);
        state.timer = state.interval && activeRoot ? setInterval(refresh, state.interval * 1000) : null;
    }
    async function refresh() {
        const root = activeRoot;
        if (!isActive(root)) return;
        refreshController?.abort();
        const controller = new AbortController();
        refreshController = controller;
        const button = byId('dashboard-refresh'); if (button) button.disabled = true;
        document.querySelectorAll('.dashboard-kpi').forEach(card => card.classList.add('is-loading'));
        try {
            const [overview, analytics] = await Promise.all([
                getJson('/api/dashboard/overview', controller.signal),
                getJson('/api/dashboard/analytics', controller.signal)
            ]);
            if (!isActive(root) || controller.signal.aborted) return;
            renderKpis(overview); renderAnalytics(analytics); clearError();
        } catch (error) {
            if (error?.name !== 'AbortError' && isActive(root)) showError(error?.message || 'Dashboard data could not be loaded.', true);
        } finally {
            if (refreshController === controller) refreshController = null;
            if (isActive(root)) {
                if (button) button.disabled = false;
                document.querySelectorAll('.dashboard-kpi').forEach(card => card.classList.remove('is-loading'));
            }
        }
    }

    async function init() {
        const root = byId('operations-dashboard');
        if (!root || root.dataset.dashboardReady) return;
        cleanup();
        activeRoot = root;
        root.dataset.dashboardReady = 'true';
        Object.assign(state, local.get());
        await loadPreferences();
        if (!isActive(root)) return;
        byId('dashboard-range').value = state.range;
        byId('dashboard-refresh-interval').value = String(state.interval);
        document.body.classList.toggle('dashboard-compact', Boolean(state.compact));
        byId('dashboard-compact').setAttribute('aria-pressed', String(Boolean(state.compact)));
        document.querySelectorAll('.dashboard-kpi').forEach(card => {
            card.hidden = state.hidden.includes(card.dataset.kpi); card.addEventListener('click', () => applyEndpointFilter(card.dataset.kpi));
            card.draggable = true;
            card.addEventListener('dragstart', event => event.dataTransfer.setData('text/plain', card.dataset.kpi));
            card.addEventListener('dragover', event => event.preventDefault());
            card.addEventListener('drop', event => { event.preventDefault(); const moved = event.dataTransfer.getData('text/plain'); state.order = state.order.filter(key => key !== moved); state.order.splice(state.order.indexOf(card.dataset.kpi), 0, moved); applyKpiOrder(); persist(); });
        });
        applyKpiOrder();
        renderToggleMenu();
        byId('dashboard-refresh').addEventListener('click', refresh);
        byId('dashboard-range').addEventListener('change', event => { state.range = event.target.value; byId('dashboard-custom-range').hidden = state.range !== 'custom'; persist(); if (state.range !== 'custom') refresh(); });
        byId('dashboard-custom-range').addEventListener('click', () => { byId('dashboard-custom-form').hidden = false; });
        byId('dashboard-custom-form').addEventListener('submit', event => { event.preventDefault(); refresh(); });
        byId('dashboard-refresh-interval').addEventListener('change', event => { state.interval = Number(event.target.value); persist(); schedule(); });
        byId('dashboard-compact').addEventListener('click', () => { state.compact = !state.compact; document.body.classList.toggle('dashboard-compact', state.compact); byId('dashboard-compact').setAttribute('aria-pressed', String(state.compact)); persist(); });
        byId('dashboard-reset-layout').addEventListener('click', () => {
            state.hidden = []; state.order = []; state.compact = false; state.interval = DEFAULT_REFRESH_INTERVAL;
            local.set({}); fetch('/api/dashboard/preferences', { method: 'DELETE', credentials: 'same-origin' }).catch(() => {});
            document.body.classList.remove('dashboard-compact'); document.querySelectorAll('.dashboard-kpi').forEach(card => card.hidden = false);
            byId('dashboard-refresh-interval').value = String(DEFAULT_REFRESH_INTERVAL); applyKpiOrder(); renderToggleMenu(); schedule();
        });
        document.querySelectorAll('#dashboard-legend input').forEach(input => input.addEventListener('change', () => renderSvg(visiblePoints())));
        byId('dashboard-zoom-in').addEventListener('click', () => { state.zoom = Math.min(4, state.zoom + 1); state.pan = 0; renderSvg(visiblePoints()); });
        byId('dashboard-zoom-out').addEventListener('click', () => { state.zoom = Math.max(0, state.zoom - 1); state.pan = 0; renderSvg(visiblePoints()); });
        byId('dashboard-pan-back').addEventListener('click', () => { state.pan = Math.max(0, state.pan - 1); renderSvg(visiblePoints()); });
        byId('dashboard-pan-forward').addEventListener('click', () => { state.pan = Math.min(Math.max(0, state.points.length - 3), state.pan + 1); renderSvg(visiblePoints()); });
        schedule(); refresh();
    }
    document.addEventListener('htmx:beforeCleanupElement', event => {
        if (activeRoot && (event.target === activeRoot || event.target.contains?.(activeRoot))) cleanup(activeRoot);
    });
    document.addEventListener('DOMContentLoaded', init);
    document.addEventListener('htmx:load', init);
})();
