var hookwiseCwUrl = document.querySelector('meta[name="hookwise-cw-url"]')?.content || '';
    async function quickUpdate(id, field, value) {
        const select = event.target;
        if (value === 'LOAD') {
            select.innerHTML = '<option value="">Loading...</option>';
            try {
                let endpoint = '';
                if (field === 'board') endpoint = '/api/cw/boards';
                else if (field === 'priority') endpoint = '/api/cw/priorities';
                else if (field === 'status') {
                    const card = select.closest('.endpoint-card');
                    const boardName = card.dataset.board;
                    const boardsRes = await fetch('/api/cw/boards');
                    const boards = await boardsRes.json();
                    const board = boards.find(b => b.name === boardName);
                    if (board) endpoint = `/api/cw/statuses/${board.id}`;
                }

                if (!endpoint) throw new Error('Could not determine endpoint');

                const res = await fetch(endpoint);
                const items = await res.json();
                select.innerHTML = '<option value="">-- Default --</option>';
                items.forEach(item => {
                    const opt = document.createElement('option');
                    opt.value = item.name;
                    opt.textContent = item.name;
                    select.appendChild(opt);
                });
            } catch (e) {
                showToast('Error loading ' + field + 's', 'danger');
            }
            return;
        }

        try {
            const res = await fetch(`/endpoint/quick-update/${id}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ field, value })
            });
            const data = await res.json();
            if (data.status === 'success') {
                showToast(field.charAt(0).toUpperCase() + field.slice(1) + ' updated', 'success');
                if (field === 'board') {
                    select.closest('.endpoint-card').dataset.board = value;
                }
            } else {
                showToast('Error: ' + data.message, 'danger');
            }
        } catch (e) {
            showToast('Connection error', 'danger');
        }
    }

    function confirmRotate(id, name) {
        document.getElementById('rotate-endpoint-name').textContent = name;
        document.getElementById('rotate-form').action = `/endpoint/rotate-token/${id}`;
        new bootstrap.Modal(document.getElementById('rotateModal')).show();
    }

    // Live Dashboard Logic (Refined)
    window.dashboardState = window.dashboardState || {
        isPaused: false,
        absoluteTime: false,
        currentDashboardPayload: null,
        isDashboardPretty: true,
        chartPeriod: 'daily'
    };

    function updateDashboardPayloadDisplay() {
        const display = document.getElementById('payload-display');
        const toggleBtn = document.getElementById('pretty-toggle');
        if (window.dashboardState.isDashboardPretty) {
            display.textContent = JSON.stringify(window.dashboardState.currentDashboardPayload, null, 2);
            toggleBtn.textContent = 'Minified';
        } else {
            display.textContent = JSON.stringify(window.dashboardState.currentDashboardPayload);
            toggleBtn.textContent = 'Pretty';
        }
        Prism.highlightElement(display);
    }

    document.getElementById('pretty-toggle').addEventListener('click', () => {
        window.dashboardState.isDashboardPretty = !window.dashboardState.isDashboardPretty;
        updateDashboardPayloadDisplay();
    });

    function initLiveActivityStream() {
        const socket = getSocket();
        if (socket) {
            const previous = window.dashboardLiveSocketHandlers;
            if (previous) {
                socket.off('connect', previous.connect);
                socket.off('disconnect', previous.disconnect);
                socket.off('new_log', previous.newLog);
                socket.io?.off('reconnect_attempt', previous.reconnect);
            }
        }

        const logContainer = document.getElementById('log-container');
        if (!logContainer || logContainer.dataset.initialized) return;
        logContainer.dataset.initialized = 'true';
        window.activityStream?.init?.();

        const pauseBtn = document.getElementById('pause-button');
        const timeBtn = document.getElementById('time-toggle');
        const clearBtn = document.getElementById('clear-logs');
        const fullscreenBtn = document.getElementById('fullscreen-button');
        const logFilter = document.getElementById('log-filter');

        if (fullscreenBtn) {
            fullscreenBtn.addEventListener('click', () => {
                const logCard = logContainer.closest('.card');
                if (!document.fullscreenElement) {
                    logCard.requestFullscreen().catch(err => {
                        showToast(`Error attempting to enable full-screen mode: ${err.message}`, 'danger');
                    });
                } else {
                    document.exitFullscreen();
                }
            });
        }

        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                clearLog();
            });
        }

        const btnTrigger = document.getElementById('btn-trigger-timeout');
        const btnClear = document.getElementById('btn-clear-stream');
        if (btnTrigger) btnTrigger.addEventListener('click', triggerTimeoutCheck);
        if (btnClear) btnClear.addEventListener('click', clearLog);

        function clearLog() {
            logContainer.innerHTML = '<div class="text-secondary opacity-50 italic">Log cleared. Waiting for new activity...</div>';
        }

        async function triggerTimeoutCheck() {
            const btn = document.getElementById('btn-trigger-timeout');
            if (!btn) return;
            
            // Backup original SVG content
            const originalHtml = btn.innerHTML;
            
            try {
                btn.disabled = true;
                // Swap to spinning refresh icon
                btn.innerHTML = `
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" fill="currentColor" class="bi bi-arrow-clockwise spin" viewBox="0 0 16 16">
                        <path fill-rule="evenodd" d="M8 3a5 5 0 1 0 4.546 2.914.5.5 0 0 1 .908-.417A6 6 0 1 1 8 2v1z"/>
                        <path d="M8 4.466V.534a.25.25 0 0 1 .41-.192l2.36 1.966c.12.1.12.284 0 .384L8.41 4.658A.25.25 0 0 1 8 4.466z"/>
                    </svg>
                `;
                
                const res = await fetch('/api/activity/trigger-timeout-check', { method: 'POST' });
                const result = await res.json();
                
                if (result.status === 'success') {
                    showToast('Timeout check triggered.', 'success');
                } else {
                    showToast(result.message || 'Failed to trigger check.', 'danger');
                }
            } catch (err) {
                console.error('Trigger failed', err);
                showToast('API Communication error.', 'danger');
            } finally {
                setTimeout(() => {
                    btn.disabled = false;
                    btn.innerHTML = originalHtml;
                }, 2000);
            }
        }

        if (pauseBtn) {
            pauseBtn.addEventListener('click', () => {
                window.dashboardState.isPaused = !window.dashboardState.isPaused;
                pauseBtn.classList.toggle('btn-primary', window.dashboardState.isPaused);
                pauseBtn.classList.toggle('btn-outline-secondary', !window.dashboardState.isPaused);
            });
        }

        if (timeBtn) {
            timeBtn.addEventListener('click', () => {
                window.dashboardState.absoluteTime = !window.dashboardState.absoluteTime;
                timeBtn.classList.toggle('btn-primary', window.dashboardState.absoluteTime);
                timeBtn.classList.toggle('btn-outline-secondary', !window.dashboardState.absoluteTime);
                // Refresh visible logs to update time format
                logContainer.querySelectorAll('.log-entry').forEach(entry => {
                    const ts = entry.dataset.timestamp;
                    if (ts) {
                        const timeSpan = entry.querySelector('.text-secondary');
                        if (timeSpan) {
                            const date = new Date(ts);
                            timeSpan.textContent = window.dashboardState.absoluteTime ? `[${date.toLocaleTimeString()}]` : `[${getRelativeTime(date)}]`;
                        }
                    }
                });
            });
        }

        const setSocketStatus = (text, className) => {
            const statusBadge = document.getElementById('socket-status');
            if (statusBadge) {
                statusBadge.textContent = text;
                statusBadge.className = className;
            }
        };

        function addLogToStream(data, isInitialHistory = false) {
            if (!isInitialHistory && window.dashboardState.isPaused) return;
            if (window.activityStream && !window.activityStream.accept(data, isInitialHistory)) return;

            // Purge the "Initializing" placeholder if it exists
            const placeholder = logContainer.querySelector('.italic');
            if (placeholder) {
                logContainer.innerHTML = '';
            }

            const logEntry = document.createElement('div');
            logEntry.className = `log-entry mb-1 p-2 border-bottom border-light border-opacity-5 hover-bg-light animate-fade-in`;
            logEntry.dataset.timestamp = data.timestamp;
            logEntry.dataset.activityKey = data.id || `${data.request_id || ''}:${data.config_name || ''}:${data.timestamp || ''}`;

            const isError = data.level === 'warning' || data.level === 'error';
            const statusColor = isError ? 'danger' : 'success';

            const date = new Date(data.timestamp);
            const timeStr = window.dashboardState.absoluteTime ? `[${date.toLocaleTimeString()}]` : `[${getRelativeTime(date)}]`;

            let flowHtml = `
                <div class="d-flex align-items-center gap-2 small">
                    <span class="text-secondary font-monospace" style="font-size: 0.7rem;">${timeStr}</span>
                    <span class="fw-bold text-light">${escapeHtml(data.config_name)}</span>
                    
                    <!-- Flow Icons (SVG Bootstrap Icons) -->
                    <div class="d-flex align-items-center bg-dark bg-opacity-50 rounded px-2 py-1 ms-2 border border-secondary border-opacity-10" style="gap: 6px;">
                        <!-- Step 1: Payload Received (Transmit) -->
                        <span class="text-success opacity-75 d-flex" title="Payload Received">
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16">
                                <path d="M2 0a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V2a2 2 0 0 0-2-2H2zm6 10.792l-4.25-4.25a.5.5 0 0 1 .708-.708L8 9.378l3.542-3.544a.5.5 0 0 1 .708.708L8 10.792z"/>
                            </svg>
                        </span>
                        <span class="text-secondary opacity-25" style="font-size: 0.6rem;">&rarr;</span>
                        
                        <!-- Step 2: Processing (Gear) -->
                        <span class="text-${statusColor} d-flex ${isError ? 'spin text-danger' : 'opacity-75'}" title="${escapeHtml(data.message)}">
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16">
                                <path d="M9.405 1.05c-.413-1.4-2.397-1.4-2.81 0l-.1.34a1.464 1.464 0 0 1-2.105.872l-.31-.17c-1.283-.698-2.686.705-1.987 1.987l.17.31a1.464 1.464 0 0 1-.872 2.105l-.34.1c-1.4.413-1.4 2.397 0 2.81l.34.1a1.464 1.464 0 0 1 .872 2.105l-.17.31c-.698 1.283.705 2.686 1.987 1.987l.31-.17a1.464 1.464 0 0 1 2.105.872l.1.34c.413 1.4 2.397 1.4 2.81 0l.1-.34a1.464 1.464 0 0 1 2.105-.872l.31.17c1.283.698 2.686-.705 1.987-1.987l-.17-.31a1.464 1.464 0 0 1 .872-2.105l.34-.1c1.4-.413 1.4-2.397 0-2.81l-.34-.1a1.464 1.464 0 0 1-.872-2.105l.17-.31c.698-1.283-.705-2.686-1.987-1.987l-.31.17a1.464 1.464 0 0 1-2.105-.872l-.1-.34zM8 10.93a2.929 2.929 0 1 1 0-5.86 2.929 2.929 0 0 1 0 5.858z"/>
                            </svg>
                        </span>
                        <span class="text-secondary opacity-25" style="font-size: 0.6rem;">&rarr;</span>
                        
                        <!-- Step 3: Action (Ticket) -->
                        <span class="text-${data.ticket_id ? 'primary' : 'secondary opacity-50'} d-flex" title="${data.ticket_id ? 'Ticket Created' : 'No Action'}">
                            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" viewBox="0 0 16 16">
                                <path d="M0 4.5A1.5 1.5 0 0 1 1.5 3h13A1.5 1.5 0 0 1 16 4.5V6a.5.5 0 0 1-.5.5 1.5 1.5 0 0 0 0 3 .5.5 0 0 1 .5.5v1.5a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 0 11.5V10a.5.5 0 0 1 .5-.5 1.5 1.5 0 1 0 0-3 .5.5 0 0 1-.5-.5V4.5Zm4 1h8v1H4v-1Zm0 3h8v1H4v-1Zm0 3h8v1H4v-1Z"/>
                            </svg>
                        </span>
                    </div>

                     <span class="text-${statusColor} ms-2 text-truncate" style="max-width: 300px;">
                        ${escapeHtml(data.message)}
                    </span>
                    
                    ${data.ticket_id ? `<a href="${hookwiseCwUrl}/service/tickets/${data.ticket_id}" target="_blank" class="badge bg-primary text-decoration-none ms-auto">#${data.ticket_id}</a>` : ''}
                    
                    <button class="btn btn-sm btn-link text-secondary ms-auto p-0 log-expand-payload" aria-label="Expand payload">
                        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" fill="currentColor" class="bi bi-arrows-angle-expand" viewBox="0 0 16 16">
                            <path fill-rule="evenodd" d="M5.828 10.172a.5.5 0 0 0-.707 0l-4.096 4.096V11.5a.5.5 0 0 0-1 0v3.975a.5.5 0 0 0 .5.5H4.5a.5.5 0 0 0 0-1H1.732l4.096-4.096a.5.5 0 0 0 0-.707zm4.344-4.344a.5.5 0 0 0 .707 0l4.096-4.096V4.5a.5.5 0 1 0 1 0V.525a.5.5 0 0 0-.5-.5H11.5a.5.5 0 1 0 0 1h2.768l-4.096 4.096a.5.5 0 0 0 0 .707z"/>
                        </svg>
                    </button>
                </div>
            `;
            logEntry.innerHTML = flowHtml;
            const payloadButton = logEntry.querySelector('.log-expand-payload');
            if (payloadButton) payloadButton.dataset.payload = JSON.stringify(data.payload);
            window.activityStream?.decorate?.(logEntry, data);

            if (isInitialHistory) {
                logContainer.appendChild(logEntry);
            } else {
                logContainer.insertBefore(logEntry, logContainer.firstChild);
            }

            if (!isInitialHistory) {
                // Future: Add scroll-lock logic here if needed
            }

            if (!isInitialHistory && window.notifyFailure && isError) {
                const notifyData = {...data, level: data.level === 'error' ? 'danger' : data.level};
                window.notifyFailure(notifyData);
            }

            const maxActivityEntries = window.activityStream?.maxEntries?.() || 200;
            while (logContainer.childNodes.length > maxActivityEntries) {
                if (isInitialHistory) {
                    logContainer.removeChild(logContainer.firstChild);
                } else {
                    logContainer.removeChild(logContainer.lastChild);
                }
            }
        }

        async function loadActivityHistory() {
            try {
                const res = await fetch('/api/activity/stream?limit=200');
                const result = await res.json();
                const history = result.events || [];
                if (history.length > 0) {
                    history.forEach(log => addLogToStream(log, true));
                }
            } catch (err) {
                console.error('Failed to load activity history', err);
            }
        }

        if (socket) {
            const handlers = {
                connect: () => setSocketStatus('Live', 'badge bg-success'),
                disconnect: () => setSocketStatus('Disconnected', 'badge bg-danger'),
                reconnect: attempt => setSocketStatus(`Reconnecting ${attempt}`, 'badge bg-warning text-dark'),
                newLog: data => addLogToStream(data),
            };
            window.dashboardLiveSocketHandlers = handlers;
            socket.on('connect', handlers.connect);
            socket.on('disconnect', handlers.disconnect);
            socket.io?.on('reconnect_attempt', handlers.reconnect);
            socket.on('new_log', handlers.newLog);
            if (socket.connected) handlers.connect();
            else handlers.disconnect();
        }

        // Load history initially
        loadActivityHistory();


        // Log Filter Logic
        if (logFilter) {
            logFilter.addEventListener('input', (e) => {
                const term = e.target.value.toLowerCase();
                logContainer.querySelectorAll('.log-entry').forEach(entry => {
                    entry.style.display = entry.innerText.toLowerCase().includes(term) ? 'block' : 'none';
                });
            });
        }

        // Delegated listener for payload modal to avoid inline onclick escapes
        logContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.log-expand-payload');
            if (btn) {
                const payload = btn.getAttribute('data-payload');
                if (window.showPayloadModal) window.showPayloadModal(payload);
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initLiveActivityStream, { once: true });
    } else {
        initLiveActivityStream();
    }

    function copyModalPayload() {
        const text = document.getElementById('payload-display').textContent;
        navigator.clipboard.writeText(text);
        showToast('Payload copied to clipboard!', 'success');
    }

    function testEndpoint(id) {
        fetch(`/endpoint/test/${id}`, { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    showToast('Test webhook queued!', 'success');
                } else {
                    showToast('Error: ' + data.message, 'danger');
                }
            })
            .catch(error => {
                showToast('Connection error', 'danger');
            });
    }

    function refreshStats() {
        if (!document.getElementById('stat-created')) return;
        if (window.dashboardState.isRefreshing) return;
        window.dashboardState.isRefreshing = true;

        Promise.all([
            fetch('/api/stats').then(res => res.json()),
            fetch(`/api/stats/history?period=${window.dashboardState.chartPeriod || 'daily'}`).then(res => res.json())
        ]).then(([data, historyData]) => {
            const elCreated = document.getElementById('stat-created');
            const elUpdated = document.getElementById('stat-updated');
            const elClosed = document.getElementById('stat-closed');
            const elRate = document.getElementById('stat-rate');
            const elFailed = document.getElementById('stat-failed');
            const elDlq = document.getElementById('stat-dlq');
            const elNoAction = document.getElementById('stat-noaction');
            const elLatency = document.getElementById('stat-latency');

            if (elCreated) elCreated.textContent = data.created_today;
            if (elUpdated) elUpdated.textContent = data.updated_today;
            if (elClosed) elClosed.textContent = data.closed_today;
            if (elRate) elRate.textContent = data.success_rate;
            if (elFailed) elFailed.textContent = data.failed_today;
            if (elDlq) elDlq.textContent = data.dlq_today;
            if (elNoAction) elNoAction.textContent = data.non_action_today;
            if (elLatency) elLatency.textContent = data.avg_processing_time;

            if (typeof MiniChart !== 'undefined') {
                const labels = historyData.map(d => d.date);
                if (MiniChart.renderMultiBar && historyData.length > 0 && historyData[0].created !== undefined) {
                    const datasets = [
                        { name: 'Created', data: historyData.map(d => d.created), color: '#3b82f6' },
                        { name: 'Updated', data: historyData.map(d => d.updated), color: '#f59e0b' },
                        { name: 'Closed', data: historyData.map(d => d.closed), color: '#10b981' }
                    ];
                    MiniChart.renderMultiBar('historyChart', labels, datasets);
                } else {
                    const counts = historyData.map(d => d.count || d.created || 0);
                    MiniChart.renderBar('historyChart', labels, counts);
                }
            }
        })
            .catch(err => console.error('Error fetching stats:', err))
            .finally(() => {
                window.dashboardState.isRefreshing = false;
            });
    }

    async function toggleMaintenance() {
        try {
            const res = await fetch('/admin/maintenance', { method: 'POST' });
            const data = await res.json();
            showToast('Maintenance mode ' + (data.maintenance_mode ? 'ENABLED' : 'DISABLED'), data.maintenance_mode ? 'warning' : 'success');
        } catch (e) {
            showToast('Error toggling maintenance mode', 'danger');
        }
    }

    function showPayloadModal(payloadStr) {
        try {
            // Handle potential double-encoding or already object
            let payload = typeof payloadStr === 'string' ? JSON.parse(payloadStr) : payloadStr;
            window.dashboardState.currentDashboardPayload = payload;
            window.dashboardState.isDashboardPretty = true;
            updateDashboardPayloadDisplay();
            new bootstrap.Modal(document.getElementById('payloadModal')).show();
        } catch (e) {
            console.error("Payload parse error", e);
            showToast('Error displaying payload', 'danger');
        }
    }

    function showSecurityHud(id, status, message, lastIp, rotatedAt) {
        const modal = new bootstrap.Modal(document.getElementById('securityHudModal'));
        const icon = document.getElementById('hud-status-icon');
        const text = document.getElementById('hud-status-text');
        const msg = document.getElementById('hud-message');
        const ip = document.getElementById('hud-last-ip');
        const age = document.getElementById('hud-token-age');

        // Status Logic
        if (status === 'OK') {
            icon.innerHTML = '<i class="fas fa-check-circle text-success"></i>';
            text.className = 'fw-bold text-success';
            text.textContent = 'System Healthy';
        } else if (status === 'ERROR') {
            icon.innerHTML = '<i class="fas fa-exclamation-triangle text-danger"></i>';
            text.className = 'fw-bold text-danger';
            text.textContent = 'Security Alert';
        } else {
            icon.innerHTML = '<i class="fas fa-info-circle text-warning"></i>';
            text.className = 'fw-bold text-warning';
            text.textContent = 'Warning';
        }

        msg.textContent = message || 'No active security notifications.';
        ip.textContent = lastIp || 'Unknown';

        if (rotatedAt && rotatedAt !== 'None') {
            const rotatedDate = new Date(rotatedAt);
            const days = Math.floor((new Date() - rotatedDate) / (1000 * 60 * 60 * 24));
            age.textContent = `${days} days ago`;
        } else {
            age.textContent = 'Never rotated';
        }

        modal.show();

        // Fetch LLM health asynchronously when HUD opens
        const llmStatus = document.getElementById('hud-llm-status');
        const llmModelRow = document.getElementById('hud-llm-model-row');
        const llmModel = document.getElementById('hud-llm-model');
        if (llmStatus) {
            llmStatus.textContent = '…';
            fetch('/api/health/llm').then(r => r.json()).then(d => {
                if (d.status === 'ok') {
                    llmStatus.textContent = `✅ OK (${d.response_ms}ms)`;
                    llmStatus.className = 'font-monospace small text-success';
                    if (d.model) {
                        llmModel.textContent = d.model;
                        llmModelRow.style.display = '';
                    }
                } else {
                    llmStatus.textContent = '❌ Offline';
                    llmStatus.className = 'font-monospace small text-danger';
                    llmModelRow.style.display = 'none';
                }
            }).catch(() => {
                llmStatus.textContent = '⚠ Unreachable';
                llmStatus.className = 'font-monospace small text-warning';
            });
        }
    }

    // Unified initialization for first-load and HTMX swaps
    function initSparklinesAndStats() {
        // Only run if on dashboard or elements exist
        if (!document.getElementById('stat-created') && !document.querySelector('canvas[data-sparkline]')) {
            return;
        }

        if (window.dashboardState.isInitialized) return;
        window.dashboardState.isInitialized = true;

        if (window.statsInterval) clearInterval(window.statsInterval);

        refreshStats();
        window.statsInterval = setInterval(refreshStats, 30000); // Every 30 seconds

        // Initialize Sparklines
        document.querySelectorAll('canvas[data-sparkline]').forEach(canvas => {
            const dataStr = canvas.getAttribute('data-sparkline');
            if (!dataStr) return;
            const data = JSON.parse(dataStr);
            if (typeof MiniChart !== 'undefined') {
                MiniChart.renderLine(canvas.id, data);
            }
        });

        // Set maintenance toggle state
        fetch('/admin/maintenance').then(r => r.json()).then(d => {
            const toggle = document.getElementById('maintenance-toggle');
            if (toggle) toggle.checked = d.maintenance_mode;
        }).catch(() => { });

        // Reset the lock after a small delay to handle intentional re-initialization (e.g. HTMX navigation)
        setTimeout(() => { window.dashboardState.isInitialized = false; }, 500);
    }

    document.addEventListener('DOMContentLoaded', initSparklinesAndStats);
    document.addEventListener('htmx:load', initSparklinesAndStats);
