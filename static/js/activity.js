/* Client-side live activity controls.  All untrusted event text stays text-only. */
(() => {
    'use strict';
    const STORAGE_KEY = 'hookwise.activity-preferences.v1';
    const NOTES_KEY = 'hookwise.activity-notes.v1';
    const state = { paused: false, seen: new Set(), rendered: new Map(), duplicates: 0, counters: { all: 0, failure: 0, success: 0 }, connection: null, keyboardBound: false };
    const load = () => { try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch (_) { return {}; } };
    const save = values => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(values)); } catch (_) { /* non-critical */ } };
    const eventKey = data => data.id || `${data.request_id || ''}:${data.config_name || ''}:${data.timestamp || ''}:${data.message || ''}`;
    const notes = () => { try { return JSON.parse(localStorage.getItem(NOTES_KEY) || '{}'); } catch (_) { return {}; } };
    const saveNotes = value => { try { localStorage.setItem(NOTES_KEY, JSON.stringify(value)); } catch (_) { /* Optional local annotations. */ } };
    async function persistAnnotation(data, value) {
        if (!data.id) return;
        const response = await fetch(`/api/activity/events/${encodeURIComponent(data.id)}/annotation`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || '',
            },
            body: JSON.stringify({ text: value.note || '', is_pinned: Boolean(value.pinned) }),
        });
        if (!response.ok) throw new Error('Could not save this activity annotation.');
    }
    function updateUi() {
        const count = document.getElementById('activity-counter');
        if (count) count.textContent = `${state.counters.all} events · ${state.counters.failure} failures`;
        const duplicate = document.getElementById('activity-duplicates');
        if (duplicate) { duplicate.hidden = state.duplicates === 0; duplicate.textContent = `${state.duplicates} duplicate${state.duplicates === 1 ? '' : 's'} hidden`; }
    }
    function controls() {
        return {
            severity: document.getElementById('activity-severity')?.value || '', action: document.getElementById('activity-action')?.value || '',
            endpoint: document.getElementById('activity-endpoint')?.value || '', board: document.getElementById('activity-board')?.value || '', status: document.getElementById('activity-status')?.value || '',
        };
    }
    function matches(data) {
        const filter = controls(); const level = data.level === 'error' ? 'failure' : data.level === 'danger' ? 'failure' : data.level;
        if (filter.severity && filter.severity !== level) return false;
        if (filter.action && filter.action !== (data.action || 'none')) return false;
        if (filter.endpoint && filter.endpoint !== (data.config_id || data.config_name)) return false;
        if (filter.board && filter.board !== data.board) return false;
        return !filter.status || filter.status === data.status;
    }
    function accept(data, initial) {
        if (state.paused && !initial) return false;
        const key = eventKey(data);
        if (state.seen.has(key)) { state.duplicates += 1; updateUi(); return false; }
        state.seen.add(key);
        if (state.seen.size > 500) state.seen.delete(state.seen.values().next().value);
        state.counters.all += 1;
        if (data.level === 'error' || data.level === 'danger' || data.status === 'failed' || data.status === 'dlq') state.counters.failure += 1;
        if (data.level === 'success' || data.status === 'processed') state.counters.success += 1;
        updateUi(); return true;
    }
    function decorate(entry, data) {
        const key = eventKey(data); const saved = notes(); const persisted = data.annotation || {};
        if (data.id && data.annotation) saved[key] = { note: persisted.text || '', pinned: Boolean(persisted.is_pinned) };
        const controls = document.createElement('span');
        controls.className = 'activity-entry-controls ms-2';
        const pin = document.createElement('button'); pin.type = 'button'; pin.className = 'btn btn-sm btn-link p-0 text-warning'; pin.textContent = saved[key]?.pinned ? '★' : '☆'; pin.title = 'Pin event';
        const annotate = document.createElement('button'); annotate.type = 'button'; annotate.className = 'btn btn-sm btn-link p-0 text-secondary ms-1'; annotate.textContent = '✎'; annotate.title = 'Annotate event';
        const note = document.createElement('small'); note.className = 'text-info ms-1'; note.textContent = saved[key]?.note || '';
        pin.addEventListener('click', async event => {
            event.stopPropagation();
            const next = notes(); next[key] = { ...(next[key] || {}), pinned: !next[key]?.pinned }; saveNotes(next);
            pin.textContent = next[key].pinned ? '★' : '☆'; entry.classList.toggle('activity-pinned', next[key].pinned);
            try { await persistAnnotation(data, next[key]); } catch (error) { window.showToast?.(error.message, 'danger'); }
        });
        annotate.addEventListener('click', async event => {
            event.stopPropagation(); const next = notes();
            const value = window.prompt('Event annotation:', next[key]?.note || ''); if (value === null) return;
            next[key] = { ...(next[key] || {}), note: value.slice(0, 280) }; saveNotes(next); note.textContent = next[key].note;
            try { await persistAnnotation(data, next[key]); } catch (error) { window.showToast?.(error.message, 'danger'); }
        });
        entry.classList.toggle('activity-pinned', Boolean(saved[key]?.pinned)); controls.append(pin, annotate, note); entry.querySelector('.d-flex')?.append(controls);
        entry.hidden = !matches(data);
        entry.dataset.requestId = data.request_id || '';
        state.rendered.set(entry, data);
    }
    function init() {
        const panel = document.getElementById('activity-controls'); if (!panel || panel.dataset.init) return; panel.dataset.init = 'true';
        state.seen.clear();
        state.rendered.clear();
        state.duplicates = 0;
        state.counters = { all: 0, failure: 0, success: 0 };
        const preferences = load();
        Object.entries(preferences).forEach(([id, value]) => { const control = document.getElementById(id); if (control) control.value = value; });
        const endpoint = document.getElementById('activity-endpoint');
        const board = document.getElementById('activity-board');
        if (endpoint) {
            [...document.querySelectorAll('.endpoint-card')].forEach(card => {
                const option = new Option(card.dataset.name || card.dataset.id, card.dataset.id || card.dataset.name);
                endpoint.add(option);
            });
        }
        if (board) {
            [...new Set([...document.querySelectorAll('.endpoint-card')].map(card => card.dataset.board).filter(Boolean))]
                .sort().forEach(name => board.add(new Option(name, name)));
        }
        panel.addEventListener('change', () => {
            const values = {}; panel.querySelectorAll('select').forEach(select => { values[select.id] = select.value; }); save(values);
            state.rendered.forEach((data, entry) => { if (entry.isConnected) entry.hidden = !matches(data); else state.rendered.delete(entry); });
        });
        document.getElementById('activity-pause')?.addEventListener('click', () => { state.paused = !state.paused; const button = document.getElementById('activity-pause'); button?.classList.toggle('active', state.paused); button?.setAttribute('aria-pressed', String(state.paused)); });
        document.getElementById('activity-clear')?.addEventListener('click', () => { const container = document.getElementById('log-container'); if (container) container.replaceChildren(); state.seen.clear(); state.rendered.clear(); state.duplicates = 0; state.counters = { all: 0, failure: 0, success: 0 }; updateUi(); });
        if (!state.keyboardBound) {
            document.addEventListener('keydown', event => { if (event.altKey && event.key.toLowerCase() === 'p' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) { event.preventDefault(); document.getElementById('activity-pause')?.click(); } });
            state.keyboardBound = true;
        }
        updateUi();
        const socket = window.getSocket?.();
        const setConnection = (message, disconnected) => {
            const banner = document.getElementById('activity-disconnected');
            if (banner) { banner.hidden = !disconnected; banner.textContent = message; }
        };
        if (state.connection) {
            state.connection.socket.off('connect', state.connection.connect);
            state.connection.socket.off('disconnect', state.connection.disconnect);
            state.connection.socket.io?.off('reconnect_attempt', state.connection.reconnect);
        }
        if (socket) {
            const connection = {
                socket,
                connect: () => setConnection('', false),
                disconnect: () => setConnection('Live activity disconnected. Reconnection is automatic.', true),
                reconnect: attempt => setConnection(`Reconnecting live activity (attempt ${attempt})…`, true),
            };
            state.connection = connection;
            socket.on('connect', connection.connect);
            socket.on('disconnect', connection.disconnect);
            socket.io?.on('reconnect_attempt', connection.reconnect);
            setConnection(socket.connected ? '' : 'Live activity disconnected. Reconnection is automatic.', !socket.connected);
        }
    }
    const maxEntries = () => {
        const value = Number.parseInt(document.getElementById('activity-buffer')?.value || '200', 10);
        return [100, 200, 500].includes(value) ? value : 200;
    };
    window.activityStream = { accept, decorate, matches, init, maxEntries };
    document.addEventListener('DOMContentLoaded', init);
    document.addEventListener('htmx:load', init);
})();
