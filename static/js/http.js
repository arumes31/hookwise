(() => {
    'use strict';

    const nativeFetch = window.fetch.bind(window);
    const safeMethods = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);
    let sessionNoticeShown = false;

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    }

    function notify(message, type = 'danger') {
        if (typeof window.showToast === 'function') window.showToast(message, type);
    }

    async function handleAuthFailure(response) {
        if (response.status === 401) {
            if (!sessionNoticeShown) {
                sessionNoticeShown = true;
                notify('Your session expired. Redirecting to sign in…', 'warning');
                window.dispatchEvent(new CustomEvent('hookwise:session-expired'));
                window.setTimeout(() => {
                    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
                }, 500);
            }
            return;
        }

        if (response.status === 400 || response.status === 403) {
            const payload = await response.clone().json().catch(() => null);
            const message = String(payload?.message || payload?.error || '');
            if (/csrf/i.test(message)) {
                notify('This page security token expired. Refresh the page and try again.', 'warning');
                window.dispatchEvent(new CustomEvent('hookwise:csrf-expired'));
            }
        }
    }

    window.fetch = async (input, init = {}) => {
        const request = input instanceof Request ? input : null;
        const method = String(init.method || request?.method || 'GET').toUpperCase();
        const target = new URL(request?.url || String(input), window.location.href);

        if (target.origin === window.location.origin && !safeMethods.has(method)) {
            const headers = new Headers(init.headers || request?.headers || {});
            if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', csrfToken());
            init = { ...init, headers };
        }

        const response = await nativeFetch(input, init);
        await handleAuthFailure(response);
        return response;
    };

    window.fetchJSON = async (input, init = {}) => {
        const headers = new Headers(init.headers || {});
        if (!headers.has('Accept')) headers.set('Accept', 'application/json');
        const response = await window.fetch(input, { ...init, headers });
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
            const error = new Error(payload?.message || payload?.error || `Request failed (${response.status})`);
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    };
})();
