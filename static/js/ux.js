/**
 * HookWise UX Logic
 * Handles search, filtering, bulk actions, and validator tool.
 */

// Singleton Socket.IO instance
var socketInstance = null;
function getSocket() {
    if (!socketInstance) {
        socketInstance = typeof io !== 'undefined' ? io() : null;
    }
    return socketInstance;
}

function initSocketHandlers() {
    const socket = getSocket();
    if (!socket) return;
    // Global listeners can go here
}

document.addEventListener('DOMContentLoaded', () => {
    reinitApp(document.body);
    initSocketHandlers();
});

// HTMX Lifecycle
// Use afterSettle to ensure we run once after the DOM is fully swapped
document.body.addEventListener('htmx:afterSettle', function (evt) {
    reinitApp(evt.detail.elt || document.body);
});

document.body.addEventListener('htmx:beforeRequest', function (evt) {
    const bar = document.getElementById('loading-bar');
    if (bar) {
        bar.style.transition = 'none';
        bar.style.width = '0%';
        setTimeout(() => {
            bar.style.transition = 'width 2s ease-out';
            bar.style.width = '70%';
        }, 10);
    }
});

document.body.addEventListener('htmx:afterRequest', function (evt) {
    const bar = document.getElementById('loading-bar');
    if (bar) {
        bar.style.transition = 'width 0.3s ease-out';
        bar.style.width = '100%';
        setTimeout(() => {
            bar.style.width = '0%';
        }, 500);
    }
});

function reinitApp(container) {
    initSearch(container);
    initBulkActions(container);
    initServiceHealth(container);
    initToasts(container);
    initTransitions(container);
    initDragAndDrop(container);
    initContextMenu(container);
    initAutoSave(container);
    initFeedback(container);
    initPullToRefresh(container);
    initOnboarding(container);
    initNotifications(container);

    // Trigger template-specific initializations if they exist
    if (window.onPageLoad) window.onPageLoad(container);

    // Delay tooltip initialization slightly to ensure layout and animations are stable
    setTimeout(() => initTooltips(container), 500);
}

// A8: Robust session handling - prevent "Back" button from showing cached protected pages after logout
window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
        // If the page is loaded from bfcache (Back-Forward Cache), force a reload
        // to trigger a server-side auth check.
        window.location.reload();
    }
});

/**
 * Modern Confirmation Prompt Wrapper
 * @param {string} message 
 * @param {object} options { title, okText, cancelText }
 * @returns {Promise<boolean>}
 */
window.hwConfirm = function (message, options = {}) {
    return new Promise((resolve) => {
        const modalEl = document.getElementById('hw-confirm-modal');
        if (!modalEl) {
            resolve(confirm(message));
            return;
        }
        const modal = new bootstrap.Modal(modalEl);

        document.getElementById('hw-confirm-message').textContent = message;
        document.getElementById('hw-confirm-title').textContent = options.title || 'Confirm Action';
        document.getElementById('hw-confirm-ok').textContent = options.okText || 'Confirm';
        document.getElementById('hw-confirm-cancel').textContent = options.cancelText || 'Cancel';

        const btnOk = document.getElementById('hw-confirm-ok');
        const btnCancel = document.getElementById('hw-confirm-cancel');

        let handled = false;

        const onHidden = () => {
            if (!handled) {
                handled = true;
                resolve(false);
            }
        };

        const cleanup = (result) => {
            if (handled) return;
            handled = true;
            modalEl.removeEventListener('hidden.bs.modal', onHidden);
            btnOk.onclick = null;
            btnCancel.onclick = null;
            modal.hide();
            resolve(result);
        };

        btnOk.onclick = () => cleanup(true);
        btnCancel.onclick = () => cleanup(false);
        modalEl.addEventListener('hidden.bs.modal', onHidden, { once: true });

        modal.show();
    });
};

// Tooltip System - Optimized for icon-only button detection
function initTooltips(container = document) {
    // 1. Robust cleanup of existing instances
    const nodes = container.querySelectorAll('[data-tooltip]');
    nodes.forEach(el => {
        try {
            const instance = bootstrap.Tooltip.getInstance(el);
            if (instance) instance.destroy();
        } catch (e) {
            console.warn('Tooltip cleanup failed for element', el, e);
        }
    });

    // 2. Precise re-initialization
    const triggerList = [].slice.call(nodes);
    triggerList.forEach(function (el) {
        const title = el.getAttribute('data-tooltip');
        if (!title) return;

        // USER RULE: Only add tooltips to buttons/links without VISIBLE text
        const clone = el.cloneNode(true);
        const icons = clone.querySelectorAll('svg, i, .bi, .visually-hidden, span.d-none');
        icons.forEach(icon => icon.remove());

        const textContent = clone.innerText.trim();
        const hasVisibleText = textContent.length > 0;

        if (hasVisibleText && (el.tagName === 'BUTTON' || el.classList.contains('btn'))) {
            el.removeAttribute('title');
            return;
        }

        new bootstrap.Tooltip(el, {
            title: title,
            placement: 'top',
            container: 'body',
            boundary: 'clippingParents',
            trigger: 'hover',
            fallbackPlacements: ['bottom', 'right']
        });
    });
}


function initToasts() {
}

// Programmatische Reloads (Pin, Pause, Bulk, Import ...) warfen den Nutzer an
// den Seitenanfang. hwReloadInPlace merkt sich die Scroll-Position; der
// Block darunter stellt sie nach dem naechsten Seitenaufbau wieder her --
// auch nach POST-Redirects (Archivieren/Duplizieren), die hwMerkeScroll
// vor dem Absenden aufrufen.
window.hwMerkeScroll = function () {
    try {
        sessionStorage.setItem('hw.scrollpos', JSON.stringify({
            p: window.location.pathname, y: Math.round(window.scrollY),
        }));
    } catch (e) {}
};
window.hwReloadInPlace = function () {
    window.hwMerkeScroll();
    window.location.reload();
};
(function () {
    let wert = null;
    try {
        wert = sessionStorage.getItem('hw.scrollpos');
        if (wert !== null) sessionStorage.removeItem('hw.scrollpos');
    } catch (e) {}
    if (wert === null) return;
    let daten = null;
    try { daten = JSON.parse(wert); } catch (e) { return; }
    // Nur auf derselben Seite wiederherstellen -- ein Redirect auf eine
    // andere (womoeglich kuerzere) Seite soll oben beginnen.
    if (!daten || daten.p !== window.location.pathname) return;
    const ziel = Number(daten.y);
    if (!Number.isFinite(ziel) || ziel <= 0) return;
    window.addEventListener('load', function () {
        requestAnimationFrame(function () { window.scrollTo(0, ziel); });
    });
})();

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const allowedTypes = new Set(['info', 'success', 'warning', 'danger', 'error']);
    const normalizedType = allowedTypes.has(type) ? type : 'info';
    const visualType = normalizedType === 'error' ? 'danger' : normalizedType;
    const toast = document.createElement('div');
    toast.className = `alert alert-${visualType} alert-dismissible fade show shadow-lg border-0`;
    toast.style.minWidth = '300px';
    // Flaeche und Textfarbe kommen aus den Alert-Tokens (Abschnitt 16) --
    // die vorherige Inline-Dunkelflaeche blieb auch im hellen Theme dunkel.

    let iconSvg = '';
    if (normalizedType === 'success') iconSvg = '<svg class="hw-icon" width="16" height="16" aria-hidden="true" focusable="false"><use href="#i-check-circle-fill"></use></svg>';
    else if (visualType === 'danger') iconSvg = '<svg class="hw-icon" width="16" height="16" aria-hidden="true" focusable="false"><use href="#i-exclamation-triangle-fill"></use></svg>';
    else iconSvg = '<svg class="hw-icon" width="16" height="16" aria-hidden="true" focusable="false"><use href="#i-info-circle-fill"></use></svg>';

    const content = document.createElement('div');
    content.className = 'd-flex align-items-center';

    const icon = document.createElement('div');
    icon.className = `me-3 text-${visualType}`;
    icon.innerHTML = iconSvg;

    const messageNode = document.createElement('div');
    messageNode.textContent = String(message);

    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'btn-close btn-close-white ms-auto';
    closeButton.dataset.bsDismiss = 'alert';

    content.append(icon, messageNode, closeButton);
    toast.appendChild(content);
    container.appendChild(toast);
    setTimeout(() => {
        const bsToast = new bootstrap.Alert(toast);
        bsToast.close();
    }, 5000);
}

// Endpoint Search
function initSearch(container = document) {
    const searchInput = container.querySelector('#endpoint-search');
    const boardFilter = container.querySelector('#board-filter');
    const statusFilter = container.querySelector('#status-filter');
    if (!searchInput) return;

    // Use a unique flag to prevent duplicate listeners
    if (searchInput.dataset.initSearch) return;
    searchInput.dataset.initSearch = 'true';

    // Global key listener should only be added once
    if (!window._searchShortcutInit) {
        document.addEventListener('keydown', (e) => {
            if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
                const globalInput = document.getElementById('endpoint-search');
                if (globalInput) {
                    e.preventDefault();
                    globalInput.focus();
                }
            }
        });
        window._searchShortcutInit = true;
    }

    const filterEndpoints = () => {
        const term = searchInput.value.toLowerCase();
        const board = boardFilter.value;
        const status = statusFilter.value;

        // Historischer Doppelfilter: endpoints-dashboard.js verdrahtet dieselben
        // Felder vollstaendig (Chips, token-Suche, Persistenz). Laeuft das
        // moderne System, tritt dieser Handler ab -- sonst kaempfen hidden
        // und display gegeneinander. Er stuerzte zudem seit der Wrapper-
        // Aenderung an .closest(...) ab und hat real nie gefiltert.
        if (document.getElementById('endpoint-filter-chips')) return;
        document.querySelectorAll('.endpoint-card').forEach(card => {
            const name = card.dataset.name.toLowerCase();
            const id = card.dataset.id.toLowerCase();
            const cardBoard = card.dataset.board;
            const cardStatus = card.dataset.status;

            const matchesSearch = name.includes(term) || id.includes(term);
            const matchesBoard = !board || cardBoard === board;
            const matchesStatus = !status || cardStatus === status;

            const wrapper = card.closest('.draggable-card, .col-md-6, .col-12');
            if (wrapper) wrapper.style.display =
                (matchesSearch && matchesBoard && matchesStatus) ? 'block' : 'none';
        });
    };

    searchInput.addEventListener('input', filterEndpoints);
    boardFilter.addEventListener('change', filterEndpoints);
    statusFilter.addEventListener('change', filterEndpoints);

    const boards = new Set();
    document.querySelectorAll('.endpoint-card').forEach(card => {
        if (card.dataset.board) boards.add(card.dataset.board);
    });
    boards.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b;
        opt.textContent = b;
        boardFilter.appendChild(opt);
    });
}

// Bulk Actions
function initBulkActions(container = document) {
    const mainCheck = container.querySelector('#check-all');
    const bulkControls = container.querySelector('#bulk-controls');
    if (!mainCheck) return;

    if (mainCheck.dataset.initBulk) return;
    mainCheck.dataset.initBulk = 'true';

    const updateControls = () => {
        const checked = document.querySelectorAll('.endpoint-check:checked').length;
        bulkControls.classList.toggle('d-none', checked === 0);
    };

    mainCheck.addEventListener('change', () => {
        document.querySelectorAll('.endpoint-check').forEach(c => c.checked = mainCheck.checked);
        updateControls();
    });

    document.querySelectorAll('.endpoint-check').forEach(c => {
        c.addEventListener('change', updateControls);
    });
}

// Service Health Monitoring
function initServiceHealth(container = document) {
    // Health display elements might be in navigation (document-level) or page-level
    const redisEl = container.querySelector('#health-redis');
    const dbEl = container.querySelector('#health-database');
    const celeryEl = container.querySelector('#health-celery');

    if (!redisEl && !dbEl && !celeryEl) {
        if (container === document.body && window.healthInterval) clearInterval(window.healthInterval);
        return;
    }
    // Velocity: das Tab-Icon bleibt die Hook-Marke; der Systemzustand haengt
    // als kleiner Status-Punkt unten rechts dran (vorher uebermalte hier ein
    // "H"-Kreis das Favicon komplett -- daher sprang das Icon zurueck).
    // Hell/Dunkel folgt dem OS-Schema, auch bei Wechseln zur Laufzeit.
    const faviconBilder = { dark: new Image(), light: new Image() };
    // Quellen aus den <link>-Tags uebernehmen: die tragen den ?v=-Parameter,
    // sonst liefert der Browser-Cache ein veraltetes SVG in den Canvas.
    const faviconQuelle = (schema, fallback) => {
        const link = document.querySelector('link[rel="icon"][media*="' + schema + '"]');
        return link ? link.href : fallback;
    };
    faviconBilder.dark.src = faviconQuelle('dark', '/static/img/favicon-hook.svg');
    faviconBilder.light.src = faviconQuelle('light', '/static/img/favicon-hook-light.svg');
    let faviconStatus = 'up';
    const schemaHell = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
    const updateFavicon = (status) => {
        faviconStatus = status || faviconStatus;
        const hell = !!(schemaHell && schemaHell.matches);
        const bild = faviconBilder[hell ? 'light' : 'dark'];
        const zeichnen = () => {
            const canvas = document.createElement('canvas');
            canvas.width = 32; canvas.height = 32;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(bild, 0, 0, 32, 32);
            const farbe = faviconStatus === 'up' ? '#00e38b'
                : faviconStatus === 'warning' ? '#ffdd65' : '#a90219';
            ctx.beginPath(); ctx.arc(6.3, 8.3, 5.6, 0, 2 * Math.PI);
            ctx.fillStyle = hell ? '#f2f8f4' : '#0b0f0f'; ctx.fill();
            ctx.beginPath(); ctx.arc(6.3, 8.3, 4.4, 0, 2 * Math.PI);
            ctx.fillStyle = farbe; ctx.fill();
            const daten = canvas.toDataURL('image/png');
            document.querySelectorAll("link[rel~='icon']").forEach(l => { l.href = daten; });
        };
        if (bild.complete && bild.naturalWidth) zeichnen();
        else bild.onload = zeichnen;
    };
    if (schemaHell && schemaHell.addEventListener && !window.hwFaviconSchema) {
        window.hwFaviconSchema = true;
        schemaHell.addEventListener('change', () => updateFavicon(faviconStatus));
    }

    const updateHealth = async () => {
        try {
            const resp = await fetch('/health/services');
            const data = await resp.json();

            let overall = 'up';
            Object.keys(data).forEach(service => {
                if (data[service] === 'down') overall = 'down';
                else if (data[service] === 'warning' && overall === 'up') overall = 'warning';

                const el = document.getElementById(`health-${service}`);
                if (el) {
                    el.className = `heartbeat-dot heartbeat-${data[service]}`;
                    el.title = `${service.toUpperCase()}: ${data[service].toUpperCase()}`;
                }
                const dashEl = document.getElementById(`dash-health-${service}`);
                if (dashEl) {
                    dashEl.className = `heartbeat-dot heartbeat-${data[service]} mb-1 mx-auto`;
                    if (service === 'celery' && data.celery_active !== undefined) {
                        const labelEl = dashEl.parentElement.querySelector('.small');
                        if (labelEl) labelEl.textContent = `Celery (${data.celery_active})`;
                    }
                }
            });
            updateFavicon(overall);
        } catch (e) {
            console.debug('Health check did not complete before navigation', e);
            updateFavicon('down');
            // Update all visible dots to a disconnected/error state
            ['redis', 'database', 'celery'].forEach(service => {
                const el = document.getElementById(`health-${service}`);
                if (el) el.className = 'heartbeat-dot heartbeat-error';
                const dashEl = document.getElementById(`dash-health-${service}`);
                if (dashEl) dashEl.className = 'heartbeat-dot heartbeat-error mb-1 mx-auto';
            });
        }
    };

    updateHealth();
    if (window.healthInterval) clearInterval(window.healthInterval);
    window.healthInterval = setInterval(updateHealth, 30000);
}

// Transitions
function initTransitions() {
    const savedView = localStorage.getItem('endpoint-view') || 'list';
    if (window.toggleView) window.toggleView(savedView);

    document.body.classList.add('page-loaded');
    document.body.classList.remove('page-leaving');
}

// Bulk Actions Implementation
window.bulkDelete = async function () {
    const checked = Array.from(document.querySelectorAll('.endpoint-check:checked')).map(c => c.dataset.id);
    if (!checked.length) return;

    if (await hwConfirm(`Delete ${checked.length} endpoints?`, { title: 'Bulk Delete', okText: 'Delete All' })) {
        try {
            const resp = await fetch('/endpoint/bulk/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: checked })
            });
            const data = await resp.json();
            if (data.status === 'success') {
                showToast(data.message, 'success');
                setTimeout(() => window.hwReloadInPlace(), 1000);
            }
        } catch (e) {
            showToast('Error deleting endpoints', 'error');
        }
    }
};

window.bulkPause = async function () {
    const checked = Array.from(document.querySelectorAll('.endpoint-check:checked')).map(c => c.dataset.id);
    if (!checked.length) return;

    try {
        const resp = await fetch('/endpoint/bulk/pause', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: checked })
        });
        const data = await resp.json();
        if (data.status === 'success') {
            showToast(data.message, 'success');
            setTimeout(() => window.hwReloadInPlace(), 1000);
        }
    } catch (e) {
        showToast('Error pausing endpoints', 'error');
    }
};

window.bulkResume = async function () {
    const checked = Array.from(document.querySelectorAll('.endpoint-check:checked')).map(c => c.dataset.id);
    if (!checked.length) return;

    try {
        const resp = await fetch('/endpoint/bulk/resume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: checked })
        });
        const data = await resp.json();
        if (data.status === 'success') {
            showToast(data.message, 'success');
            setTimeout(() => window.hwReloadInPlace(), 1000);
        }
    } catch (e) {
        showToast('Error resuming endpoints', 'error');
    }
};

window.bulkExport = async function () {
    const checked = Array.from(document.querySelectorAll('.endpoint-check:checked')).map(c => c.dataset.id);
    if (!checked.length) return;

    try {
        const resp = await fetch('/endpoint/bulk/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: checked })
        });
        const blob = await resp.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'hookwise_config_export.json';
        document.body.appendChild(a);
        a.click();
        a.remove();
    } catch (e) {
        showToast('Error exporting configurations', 'error');
    }
};

window.toggleEndpoint = async function (id) {
    try {
        const resp = await fetch(`/endpoint/toggle/${id}`, { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'success') {
            showToast(`Endpoint ${data.is_enabled ? 'enabled' : 'disabled'}`, 'success');
            setTimeout(() => window.hwReloadInPlace(), 500);
        }
    } catch (e) {
        showToast('Error toggling endpoint', 'error');
    }
};

window.togglePin = async function (id) {
    try {
        const resp = await fetch('/endpoint/toggle-pin/' + id, { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'success') {
            showToast('Endpoint ' + (data.is_pinned ? 'pinned' : 'unpinned'), 'success');
            setTimeout(() => window.hwReloadInPlace(), 500);
        }
    } catch (e) {
        showToast('Error toggling pin', 'error');
    }
};

window.toggleView = function (view) {
    const grid = document.getElementById('endpoint-grid');
    if (!grid) return;

    const buttons = document.querySelectorAll('[onclick^="toggleView"]');
    buttons.forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('onclick').includes(view));
    });

    if (view === 'list') {
        grid.querySelectorAll('.col-xl-6, .col-lg-12').forEach(col => {
            col.classList.remove('col-xl-6', 'col-lg-12');
            col.classList.add('col-12');
        });
    } else {
        grid.querySelectorAll('.draggable-card.col-12').forEach(col => {
            col.classList.remove('col-12');
            col.classList.add('col-xl-6', 'col-lg-12');
        });
    }
    localStorage.setItem('endpoint-view', view);
};

// Kartenmenues (details.hw-more): der Browser schliesst <details> nie von
// selbst -- Klick ausserhalb und Escape schliessen jetzt alle offenen
// Menues. Beim Oeffnen schliessen sich Geschwister, und wenn das Menue
// unten aus dem Viewport laufen wuerde, klappt es nach oben auf
// (.hw-more-menu--oben, Gegenstueck in hookwise-console.css). Alles
// delegiert und im Head-Skript registriert: gilt damit auch fuer per
// hx-boost getauschte Seiten und fuer geklonte Menues in Tabellenzeilen.
document.addEventListener('pointerdown', function (ev) {
    document.querySelectorAll('details.hw-more[open]').forEach(function (menue) {
        if (!menue.contains(ev.target)) menue.removeAttribute('open');
    });
});
document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') return;
    document.querySelectorAll('details.hw-more[open]').forEach(function (menue) {
        menue.removeAttribute('open');
        const griff = menue.querySelector('summary');
        if (griff) griff.focus();
    });
});
document.addEventListener('toggle', function (ev) {
    const det = ev.target;
    if (!det || !det.matches || !det.matches('details.hw-more') || !det.open) return;
    document.querySelectorAll('details.hw-more[open]').forEach(function (anderes) {
        if (anderes !== det) anderes.removeAttribute('open');
    });
    const menue = det.querySelector('.hw-more-menu');
    if (!menue) return;
    menue.classList.remove('hw-more-menu--oben');
    const kante = menue.getBoundingClientRect();
    if (kante.bottom > window.innerHeight - 8) menue.classList.add('hw-more-menu--oben');
}, true);

// Die Menuezeilen bestanden aus Icon-Knopf + Textbeschriftung -- klickbar
// war nur das Icon, der Text tat nichts. Delegiert: ein Klick irgendwo in
// der Zeile leitet auf das Bedienelement der Zeile weiter (ausser der
// Klick traf bereits ein Bedienelement, dann laeuft dessen Handler nativ).
document.addEventListener('click', function (ev) {
    if (!(ev.target instanceof Element)) return;
    const zeile = ev.target.closest('.hw-more-row');
    if (!zeile) return;
    if (ev.target.closest('button, a, input, select, label')) return;
    const steuer = zeile.querySelector('button, a');
    if (steuer) steuer.click();
});

window.revealToken = async function (id) {
    const el = document.getElementById('token-' + id);
    if (!el) return;
    if (el.textContent.includes('•')) {
        try {
            const resp = await fetch('/endpoint/token/' + id);
            const data = await resp.json();
            el.textContent = data.token;
            el.classList.remove('text-secondary');
            el.classList.add('text-primary');
        } catch (e) {
            showToast('Error fetching token', 'error');
        }
    } else {
        el.textContent = '••••••••••••••••••••••••••••••••';
        el.classList.add('text-secondary');
        el.classList.remove('text-primary');
    }
};

window.copyToken = async function (id) {
    const el = document.getElementById('token-' + id);
    if (!el) return;
    let token = el.textContent;
    if (token.includes('•')) {
        const resp = await fetch('/endpoint/token/' + id);
        const data = await resp.json();
        token = data.token;
    }
    navigator.clipboard.writeText(token);
    showToast('Token copied to clipboard!', 'success');
};

// Relative Time Helper
window.getRelativeTime = function (timestamp) {
    const now = new Date();
    const then = new Date(timestamp);
    const diff = Math.floor((now - then) / 1000);

    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return then.toLocaleDateString();
};

// JSON Validator (Internal Tool)
window.testPath = function () {
    const jsonStr = document.getElementById('sample-json').value;
    const path = document.getElementById('trigger_field').value;
    const resultEl = document.getElementById('validation-result');

    try {
        const obj = JSON.parse(jsonStr);
        const resolve = (obj, path) => {
            const cleanPath = path.startsWith('$.') ? path.substring(2) : path;
            return cleanPath.replace(/\[(\d+)\]/g, '.$1')
                .split('.')
                .filter(p => p !== "")
                .reduce((o, i) => (o && o[i] !== undefined) ? o[i] : undefined, obj);
        };
        const val = resolve(obj, path);
        resultEl.className = 'mt-2 small ' + (val !== undefined ? 'text-success' : 'text-danger');
        resultEl.textContent = val !== undefined ? `Found value: ${JSON.stringify(val)}` : 'Field not found in payload';

        if (val !== undefined && !document.getElementById('trigger_field').value.includes(path)) {
            const autofillBtn = document.createElement('button');
            autofillBtn.className = 'btn btn-sm btn-link text-info p-0 ms-2';
            autofillBtn.textContent = 'Use this path';
            autofillBtn.onclick = () => {
                document.getElementById('trigger_field').value = path;
                showToast('Trigger field updated', 'success');
            };
            resultEl.appendChild(autofillBtn);
        }
    } catch (e) {
        resultEl.className = 'mt-2 small text-danger';
        resultEl.textContent = 'Invalid JSON input';
    }
};

// Error Troubleshooting Links
function getTroubleshootingLink(message) {
    const baseUrl = 'https://docs.connectwise.com/search?q=';
    if (message.includes('401')) return baseUrl + 'API+Authentication';
    if (message.includes('404')) return baseUrl + 'Resource+Not+Found';
    if (message.includes('error')) return baseUrl + 'Troubleshooting';
    return null;
}

window.escapeHtml = function (text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
};

// Nr. 14: Duplizieren aus dem Kartenmenue. Nutzt dieselbe POST-Form wie das
// Rechtsklick-Kontextmenue -- eine Route, zwei Wege dorthin.
// Nr. 12: Archivieren -- umkehrbar, deshalb ohne Rueckfrage.
window.archiveEndpoint = function (id) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/endpoint/archive/' + id;
    const csrfInput = document.createElement('input');
    csrfInput.type = 'hidden';
    csrfInput.name = 'csrf_token';
    csrfInput.value = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    form.appendChild(csrfInput);
    document.body.appendChild(form);
    window.hwMerkeScroll();
    form.submit();
};

window.cloneEndpoint = function (id) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/endpoint/clone/' + id;
    const csrfInput = document.createElement('input');
    csrfInput.type = 'hidden';
    csrfInput.name = 'csrf_token';
    csrfInput.value = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    form.appendChild(csrfInput);
    document.body.appendChild(form);
    window.hwMerkeScroll();
    form.submit();
};

window.copyToClipboard = function (text) {
    navigator.clipboard.writeText(text);
    showToast('Copied to clipboard!', 'success');
};

function initDragAndDrop(container = document) {
    const grid = container.querySelector('#endpoint-grid');
    if (!grid) return;

    if (grid.dataset.initDrag) return;
    grid.dataset.initDrag = 'true';

    let draggedItem = null;

    grid.addEventListener('dragstart', (e) => {
        draggedItem = e.target.closest('.draggable-card');
        if (draggedItem) {
            e.dataTransfer.effectAllowed = 'move';
            setTimeout(() => draggedItem.style.opacity = '0.5', 0);
        }
    });

    grid.addEventListener('dragend', (e) => {
        if (draggedItem) {
            setTimeout(() => {
                draggedItem.style.opacity = '1';
                draggedItem = null;
                saveOrder();
            }, 0);
        }
    });

    grid.addEventListener('dragover', (e) => {
        e.preventDefault();
        const afterElement = getDragAfterElement(grid, e.clientY);
        if (draggedItem) {
            if (afterElement == null) {
                grid.appendChild(draggedItem);
            } else {
                grid.insertBefore(draggedItem, afterElement);
            }
        }
    });

    function getDragAfterElement(container, y) {
        const draggableElements = [...container.querySelectorAll('.draggable-card:not(.dragging)')];
        return draggableElements.reduce((closest, child) => {
            const box = child.getBoundingClientRect();
            const offset = y - box.top - box.height / 2;
            if (offset < 0 && offset > closest.offset) {
                return { offset: offset, element: child };
            } else {
                return closest;
            }
        }, { offset: Number.NEGATIVE_INFINITY }).element;
    }

    async function saveOrder() {
        const order = [...grid.querySelectorAll('.draggable-card')].map(c => c.dataset.id);
        try {
            await fetch('/endpoint/reorder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ order })
            });
        } catch (e) {
            showToast('Error saving order', 'error');
        }
    }
}

function initContextMenu(container = document) {
    const menu = container.querySelector('#context-menu') || document.getElementById('context-menu');
    if (!menu) return;

    if (window._ctxMenuInit) return;
    window._ctxMenuInit = true;

    document.addEventListener('contextmenu', (e) => {
        const card = e.target.closest('.endpoint-card');
        if (card) {
            e.preventDefault();
            const id = card.dataset.id;
            const name = card.dataset.name;

            menu.style.display = 'block';
            menu.style.left = e.pageX + 'px';
            menu.style.top = e.pageY + 'px';

            document.getElementById('ctx-edit').href = '/endpoint/edit/' + id;
            document.getElementById('ctx-test').onclick = () => { window.testEndpoint(id); menu.style.display = 'none'; };
            document.getElementById('ctx-clone').onclick = () => {
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = '/endpoint/clone/' + id;
                const csrfInput = document.createElement('input');
                csrfInput.type = 'hidden';
                csrfInput.name = 'csrf_token';
                csrfInput.value = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
                form.appendChild(csrfInput);
                document.body.appendChild(form);
                form.submit();
            };
            document.getElementById('ctx-delete').onclick = async () => {
                if (await hwConfirm('Delete endpoint ' + name + '?', { title: 'Delete Endpoint', okText: 'Delete' })) {
                    setTimeout(() => {
                        const form = document.createElement('form');
                        form.method = 'POST';
                        form.action = '/endpoint/delete/' + id;
                        const csrfInput = document.createElement('input');
                        csrfInput.type = 'hidden';
                        csrfInput.name = 'csrf_token';
                        csrfInput.value = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
                        form.appendChild(csrfInput);
                        document.body.appendChild(form);
                        form.submit();
                    }, 300);
                }
            };
        } else {
            menu.style.display = 'none';
        }
    });

    document.addEventListener('click', () => {
        menu.style.display = 'none';
    });
}

window.startLoading = function () {
    const bar = document.getElementById('loading-bar');
    if (!bar) return;
    bar.style.width = '0%';
    setTimeout(() => bar.style.width = '30%', 10);
    setTimeout(() => bar.style.width = '70%', 200);
};

window.stopLoading = function () {
    const bar = document.getElementById('loading-bar');
    if (!bar) return;
    bar.style.width = '100%';
    setTimeout(() => bar.style.width = '0%', 500);
};

if (!window.originalFetch) {
    window.originalFetch = window.fetch;
    window.fetch = function (resource, options) {
        if (options && ['POST', 'PUT', 'DELETE'].includes(options.method)) {
            if (!options.headers) options.headers = {};
            const csrfTokenMeta = document.querySelector('meta[name="csrf-token"]');
            if (csrfTokenMeta) {
                options.headers['X-CSRFToken'] = csrfTokenMeta.getAttribute('content');
            }
        }
        startLoading();
        return window.originalFetch.apply(this, arguments).finally(() => stopLoading());
    };
}

async function initAutoSave() {
    const form = document.getElementById('endpoint-form');
    if (!form) return;

    const formId = window.location.pathname;
    const saved = localStorage.getItem('autosave_' + formId);
    if (saved) {
        const data = JSON.parse(saved);
        const isEditPage = window.location.pathname.includes('/endpoint/edit/');

        const proceed = isEditPage || await hwConfirm('Restore unsaved changes?', {
            title: 'Unsaved Changes',
            okText: 'Restore',
            cancelText: 'Discard'
        });

        if (proceed) {
            Object.keys(data).forEach(key => {
                const el = form.elements[key];
                if (el) {
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        el.checked = (data[key] === 'true' || data[key] === 'on' || data[key] === true);
                    } else {
                        el.value = data[key];
                    }
                }
            });
            if (window.updatePreview) window.updatePreview();
            if (window.toggleBearerMgmt) window.toggleBearerMgmt();
            if (window.toggleAdvanced) window.toggleAdvanced();
            if (window.toggleAI) window.toggleAI();

            if (isEditPage) showToast('Auto-restored unsaved changes', 'info');
        } else {
            localStorage.removeItem('autosave_' + formId);
        }
    }

    form.addEventListener('input', () => {
        const data = {};
        new FormData(form).forEach((value, key) => data[key] = value);
        localStorage.setItem('autosave_' + formId, JSON.stringify(data));
    });

    form.addEventListener('submit', () => {
        localStorage.removeItem('autosave_' + formId);
    });
}

function initFeedback() {
    const form = document.getElementById('feedback-form');
    if (!form) return;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const message = form.elements['message'].value;
        try {
            await fetch('/api/feedback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message,
                    ua: navigator.userAgent,
                    url: window.location.href
                })
            });
            showToast('Feedback sent! Thank you.', 'success');
            const modal = bootstrap.Modal.getInstance(document.getElementById('feedbackModal'));
            if (modal) modal.hide();
            form.reset();
        } catch (e) {
            showToast('Error sending feedback', 'danger');
        }
    });
}

function initPullToRefresh() {
    let touchStart = 0;
    let touchEnd = 0;

    window.addEventListener('touchstart', (e) => {
        if (window.scrollY === 0) touchStart = e.touches[0].clientY;
    }, { passive: true });

    window.addEventListener('touchmove', (e) => {
        if (window.scrollY === 0) {
            touchEnd = e.touches[0].clientY;
        }
    }, { passive: true });

    window.addEventListener('touchend', () => {
        if (window.scrollY === 0 && touchEnd - touchStart > 150) {
            showToast('Refreshing...', 'info');
            window.location.reload();
        }
        touchStart = 0;
        touchEnd = 0;
    });
}

function initOnboarding() {
    const onboardingModal = document.getElementById('onboardingModal');
    if (!onboardingModal) return;

    const seen = localStorage.getItem('onboarding_seen');
    if (!seen) {
        new bootstrap.Modal(onboardingModal).show();
        localStorage.setItem('onboarding_seen', 'true');
    }
}

const NOTIFICATION_READ_KEY = 'hookwise.notifications.read.v1';

function getReadNotificationIds() {
    try {
        const stored = JSON.parse(localStorage.getItem(NOTIFICATION_READ_KEY) || '[]');
        return new Set(Array.isArray(stored) ? stored : []);
    } catch (_error) {
        return new Set();
    }
}

function saveReadNotificationIds(ids) {
    try {
        localStorage.setItem(NOTIFICATION_READ_KEY, JSON.stringify([...ids].slice(-200)));
    } catch (_error) {
        // Notification state is non-critical when storage is unavailable.
    }
}

function updateNotificationCount(center, readIds) {
    const items = [...center.querySelectorAll('.notification-item[data-notification-id]')];
    let unread = 0;
    items.forEach(item => {
        const isRead = readIds.has(item.dataset.notificationId);
        item.classList.toggle('is-read', isRead);
        if (!isRead) unread += 1;
    });

    const count = center.querySelector('#notification-count');
    const toggle = center.querySelector('#notificationMenu');
    const markAll = center.querySelector('#mark-notifications-read');
    if (count) {
        count.textContent = String(unread);
        count.classList.toggle('d-none', unread === 0);
    }
    if (toggle) {
        toggle.setAttribute(
            'aria-label',
            unread === 0 ? 'Open notifications' : `Open notifications, ${unread} unread`
        );
    }
    if (markAll) markAll.disabled = unread === 0;
}

function markAllNotificationsRead(center) {
    const readIds = getReadNotificationIds();
    center.querySelectorAll('.notification-item[data-notification-id]').forEach(item => {
        readIds.add(item.dataset.notificationId);
    });
    saveReadNotificationIds(readIds);
    updateNotificationCount(center, readIds);
}

function initNotifications() {
    const center = document.querySelector('.notification-center');
    if (center && center.dataset.initialized !== 'true') {
        center.dataset.initialized = 'true';
        updateNotificationCount(center, getReadNotificationIds());

        center.querySelector('#mark-notifications-read')?.addEventListener('click', event => {
            event.preventDefault();
            event.stopPropagation();
            markAllNotificationsRead(center);
        });

        center.querySelector('#notificationMenu')?.addEventListener('shown.bs.dropdown', () => {
            markAllNotificationsRead(center);
        });

        center.querySelectorAll('.notification-item[data-notification-id]').forEach(item => {
            item.addEventListener('click', () => {
                const readIds = getReadNotificationIds();
                readIds.add(item.dataset.notificationId);
                saveReadNotificationIds(readIds);
            });
        });
    }

    if ('Notification' in window && Notification.permission === 'default') {
        if (document.getElementById('enable-browser-notifications')) return;
        const btn = document.createElement('button');
        btn.id = 'enable-browser-notifications';
        btn.className = 'btn btn-sm btn-link text-info p-0 ms-2';
        btn.textContent = 'Enable Notifications';
        btn.onclick = () => {
            Notification.requestPermission().then(p => {
                if (p === 'granted') showToast('Notifications enabled', 'success');
                btn.remove();
            });
        };
        const container = document.getElementById('socket-status')?.parentElement;
        if (container) container.appendChild(btn);
    }
}

window.notifyFailure = function (data) {
    if (data.level === 'danger' && 'Notification' in window && Notification.permission === 'granted') {
        new Notification('HookWise Alert: ' + data.config_name, {
            body: data.message,
            icon: '/static/img/logo.png'
        });
    }
};

window.addEventListener('scroll', () => {
    const btn = document.getElementById('back-to-top');
    if (btn) {
        if (window.scrollY > 300) {
            btn.classList.remove('d-none');
        } else {
            btn.classList.add('d-none');
        }
    }
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const modals = document.querySelectorAll('.modal.show');
        modals.forEach(m => {
            const instance = bootstrap.Modal.getInstance(m);
            if (instance) instance.hide();
        });
    }
});
