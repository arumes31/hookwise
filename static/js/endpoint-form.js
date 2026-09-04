function updatePreview() {
        const prefix = document.getElementById('ticket_prefix').value;
        const preview = document.getElementById('summary-preview');
        preview.textContent = (prefix ? prefix + ' ' : '') + 'Source Name';
    }

    function initFormSelects() {
        const form = document.getElementById('endpoint-form');
        if (!form || form.dataset.initialized) return;
        form.dataset.initialized = 'true';

        form.addEventListener('input', () => {
            clearTimeout(window.saveStateTimeout);
            window.saveStateTimeout = setTimeout(saveState, 500);
        });
        saveState(); // Initial state

        document.getElementById('ticket_prefix').addEventListener('input', updatePreview);
        updatePreview();
        toggleBearerMgmt();

        const boardSelect = document.getElementById('board');
        const statusSelect = document.getElementById('status');
        const closeStatusSelect = document.getElementById('close_status');
        const typeSelect = document.getElementById('ticket_type');
        const subtypeSelect = document.getElementById('subtype');
        const itemSelect = document.getElementById('item');
        const prioritySelect = document.getElementById('priority');

        const boardHidden = document.getElementById('board_hidden')?.value || '';
        const statusHidden = document.getElementById('status_hidden')?.value || '';
        const closeStatusHidden = document.getElementById('close_status_hidden')?.value || '';
        const typeHidden = document.getElementById('type_hidden')?.value || '';
        const subtypeHidden = document.getElementById('subtype_hidden')?.value || '';
        const itemHidden = document.getElementById('item_hidden')?.value || '';
        const priorityHidden = document.getElementById('priority_hidden')?.value || '';

        // Cached boards list so loadBoardDetails doesn't re-fetch on every call
        let cachedBoards = null;

        const loadBoardDetails = async (boardName) => {
            if (!boardName) {
                [statusSelect, closeStatusSelect, typeSelect, subtypeSelect, itemSelect].forEach(s => s.innerHTML = '<option value="">-- Select Board --</option>');
                return;
            }

            try {
                if (!cachedBoards) {
                    const boardsRes = await fetch('/api/cw/boards');
                    cachedBoards = await boardsRes.json();
                }
                const board = cachedBoards.find(b => b.name === boardName);
                if (!board) return;

                const fetchAndPopulate = async (url, select, hiddenVal) => {
                    select.innerHTML = '<option value="">Loading...</option>';
                    const res = await fetch(url);
                    const items = await res.json();
                    select.innerHTML = '<option value="">-- Use Default --</option>';
                    items.forEach(i => {
                        const opt = document.createElement('option');
                        opt.value = i.name;
                        opt.textContent = i.name;
                        if (i.name === hiddenVal) opt.selected = true;
                        select.appendChild(opt);
                    });
                };

                fetchAndPopulate(`/api/cw/statuses/${board.id}`, statusSelect, statusHidden);
                fetchAndPopulate(`/api/cw/statuses/${board.id}`, closeStatusSelect, closeStatusHidden);
                fetchAndPopulate(`/api/cw/types/${board.id}`, typeSelect, typeHidden);
                fetchAndPopulate(`/api/cw/subtypes/${board.id}`, subtypeSelect, subtypeHidden);
                fetchAndPopulate(`/api/cw/items/${board.id}`, itemSelect, itemHidden);

            } catch (e) { console.error('Error loading board details', e); }
        };

        boardSelect.addEventListener('change', (e) => loadBoardDetails(e.target.value));

        // Initialize Maintenance Windows
        const mwRaw = document.getElementById('maintenance_windows').value;
        if (mwRaw) {
            try {
                const windows = JSON.parse(mwRaw);
                windows.forEach(w => addMaintenanceWindow(w));
            } catch (e) { console.error('Error parsing maintenance windows', e); }
        }

        // Fetch Boards and Priorities, then initialize routing rules after selects are populated
        (async () => {
            try {
                const res = await fetch('/api/cw/boards');
                cachedBoards = await res.json();
                boardSelect.innerHTML = '<option value="">-- Use Default --</option>';
                cachedBoards.forEach(b => {
                    const opt = document.createElement('option');
                    opt.value = b.name;
                    opt.textContent = b.name;
                    if (b.name === boardHidden) opt.selected = true;
                    boardSelect.appendChild(opt);
                });
                if (boardHidden) await loadBoardDetails(boardHidden);
            } catch (e) {
                boardSelect.innerHTML = '<option value="">Error loading boards</option>';
            }

            // Fetch Priorities
            try {
                const res = await fetch('/api/cw/priorities');
                const priorities = await res.json();
                prioritySelect.innerHTML = '<option value="">-- Use Default --</option>';
                priorities.forEach(p => {
                    const opt = document.createElement('option');
                    opt.value = p.name;
                    opt.textContent = p.name;
                    if (p.name === priorityHidden) opt.selected = true;
                    prioritySelect.appendChild(opt);
                });
            } catch (e) {
                prioritySelect.innerHTML = '<option value="">Error loading priorities</option>';
            }

            // Initialize Routing Rules only AFTER boards/priorities are populated
            const rulesRaw = document.getElementById('routing_rules').value;
            if (rulesRaw) {
                try {
                    const rules = JSON.parse(rulesRaw);
                    rules.forEach(r => addRoutingRule(r));
                } catch (e) { console.error('Error parsing routing rules', e); }
            }

            // Initialize Remove Strings
            const removeStringsRaw = document.getElementById('summary_remove_strings').value;
            if (removeStringsRaw) {
                // We split by comma to render the individual boxes but don't trim arbitrarily,
                // because the user might have specifically requested a leading space to be removed.
                const removeStrings = removeStringsRaw.split(',');
                removeStrings.forEach(s => addRemoveString(s));
            }
        })();

        // Debounce helper
        function debounce(func, wait) {
            let timeout;
            return function (...args) {
                clearTimeout(timeout);
                timeout = setTimeout(() => func.apply(this, args), wait);
            };
        }

        async function fetchCompanies(query = '') {
            try {
                const url = query ? `/api/cw/companies?search=${encodeURIComponent(query)}` : '/api/cw/companies';
                const res = await fetch(url);
                const companies = await res.json();
                const datalist = document.getElementById('company_list');
                if (datalist) {
                    datalist.innerHTML = '';
                    companies.forEach(c => {
                        const opt = document.createElement('option');
                        opt.value = c.identifier;
                        opt.textContent = `${c.name} (${c.identifier})`;
                        datalist.appendChild(opt);
                    });
                }
            } catch (e) {
                console.error('Error loading companies', e);
            }
        }

        // Initial load (first 50)
        fetchCompanies();

        // Search-as-you-type listener
        const companyInput = document.getElementById('customer_id_default');
        if (companyInput) {
            companyInput.addEventListener('input', debounce((e) => {
                const val = e.target.value;
                if (val.length >= 2) {
                    fetchCompanies(val);
                } else if (val.length === 0) {
                    fetchCompanies();
                }
            }, 500));
        }
    }

    // Page-specific scripts execute after HTMX has swapped the new body, so
    // registering for the current htmx:load event can be too late. Initialize
    // immediately when the DOM already exists and keep listeners for later loads.
    document.addEventListener('htmx:load', initFormSelects);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initFormSelects, { once: true });
    } else {
        initFormSelects();
    }


