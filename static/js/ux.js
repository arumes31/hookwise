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
    // Bootstrap toast initialization if needed
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
                const dashEl = document.getElementById(`dash-health-${service}`);
                if (dashEl) {
                    dashEl.className = `heartbeat-dot heartbeat-${data[service]} mb-1 mx-auto`;
                    if (service === 'celery' && data.celery_active !== undefined) {
                        dashEl.parentElement.querySelector('.small').textContent = `Celery (${data.celery_active})`;
                    }
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
