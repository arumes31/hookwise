/**
 * HookWise UX Logic
 * Handles search, filtering, bulk actions, and validator tool.
 */

document.addEventListener('DOMContentLoaded', () => {
    initSearch();
    initBulkActions();
    initServiceHealth();
    initToasts();
    initTransitions();
    initTooltips();
    initDragAndDrop();
    initContextMenu();
    initAutoSave();
    initFeedback();
    initPullToRefresh();
    initOnboarding();
    initNotifications();
});

// Tooltip System
function initTooltips() {
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-tooltip]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl, {
            title: tooltipTriggerEl.getAttribute('data-tooltip'),
            placement: 'top'
        });
    });
}

function initToasts() {
    console.log('Toasts initialized');
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `alert alert-${type === 'error' ? 'danger' : type} alert-dismissible fade show shadow-lg border-0 mb-2`;
    toast.style.minWidth = '300px';
    toast.style.backdropFilter = 'blur(10px)';
    toast.style.background = 'rgba(15, 23, 42, 0.9)';
    toast.style.color = 'white';
    
    let iconSvg = '';
    if (type === 'success') iconSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-check-circle-fill" viewBox="0 0 16 16"><path d="M16 8A8 8 0 1 1 0 8a8 8 0 0 1 16 0zm-3.97-3.03a.75.75 0 0 0-1.08.022L7.477 9.417 5.384 7.323a.75.75 0 0 0-1.06 1.06L6.97 11.03a.75.75 0 0 0 1.079-.02l3.992-4.99a.75.75 0 0 0-.01-1.05z"/></svg>';
    else if (type === 'danger' || type === 'error') iconSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-exclamation-triangle-fill" viewBox="0 0 16 16"><path d="M8.982 1.566a1.13 1.13 0 0 0-1.96 0L.165 13.233c-.457.778.091 1.767.98 1.767h13.713c.889 0 1.438-.99.98-1.767L8.982 1.566zM8 5c.535 0 .954.462.9.995l-.35 3.507a.552.552 0 0 1-1.1 0L7.1 5.995A.905.905 0 0 1 8 5zm.002 6a1 1 0 1 1 0 2 1 1 0 0 1 0-2z"/></svg>';
    else iconSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-info-circle-fill" viewBox="0 0 16 16"><path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm.93-9.412-1 4.705c-.07.34.029.533.304.533.194 0 .487-.07.686-.246l-.088.416c-.287.346-.92.598-1.465.598-.703 0-1.002-.422-.808-1.319l.738-3.468c.064-.293.006-.399-.287-.47l-.451-.081.082-.381 2.29-.287zM8 5.5a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/></svg>';

    toast.innerHTML = `
        <div class="d-flex align-items-center">
            <div class="me-3 text-${type === 'error' ? 'danger' : type}">
                ${iconSvg}
            </div>
            <div>${message}</div>
            <button type="button" class="btn-close btn-close-white ms-auto" data-bs-dismiss="alert"></button>
        </div>
    `;

    container.appendChild(toast);
    setTimeout(() => {
        const bsToast = new bootstrap.Alert(toast);
        bsToast.close();
    }, 5000);
}

// Endpoint Search
function initSearch() {
    const searchInput = document.getElementById('endpoint-search');
    const boardFilter = document.getElementById('board-filter');
    const statusFilter = document.getElementById('status-filter');
    if (!searchInput) return;

    document.addEventListener('keydown', (e) => {
        if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
            e.preventDefault();
            searchInput.focus();
        }
    });

    const filterEndpoints = () => {
        const term = searchInput.value.toLowerCase();
        const board = boardFilter.value;
        const status = statusFilter.value;

        document.querySelectorAll('.endpoint-card').forEach(card => {
            const name = card.dataset.name.toLowerCase();
            const id = card.dataset.id.toLowerCase();
            const cardBoard = card.dataset.board;
            const cardStatus = card.dataset.status;

            const matchesSearch = name.includes(term) || id.includes(term);
            const matchesBoard = !board || cardBoard === board;
            const matchesStatus = !status || cardStatus === status;

            card.closest('.col-md-6, .col-12').style.display =
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
function initBulkActions() {
    const mainCheck = document.getElementById('check-all');
    const bulkControls = document.getElementById('bulk-controls');
    if (!mainCheck) return;

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
function initServiceHealth() {
    const updateFavicon = (status) => {
        const canvas = document.createElement('canvas');
        canvas.width = 32;
        canvas.height = 32;
        const ctx = canvas.getContext('2d');
        
        ctx.beginPath();
        ctx.arc(16, 16, 14, 0, 2 * Math.PI);
        ctx.fillStyle = status === 'up' ? '#3fb950' : status === 'warning' ? '#d29922' : '#f85149';
        ctx.fill();
        
        ctx.fillStyle = 'white';
        ctx.font = 'bold 20px Inter';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('H', 16, 16);

        const link = document.querySelector("link[rel~='icon']");
        if (link) link.href = canvas.toDataURL('image/png');
    };

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
            console.error('Health check failed', e);
            updateFavicon('down');
        }
    };

    updateHealth();
    setInterval(updateHealth, 30000);
}

// Transitions
function initTransitions() {
    const savedView = localStorage.getItem('endpoint-view') || 'grid';
    if (window.toggleView) window.toggleView(savedView);

    document.body.classList.add('page-loaded');

    document.querySelectorAll('a').forEach(link => {
        if (link.hostname === window.location.hostname && !link.hash && link.target !== '_blank' && !link.getAttribute('href')?.startsWith('javascript:') && !link.getAttribute('href')?.startsWith('#')) {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const url = link.href;
                document.body.classList.remove('page-loaded');
                document.body.classList.add('page-leaving');
                setTimeout(() => window.location.href = url, 300);
            });
        }
    });
}

// Bulk Actions Implementation
window.bulkDelete = async function () {
    const checked = Array.from(document.querySelectorAll('.endpoint-check:checked')).map(c => c.dataset.id);
    if (!checked.length) return;

    if (confirm(`Delete ${checked.length} endpoints?`)) {
        try {
            const resp = await fetch('/endpoint/bulk/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: checked })
            });
            const data = await resp.json();
            if (data.status === 'success') {
                showToast(data.message, 'success');
                setTimeout(() => window.location.reload(), 1000);
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
            setTimeout(() => window.location.reload(), 1000);
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
            setTimeout(() => window.location.reload(), 1000);
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
            setTimeout(() => window.location.reload(), 500);
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
            setTimeout(() => window.location.reload(), 500);
        }
    } catch (e) {
        showToast('Error toggling pin', 'error');
    }
};

window.toggleView = function (view) {
    const grid = document.getElementById('endpoint-grid');
    if (!grid) return;

    const buttons = document.querySelectorAll('[onclick^="toggleView"]');
    buttons.forEach(btn => btn.classList.toggle('active', btn.getAttribute('onclick').includes(view)));

    if (view === 'list') {
        grid.querySelectorAll('.col-md-6').forEach(col => {
            col.classList.remove('col-md-6');
            col.classList.add('col-12');
        });
    } else {
        grid.querySelectorAll('.col-12').forEach(col => {
            col.classList.remove('col-12');
            col.classList.add('col-md-6');
        });
    }
    localStorage.setItem('endpoint-view', view);
};

window.revealToken = async function (id) {
    const el = document.getElementById('token-' + id);
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
window.getTroubleshootingLink = function (message) {
    const baseUrl = 'https://docs.connectwise.com/search?q=';
    if (message.includes('401')) return baseUrl + 'API+Authentication';
    if (message.includes('404')) return baseUrl + 'Resource+Not+Found';
    if (message.includes('error')) return baseUrl + 'Troubleshooting';
    return null;
};

window.copyToClipboard = function (text) {
    navigator.clipboard.writeText(text);
    showToast('Copied to clipboard!', 'success');
};

function initDragAndDrop() {
    const grid = document.getElementById('endpoint-grid');
    if (!grid) return;

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

function initContextMenu() {
    const menu = document.getElementById('context-menu');
    if (!menu) return;

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
                document.body.appendChild(form);
                form.submit();
            };
            document.getElementById('ctx-delete').onclick = () => {
                if (confirm('Delete endpoint ' + name + '?')) {
                    const form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '/endpoint/delete/' + id;
                    document.body.appendChild(form);
                    form.submit();
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

window.startLoading = function() {
    const bar = document.getElementById('loading-bar');
    if (!bar) return;
    bar.style.width = '0%';
    setTimeout(() => bar.style.width = '30%', 10);
    setTimeout(() => bar.style.width = '70%', 200);
};

window.stopLoading = function() {
    const bar = document.getElementById('loading-bar');
    if (!bar) return;
    bar.style.width = '100%';
    setTimeout(() => bar.style.width = '0%', 500);
};

const originalFetch = window.fetch;
window.fetch = function() {
    startLoading();
    return originalFetch.apply(this, arguments).finally(() => stopLoading());
};

function initAutoSave() {
    const form = document.getElementById('endpoint-form');
    if (!form) return;

    const formId = window.location.pathname;
    const saved = localStorage.getItem('autosave_' + formId);
    if (saved) {
        if (confirm('Restore unsaved changes?')) {
            const data = JSON.parse(saved);
            Object.keys(data).forEach(key => {
                const el = form.elements[key];
                if (el) el.value = data[key];
            });
            if (window.updatePreview) window.updatePreview();
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

function initNotifications() {
    if ('Notification' in window && Notification.permission === 'default') {
        const btn = document.createElement('button');
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

window.notifyFailure = function(data) {
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
