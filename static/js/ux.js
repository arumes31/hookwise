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
});

// Toast System
function initToasts() {
    // Bootstrap toast initialization if needed
    console.log('Toasts initialized');
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `alert alert-${type === 'error' ? 'danger' : type} alert-dismissible fade show shadow-sm`;
    toast.style.minWidth = '250px';
    toast.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
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
    if (!searchInput) return;

    searchInput.addEventListener('input', (e) => {
        const term = e.target.value.toLowerCase();
        document.querySelectorAll('.endpoint-card').forEach(card => {
            const name = card.dataset.name.toLowerCase();
            const id = card.dataset.id.toLowerCase();
            card.closest('.col-md-6').style.display =
                (name.includes(term) || id.includes(term)) ? 'block' : 'none';
        });
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
    const footer = document.querySelector('footer .health-indicators');
    if (!footer) return;

    const updateHealth = async () => {
        try {
            const resp = await fetch('/health/services');
            const data = await resp.json();

            Object.keys(data).forEach(service => {
                const el = document.getElementById(`health-${service}`);
                if (el) {
                    el.className = `heartbeat-dot heartbeat-${data[service]}`;
                    el.title = `${service.toUpperCase()}: ${data[service].toUpperCase()}`;
                }
            });
        } catch (e) {
            console.error('Health check failed', e);
        }
    };

    updateHealth();
    setInterval(updateHealth, 30000);
}

// Transitions
function initTransitions() {
    document.querySelectorAll('a').forEach(link => {
        if (link.hostname === window.location.hostname && !link.hash && link.target !== '_blank') {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                const url = link.href;
                document.body.classList.add('fade-out');
                setTimeout(() => window.location.href = url, 300);
            });
        }
    });
    document.body.classList.add('fade-in');
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
        showToast(data.message, 'info');
    } catch (e) {
        showToast('Error pausing endpoints', 'error');
    }
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
        const val = path.split('.').reduce((o, i) => o ? o[i] : undefined, obj);
        resultEl.className = 'mt-2 small ' + (val !== undefined ? 'text-success' : 'text-danger');
        resultEl.textContent = val !== undefined ? `Found value: ${val}` : 'Field not found in payload';
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
