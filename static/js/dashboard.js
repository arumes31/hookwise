(() => {
    'use strict';

    const storageKey = 'hookwise.dashboard.layout.v1';

    // Jede KPI-Kachel verlinkt auf die Sicht, die ihre Zahl erklaert:
    // DLQ -> History mit status=dlq, Failing -> Webhooks-Quickfilter usw.
    // (/webhooks liest q/status/filter aus der URL, /history filtert serverseitig.)
    const KPI_ZIELE = {
        dead_letter_queue: '/history?status=dlq',
        failing_endpoints: '/webhooks?filter=recent_failures',
        success_rate: '/history?status=processed',
        total_endpoints: '/webhooks',
        active_endpoints: '/webhooks?status=enabled',
        total_events: '/history',
        processed_events: '/history?status=processed',
        average_latency: '/history',
        skipped_no_action: '/history?status=skipped',
        failed_events: '/history?status=failed',
        stale_endpoints: '/webhooks?filter=stale',
    };
    if (!window.hwKpiNavigation) {
        window.hwKpiNavigation = true;
        document.addEventListener('click', (ev) => {
            const kachel = ev.target.closest('.dashboard-kpi[data-kpi]');
            if (!kachel) return;
            const ziel = KPI_ZIELE[kachel.dataset.kpi];
            if (ziel) window.location.href = ziel;
        });
    }

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
            // Ohne Vergleichswert stand auf fast jeder Kachel 'No prior
            // comparison' -- eine Zeile, die nur sagt, dass es nichts zu
            // sagen gibt. Sie entfaellt; ein echtes Delta erscheint weiter.
            detail.hidden = deltaText === 'No prior comparison';
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
        // Version 2. Wichtigste Korrektur gegenueber v1: failure_rate ist ein
        // Prozentwert und teilte sich die Achse mit Stueckzahlen -- die Linie
        // lag damit an beliebiger Hoehe. Sie hat jetzt eine eigene rechte
        // Achse (0..max %). p95 (Sekunden) wird auf seine eigene Spanne
        // normiert und gestrichelt gezeichnet; den echten Wert nennt der
        // Tooltip. Linien laufen als weiche Kurven, Balken haben eine
        // Mindestbreite, das Volumen eine Verlaufsflaeche.
        const host = byId('dashboard-chart');
        const enabled = [...document.querySelectorAll('#dashboard-legend input:checked')].map(input => input.value);
        if (!points.length || !enabled.length) { host.replaceChildren(); return; }
        const width = 920, height = 260, padL = 46, padR = 46, padY = 24;
        const ns = 'http://www.w3.org/2000/svg';
        const css = getComputedStyle(document.documentElement);
        const tok = name => css.getPropertyValue(name).trim();
        const colors = {
            volume: tok('--accent'), successful: tok('--ok'), failed: tok('--crit'),
            failure_rate: tok('--warn'), p95: tok('--text-muted'),
        };
        const innerW = width - padL - padR, innerH = height - padY * 2;
        const xAt = index => padL + (points.length === 1 ? innerW / 2 : index * (innerW / (points.length - 1)));
        const countKeys = ['volume', 'successful', 'failed'].filter(key => enabled.includes(key));
        const maxCount = Math.max(1, ...countKeys.flatMap(key => points.map(point => Number(point[key] || 0))));
        const maxRate = Math.max(10, ...points.map(point => Number(point.failure_rate || 0)));
        const maxP95 = Math.max(0.1, ...points.map(point => Number(point.p95 || 0)));
        const yCount = value => height - padY - (value / maxCount) * innerH;
        const yRate = value => height - padY - (value / maxRate) * innerH;
        const yP95 = value => height - padY - (value / maxP95) * innerH;
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', 'Event history for the selected range');
        svg.style.width = '100%';
        const el = (tag, attrs, parent = svg) => {
            const node = document.createElementNS(ns, tag);
            Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
            parent.appendChild(node); return node;
        };
        const defs = el('defs', {});
        const grad = el('linearGradient', { id: 'hw-vol-grad', x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
        el('stop', { offset: '0%', 'stop-color': colors.volume, 'stop-opacity': '.22' }, grad);
        el('stop', { offset: '100%', 'stop-color': colors.volume, 'stop-opacity': '0' }, grad);
        // Weiche Kurve durch die Punkte (Catmull-Rom als Bezier)
        const glatt = coords => {
            if (coords.length < 3) return 'M' + coords.map(c => c.join(',')).join(' L');
            let d = `M${coords[0][0]},${coords[0][1]}`;
            for (let i = 0; i < coords.length - 1; i++) {
                const p0 = coords[Math.max(0, i - 1)], p1 = coords[i],
                      p2 = coords[i + 1], p3 = coords[Math.min(coords.length - 1, i + 2)];
                // Kontrollpunkte in Y auf den Bereich der beiden Stuetzpunkte
                // klemmen: Catmull-Rom schiesst sonst an Extremen ueber das
                // Maximum hinaus -- die Kurve zeigte Werte, die es nicht gibt.
                const lo = Math.min(p1[1], p2[1]), hi = Math.max(p1[1], p2[1]);
                const klemm = y => Math.min(hi, Math.max(lo, y));
                const c1 = [p1[0] + (p2[0] - p0[0]) / 6, klemm(p1[1] + (p2[1] - p0[1]) / 6)];
                const c2 = [p2[0] - (p3[0] - p1[0]) / 6, klemm(p2[1] - (p3[1] - p1[1]) / 6)];
                d += ` C${c1[0]},${c1[1]} ${c2[0]},${c2[1]} ${p2[0]},${p2[1]}`;
            }
            return d;
        };
        [0, .25, .5, .75, 1].forEach(f => {
            const y = height - padY - f * innerH;
            el('line', { x1: padL, x2: width - padR, y1: y, y2: y,
                stroke: tok('--line'), 'stroke-width': 1,
                'stroke-dasharray': '2 6' });
            if (f > 0 && countKeys.length) {
                const lbl = el('text', { x: padL - 8, y: y + 3, 'text-anchor': 'end',
                    fill: tok('--text-faint'), 'font-size': '10' });
                lbl.textContent = String(Math.round(maxCount * f));
            }
            if (f > 0 && enabled.includes('failure_rate')) {
                const lbl = el('text', { x: width - padR + 8, y: y + 3, 'text-anchor': 'start',
                    fill: colors.failure_rate, 'font-size': '10', 'fill-opacity': '.85' });
                lbl.textContent = Math.round(maxRate * f) + '%';
            }
        });
        const step = Math.ceil(points.length / 8);
        points.forEach((point, index) => {
            if (index % step !== 0 && index !== points.length - 1) return;
            const lbl = el('text', { x: xAt(index), y: height - 6, 'text-anchor': 'middle',
                fill: tok('--text-faint'), 'font-size': '10' });
            lbl.textContent = String(point.label).slice(5);
        });
        // Velocity-Stil: keine Balken. successful als duenne Zweitlinie,
        // failed als rote Punkte auf Werthoehe -- wie im Artifact-Chart.
        if (enabled.includes('successful')) {
            const coords = points.map((point, index) => [xAt(index), yCount(Number(point.successful || 0))]);
            el('path', { d: glatt(coords), fill: 'none', stroke: colors.successful,
                'stroke-width': 1.6, 'stroke-opacity': '.7',
                'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
        }
        if (enabled.includes('failed')) {
            points.forEach((point, index) => {
                const value = Number(point.failed || 0);
                if (!value) return;
                const dot = el('circle', { cx: xAt(index), cy: yCount(value), r: 3.5,
                    fill: tok('--crit-soft'), stroke: colors.failed, 'stroke-width': 1.5 });
                const title = document.createElementNS(ns, 'title');
                title.textContent = `${point.label} · failed: ${value}`;
                dot.appendChild(title);
            });
        }
        if (enabled.includes('volume')) {
            const coords = points.map((point, index) => [xAt(index), yCount(Number(point.volume || 0))]);
            if (points.length > 1) {
                el('path', { d: glatt(coords)
                    + ` L${coords[coords.length - 1][0]},${height - padY} L${coords[0][0]},${height - padY} Z`,
                    fill: 'url(#hw-vol-grad)' });
            }
            el('path', { d: glatt(coords), fill: 'none', stroke: colors.volume,
                'stroke-width': 2.25, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
            const last = coords[coords.length - 1];
            el('circle', { cx: last[0], cy: last[1], r: 4, fill: colors.volume,
                stroke: tok('--bg-surface'), 'stroke-width': 1.5, 'class': 'hw-chart-puls' });
        }
        [['failure_rate', yRate, 'none'], ['p95', yP95, '5 4']].forEach(entry => {
            const key = entry[0], yFn = entry[1], dash = entry[2];
            if (!enabled.includes(key)) return;
            const coords = points.map((point, index) => [xAt(index), yFn(Number(point[key] || 0))]);
            el('path', { d: glatt(coords), fill: 'none', stroke: colors[key],
                'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
                'stroke-dasharray': dash });
            const last = coords[coords.length - 1];
            el('circle', { cx: last[0], cy: last[1], r: 3, fill: colors[key] });
        });
        points.forEach((point, index) => {
            const zone = el('rect', { x: xAt(index) - innerW / Math.max(1, points.length) / 2, y: padY,
                width: innerW / Math.max(1, points.length), height: innerH, fill: 'transparent' });
            const title = document.createElementNS(ns, 'title');
            title.textContent = `${point.label}: ${point.volume} Ereignisse · ${point.failed} fehlgeschlagen (${point.failure_rate}%) · p95 ${point.p95}s`;
            zone.appendChild(title);
        });
        host.replaceChildren(svg);
    }


    function renderKpiSparks(points) {
        // Hinterlegte Miniverlaeufe auf den Kacheln, deren Kennzahl eine
        // Zeitreihe hat (Ereignisse, Raten, Latenz). Bestandszaehler wie
        // "Total endpoints" haben keine Historie und bleiben ruhig.
        // Die Reihe kommt aus denselben Analytics-Punkten wie das grosse
        // Diagramm -- keine zweite Datenquelle, kein weiterer Abruf.
        const css = getComputedStyle(document.documentElement);
        const tok = name => css.getPropertyValue(name).trim();
        const reihen = {
            total_events: { werte: p => Number(p.volume || 0), farbe: tok('--accent') },
            processed_events: { werte: p => Number(p.processed || 0), farbe: tok('--accent') },
            failed_events: { werte: p => Number(p.failed || 0), farbe: tok('--crit') },
            skipped_no_action: { werte: p => Math.max(0, Number(p.successful || 0) - Number(p.processed || 0)), farbe: tok('--accent') },
            success_rate: { werte: p => 100 - Number(p.failure_rate || 0), farbe: tok('--ok') },
            average_latency: { werte: p => Number(p.average_latency || 0), farbe: tok('--accent') },
        };
        document.querySelectorAll('.dashboard-kpi').forEach(card => {
            card.querySelector('.hw-kpi-spark')?.remove();
            const reihe = reihen[card.dataset.kpi];
            if (!reihe || points.length < 2) return;
            const werte = points.map(reihe.werte);
            const max = Math.max(1, ...werte);
            const w = 120, h = 30;
            const ns = 'http://www.w3.org/2000/svg';
            const svg = document.createElementNS(ns, 'svg');
            svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
            svg.setAttribute('preserveAspectRatio', 'none');
            svg.setAttribute('class', 'hw-kpi-spark');
            svg.setAttribute('aria-hidden', 'true');
            const xy = werte.map((v, i) => [i * (w / (werte.length - 1)), h - 3 - (v / max) * (h - 6)]);
            const linie = 'M' + xy.map(c => c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' L');
            const flaeche = document.createElementNS(ns, 'path');
            flaeche.setAttribute('d', `${linie} L${w},${h} L0,${h} Z`);
            flaeche.setAttribute('fill', reihe.farbe);
            flaeche.setAttribute('fill-opacity', '.10');
            svg.appendChild(flaeche);
            const pfad = document.createElementNS(ns, 'path');
            pfad.setAttribute('d', linie);
            pfad.setAttribute('fill', 'none');
            pfad.setAttribute('stroke', reihe.farbe);
            pfad.setAttribute('stroke-opacity', '.45');
            pfad.setAttribute('stroke-width', '1.5');
            svg.appendChild(pfad);
            card.appendChild(svg);
        });
    }

    function renderAnalytics(data) {
        state.points = data.points || [];
        state.pan = 0;
        renderKpiSparks(state.points);
        byId('dashboard-timezone').textContent = data.timezone || 'UTC';
        const rangeNamen = { '24h': 'last 24 hours', '7d': 'last 7 days',
            '30d': 'last 30 days', '90d': 'last 90 days', custom: 'custom range' };
        document.querySelectorAll('.hw-range-label').forEach(el => {
            el.textContent = rangeNamen[state.range] || state.range;
        });
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
        // Seit der Seitentrennung liegt die Endpoint-Liste auf /webhooks.
        // Auf dem Dashboard gibt es sie nicht mehr -- der Klick fuehrt dann
        // dorthin und uebergibt die Kennzahl als Parameter; webhooks.html
        // wendet den Filter beim Laden an.
        if (!byId('endpoint-list')) {
            // Abbildung auf die Schnellfilter, die /webhooks bereits kennt
            // (endpoints-dashboard.js): so greifen Chips, Leerzustand und
            // URL-Persistenz der Zielseite, statt dass hier ein zweites
            // Filtersystem entsteht.
            const ziel = {
                failing_endpoints: '?filter=recent_failures',
                dead_letter_queue: '?filter=recent_failures',
                failed_events: '?filter=recent_failures',
                stale_endpoints: '?filter=stale',
                average_latency: '?filter=high_latency',
                active_endpoints: '?status=enabled',
            };
            // Ohne Parameter wuerde /webhooks den zuletzt gespeicherten Filter
            // wiederherstellen -- ein leerer filter-Parameter loescht ihn explizit.
            window.location.href = '/webhooks' + (ziel[key] || '?filter=&status=&q=');
            return;
        }
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
        // Schnellwahl im Leerzustand des Diagramms: setzt den Regler und laedt
        // neu -- niemand muss den Bezug zum Regler oben rechts erst suchen.
        document.querySelectorAll('.hw-range-quick').forEach(btn => {
            btn.addEventListener('click', () => {
                byId('dashboard-range').value = btn.dataset.range;
                state.range = btn.dataset.range;
                byId('dashboard-custom-range').hidden = true;
                persist(); refresh();
            });
        });
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
