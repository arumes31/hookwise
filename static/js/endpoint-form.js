function updatePreview() {
        const prefix = document.getElementById('ticket_prefix').value;
        const preview = document.getElementById('summary-preview');
        preview.textContent = (prefix ? prefix + ' ' : '') + 'Source Name';
    }

    function initFormSelects() {
        const form = document.getElementById('endpoint-form');
        if (
            !form
            || form.dataset.autosaveReady !== 'true'
            || ['loading', 'true'].includes(form.dataset.initialized)
        ) return;
        form.dataset.initialized = 'loading';

        const boardSelect = document.getElementById('board');
        const statusSelect = document.getElementById('status');
        const closeStatusSelect = document.getElementById('close_status');
        const typeSelect = document.getElementById('ticket_type');
        const subtypeSelect = document.getElementById('subtype');
        const itemSelect = document.getElementById('item');
        const prioritySelect = document.getElementById('priority');
        const companyList = document.getElementById('company_list');
        const companyInput = document.getElementById('customer_id_default');
        const loadError = document.getElementById('connectwise-load-error');
        const retryButton = document.getElementById('connectwise-retry');
        const addRoutingRuleButton = document.getElementById('add-routing-rule');
        const saveControls = form.querySelectorAll
            ? Array.from(form.querySelectorAll('#hw-submit-btn, #draft-btn, #hw-save-another-btn'))
            : [];
        const dependentSelects = [statusSelect, closeStatusSelect, typeSelect, subtypeSelect, itemSelect];
        const detailSources = ['statuses', 'types', 'subtypes', 'items'];

        const boardHidden = document.getElementById('board_hidden')?.value || '';
        const statusHidden = document.getElementById('status_hidden')?.value || '';
        const closeStatusHidden = document.getElementById('close_status_hidden')?.value || '';
        const typeHidden = document.getElementById('type_hidden')?.value || '';
        const subtypeHidden = document.getElementById('subtype_hidden')?.value || '';
        const itemHidden = document.getElementById('item_hidden')?.value || '';
        const priorityHidden = document.getElementById('priority_hidden')?.value || '';

        const state = form.connectWiseLoaderState || {
            boards: null,
            boardGeneration: 0,
            boardController: null,
            boardLoadPromise: null,
            companyGeneration: 0,
            companyController: null,
            initializationGeneration: 0,
            initializationController: null,
            failures: new Set(),
            boardDetailsReady: false,
            prioritiesReady: false,
            boardChangeSaveBlocked: false,
        };
        if (!(state.failures instanceof Set)) state.failures = new Set();
        form.connectWiseLoaderState = state;

        const setSingleOption = (select, text) => {
            select.innerHTML = `<option value="">${text}</option>`;
            delete select.dataset.loading;
        };

        const appendRetainedOption = (select, selectedValue, suffix) => {
            if (!selectedValue) return;
            const option = document.createElement('option');
            option.value = selectedValue;
            option.textContent = `${selectedValue} (${suffix})`;
            option.selected = true;
            select.appendChild(option);
        };

        const setLookupFailure = (select, text, selectedValue = '') => {
            setSingleOption(select, text);
            appendRetainedOption(select, selectedValue, 'current value retained; lookup unavailable');
        };

        const hasLoadedOptions = select => Array.from(select.options).some(option => (
            option.value
            && !option.textContent.includes('retained while loading')
            && !option.textContent.includes('lookup unavailable')
            && !option.textContent.includes('saved value unavailable')
        ));

        const setLookupLoading = (select, selectedValue = '', preserveOptions = true) => {
            if (preserveOptions && hasLoadedOptions(select)) {
                select.dataset.loading = 'true';
                return;
            }
            setSingleOption(select, 'Loading...');
            appendRetainedOption(select, selectedValue, 'current value retained while loading');
            select.dataset.loading = 'true';
        };

        const updateLoadError = () => {
            if (loadError) loadError.classList.toggle('d-none', state.failures.size === 0);
        };
        const markFailure = (source, failed) => {
            if (failed) state.failures.add(source);
            else state.failures.delete(source);
            updateLoadError();
        };
        const clearDetailFailures = () => {
            detailSources.forEach(source => state.failures.delete(source));
            updateLoadError();
        };
        updateLoadError();

        const populateSelect = (select, items, selectedValue = '') => {
            setSingleOption(select, '-- Use Default --');
            let selectionFound = !selectedValue;
            items.forEach(item => {
                const option = document.createElement('option');
                option.value = item.name;
                option.textContent = item.name;
                if (item.name === selectedValue) {
                    option.selected = true;
                    selectionFound = true;
                }
                select.appendChild(option);
            });
            if (!selectionFound) {
                appendRetainedOption(select, selectedValue, 'saved value unavailable');
            }
            return selectionFound;
        };

        const snapshotSelection = (select, fallback = '') => {
            if (select.dataset.autosaveRestored === 'true') return select.value;
            const selectedOption = Array.from(select.options).find(option => option.selected);
            const placeholder = !selectedOption
                || selectedOption.textContent === 'Loading...'
                || selectedOption.textContent.startsWith('Unable to load')
                || selectedOption.textContent.startsWith('-- Select Board');
            return placeholder ? fallback : selectedOption.value;
        };

        const isNamedLookupItem = item => (
            item !== null
            && typeof item === 'object'
            && typeof item.name === 'string'
            && item.name.trim().length > 0
        );
        const isBoard = item => isNamedLookupItem(item) && Number.isInteger(item.id) && item.id > 0;
        const isCompany = item => (
            item !== null
            && typeof item === 'object'
            && typeof item.identifier === 'string'
            && item.identifier.trim().length > 0
            && typeof item.name === 'string'
        );

        const fetchArray = async (url, options = {}, itemValidator = isNamedLookupItem) => {
            const response = await fetch(url, options);
            if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
            const data = await response.json();
            if (!Array.isArray(data)) throw new TypeError(`${url} did not return a JSON array`);
            if (!data.every(itemValidator)) throw new TypeError(`${url} returned malformed lookup data`);
            return data;
        };

        const logLookupFailure = (label, error) => {
            console.error(`Error loading ${label}`, error);
        };

        const setBoardChangeSaveBlocked = blocked => {
            state.boardChangeSaveBlocked = blocked;
            saveControls.forEach(control => {
                if (blocked && !control.disabled) {
                    control.disabled = true;
                    control.dataset.connectWiseBoardBlocked = 'true';
                } else if (!blocked && control.dataset.connectWiseBoardBlocked === 'true') {
                    control.disabled = false;
                    delete control.dataset.connectWiseBoardBlocked;
                }
            });
        };

        const hydrateRoutingRules = () => {
            if (!state.boardDetailsReady || !state.prioritiesReady) return false;
            if (form.dataset.routingRulesInitialized) {
                if (addRoutingRuleButton) addRoutingRuleButton.disabled = false;
                return true;
            }

            const rulesRaw = document.getElementById('routing_rules').value;
            if (rulesRaw) {
                try {
                    JSON.parse(rulesRaw).forEach(rule => addRoutingRule(rule));
                } catch (error) {
                    console.error('Error parsing routing rules', error);
                }
            }
            form.dataset.routingRulesInitialized = 'true';
            if (addRoutingRuleButton) addRoutingRuleButton.disabled = false;
            return true;
        };
        state.hydrateRoutingRules = hydrateRoutingRules;
        if (addRoutingRuleButton && !form.dataset.routingRulesInitialized) {
            addRoutingRuleButton.disabled = true;
        }

        const loadBoardDetails = async (
            boardName,
            selectedValues = ['', '', '', '', ''],
            preserveOptions = true,
        ) => {
            const generation = ++state.boardGeneration;
            if (state.boardController) state.boardController.abort();
            const controller = new AbortController();
            state.boardController = controller;
            const isCurrent = () => (
                !controller.signal.aborted
                && generation === state.boardGeneration
                && form.isConnected
            );
            if (addRoutingRuleButton) addRoutingRuleButton.disabled = true;

            if (!boardName) {
                dependentSelects.forEach(select => setSingleOption(select, '-- Select Board --'));
                markFailure('boards', false);
                clearDetailFailures();
                state.boardDetailsReady = true;
                state.hydrateRoutingRules();
                setBoardChangeSaveBlocked(false);
                return true;
            }

            state.boardDetailsReady = false;

            dependentSelects.forEach((select, index) => {
                setLookupLoading(select, selectedValues[index] || '', preserveOptions);
            });

            try {
                if (!state.boards) {
                    state.boards = await fetchArray('/api/cw/boards', { signal: controller.signal }, isBoard);
                }
                if (!isCurrent()) return null;
                const board = state.boards.find(candidate => candidate.name === boardName);
                if (!board) throw new Error(`Board not found: ${boardName}`);
                markFailure('boards', false);

                const lookups = [
                    {
                        source: 'statuses',
                        label: 'statuses',
                        url: `/api/cw/statuses/${board.id}`,
                        selects: [statusSelect, closeStatusSelect],
                        values: [selectedValues[0], selectedValues[1]],
                    },
                    {
                        source: 'types',
                        label: 'types',
                        url: `/api/cw/types/${board.id}`,
                        selects: [typeSelect],
                        values: [selectedValues[2]],
                    },
                    {
                        source: 'subtypes',
                        label: 'subtypes',
                        url: `/api/cw/subtypes/${board.id}`,
                        selects: [subtypeSelect],
                        values: [selectedValues[3]],
                    },
                    {
                        source: 'items',
                        label: 'items',
                        url: `/api/cw/items/${board.id}`,
                        selects: [itemSelect],
                        values: [selectedValues[4]],
                    },
                ];

                const loadLookup = async lookup => {
                    try {
                        const items = await fetchArray(lookup.url, { signal: controller.signal });
                        if (!isCurrent()) return null;
                        const selections = lookup.selects.map((select, index) => (
                            snapshotSelection(select, lookup.values[index] || '')
                        ));
                        const selectionFound = lookup.selects
                            .map((select, index) => populateSelect(select, items, selections[index]))
                            .every(Boolean);
                        markFailure(lookup.source, !selectionFound);
                        return { loaded: true, selectionFound };
                    } catch (error) {
                        if (!isCurrent()) return null;
                        lookup.selects.forEach((select, index) => {
                            const selection = snapshotSelection(select, lookup.values[index] || '');
                            setLookupFailure(select, `Unable to load ${lookup.label}`, selection);
                        });
                        logLookupFailure(lookup.label, error);
                        markFailure(lookup.source, true);
                        return { loaded: false, selectionFound: false };
                    }
                };

                const results = await Promise.all(lookups.map(loadLookup));
                if (!isCurrent()) return null;
                state.boardDetailsReady = results.every(result => result?.loaded === true);
                state.hydrateRoutingRules();
                const selectionSucceeded = results.every(result => result?.selectionFound === true);
                if (selectionSucceeded) setBoardChangeSaveBlocked(false);
                return selectionSucceeded;
            } catch (error) {
                if (!isCurrent()) return null;
                state.boardDetailsReady = false;
                dependentSelects.forEach((select, index) => {
                    const selection = snapshotSelection(select, selectedValues[index] || '');
                    setLookupFailure(select, 'Unable to load board details', selection);
                });
                detailSources.forEach(source => state.failures.add(source));
                updateLoadError();
                logLookupFailure('board details', error);
                return false;
            }
        };
        state.loadBoardDetails = loadBoardDetails;

        const fetchCompanies = async (query = '') => {
            const generation = ++state.companyGeneration;
            if (state.companyController) state.companyController.abort();
            const controller = new AbortController();
            state.companyController = controller;

            try {
                const url = query ? `/api/cw/companies?search=${encodeURIComponent(query)}` : '/api/cw/companies';
                const companies = await fetchArray(url, { signal: controller.signal }, isCompany);
                if (controller.signal.aborted || generation !== state.companyGeneration || !form.isConnected) return null;
                if (companyList) {
                    companyList.innerHTML = '';
                    companies.forEach(company => {
                        const option = document.createElement('option');
                        option.value = company.identifier;
                        option.textContent = `${company.name} (${company.identifier})`;
                        companyList.appendChild(option);
                    });
                }
                markFailure('companies', false);
                return true;
            } catch (error) {
                if (controller.signal.aborted || generation !== state.companyGeneration || !form.isConnected) return null;
                if (companyList) {
                    companyList.innerHTML = '';
                    const option = document.createElement('option');
                    option.value = 'Unable to load companies';
                    companyList.appendChild(option);
                }
                logLookupFailure('companies', error);
                markFailure('companies', true);
                return false;
            }
        };
        state.fetchCompanies = fetchCompanies;
        state.cancelCompanyFetch = () => {
            state.companyGeneration += 1;
            if (state.companyController) state.companyController.abort();
            state.companyController = null;
        };

        function debounce(func, wait) {
            let timeout;
            return function (...args) {
                clearTimeout(timeout);
                timeout = setTimeout(() => func.apply(this, args), wait);
            };
        }

        if (!form.dataset.connectWiseEventsBound) {
            form.addEventListener('input', () => {
                clearTimeout(window.saveStateTimeout);
                window.saveStateTimeout = setTimeout(saveState, 500);
            });
            saveState();

            document.getElementById('ticket_prefix').addEventListener('input', updatePreview);
            updatePreview();
            toggleBearerMgmt();

            boardSelect.addEventListener('change', event => {
                setBoardChangeSaveBlocked(true);
                const promise = state.loadBoardDetails(
                    event.target.value,
                    ['', '', '', '', ''],
                    false,
                );
                state.boardLoadPromise = promise;
                promise.then(result => {
                    if (result === false) delete form.dataset.initialized;
                    else if (result === true && state.failures.size === 0) form.dataset.initialized = 'true';
                });
            });
            if (retryButton) {
                retryButton.addEventListener('click', () => {
                    delete form.dataset.initialized;
                    initFormSelects();
                });
            }
            if (companyInput) {
                const loadCompaniesForInput = debounce(value => {
                    if (value.length >= 2) state.fetchCompanies(value);
                    else if (value.length === 0) state.fetchCompanies();
                }, 500);
                companyInput.addEventListener('input', event => {
                    const value = event.target.value;
                    state.cancelCompanyFetch();
                    if (value.length < 2 && companyList) companyList.innerHTML = '';
                    loadCompaniesForInput(value);
                });
            }

            const maintenanceWindowsRaw = document.getElementById('maintenance_windows').value;
            if (maintenanceWindowsRaw) {
                try {
                    JSON.parse(maintenanceWindowsRaw).forEach(windowData => addMaintenanceWindow(windowData));
                } catch (error) {
                    console.error('Error parsing maintenance windows', error);
                }
            }

            const removeStringsRaw = document.getElementById('summary_remove_strings').value;
            if (removeStringsRaw) {
                removeStringsRaw.split(',').forEach(value => addRemoveString(value));
            }
            form.dataset.connectWiseEventsBound = 'true';
        }

        if (state.initializationController) state.initializationController.abort();
        const initializationController = new AbortController();
        state.initializationController = initializationController;
        const initializationGeneration = ++state.initializationGeneration;
        const isCurrentInitialization = () => (
            !initializationController.signal.aborted
            && initializationGeneration === state.initializationGeneration
            && form.isConnected
        );
        state.boardDetailsReady = false;
        state.prioritiesReady = false;

        (async () => {
            markFailure('initialization', false);
            const selectedBoard = snapshotSelection(boardSelect, boardHidden);
            const selectedPriority = snapshotSelection(prioritySelect, priorityHidden);
            const savedDependentValues = [statusHidden, closeStatusHidden, typeHidden, subtypeHidden, itemHidden];
            const dependentFallbacks = selectedBoard === boardHidden
                ? savedDependentValues
                : ['', '', '', '', ''];
            const selectedDependentValues = dependentSelects.map(
                (select, index) => snapshotSelection(select, dependentFallbacks[index]),
            );
            const boardGenerationAtStart = state.boardGeneration;

            setLookupLoading(boardSelect, selectedBoard);
            setLookupLoading(prioritySelect, selectedPriority);
            if (selectedBoard) {
                dependentSelects.forEach((select, index) => {
                    setLookupLoading(select, selectedDependentValues[index]);
                });
            } else {
                dependentSelects.forEach(select => setSingleOption(select, '-- Select Board --'));
            }

            const loadBoardsAndDetails = async () => {
                try {
                    const boards = await fetchArray(
                        '/api/cw/boards',
                        { signal: initializationController.signal },
                        isBoard,
                    );
                    if (!isCurrentInitialization()) return null;
                    state.boards = boards;
                    const currentBoard = snapshotSelection(boardSelect, selectedBoard);
                    const selectedBoardIsAvailable = populateSelect(boardSelect, boards, currentBoard);
                    markFailure('boards', !selectedBoardIsAvailable);
                    if (!selectedBoardIsAvailable) {
                        state.boardDetailsReady = false;
                        clearDetailFailures();
                        dependentSelects.forEach((select, index) => {
                            const selection = snapshotSelection(select, selectedDependentValues[index]);
                            setLookupFailure(select, 'Unable to load board details', selection);
                        });
                        return false;
                    }

                    if (state.boardGeneration !== boardGenerationAtStart) {
                        return state.boardLoadPromise ? await state.boardLoadPromise !== false : true;
                    }

                    const currentDependentValues = dependentSelects.map(
                        (select, index) => snapshotSelection(select, selectedDependentValues[index]),
                    );
                    const promise = state.loadBoardDetails(currentBoard, currentDependentValues);
                    state.boardLoadPromise = promise;
                    const result = await promise;
                    if (state.boardLoadPromise !== promise && state.boardLoadPromise) {
                        return await state.boardLoadPromise !== false;
                    }
                    return result !== false;
                } catch (error) {
                    if (!isCurrentInitialization()) return null;
                    state.boards = null;
                    state.boardDetailsReady = false;
                    const currentBoard = snapshotSelection(boardSelect, selectedBoard);
                    setLookupFailure(boardSelect, 'Unable to load boards', currentBoard);
                    clearDetailFailures();
                    dependentSelects.forEach((select, index) => {
                        const selection = snapshotSelection(select, selectedDependentValues[index]);
                        setLookupFailure(select, 'Unable to load board details', selection);
                    });
                    logLookupFailure('boards', error);
                    markFailure('boards', true);
                    return false;
                }
            };

            const loadPriorities = async () => {
                try {
                    const priorities = await fetchArray(
                        '/api/cw/priorities',
                        { signal: initializationController.signal },
                    );
                    if (!isCurrentInitialization()) return null;
                    const currentPriority = snapshotSelection(prioritySelect, selectedPriority);
                    const selectionFound = populateSelect(prioritySelect, priorities, currentPriority);
                    state.prioritiesReady = true;
                    markFailure('priorities', !selectionFound);
                    state.hydrateRoutingRules();
                    return selectionFound;
                } catch (error) {
                    if (!isCurrentInitialization()) return null;
                    state.prioritiesReady = false;
                    const currentPriority = snapshotSelection(prioritySelect, selectedPriority);
                    setLookupFailure(prioritySelect, 'Unable to load priorities', currentPriority);
                    logLookupFailure('priorities', error);
                    markFailure('priorities', true);
                    return false;
                }
            };

            const [boardsLoaded, prioritiesLoaded, companiesLoaded] = await Promise.all([
                loadBoardsAndDetails(),
                loadPriorities(),
                state.fetchCompanies(),
            ]);
            if (!isCurrentInitialization()) return;

            const initializationSucceeded = (
                boardsLoaded !== false
                && prioritiesLoaded !== false
                && companiesLoaded !== false
            );
            if (initializationSucceeded) form.dataset.initialized = 'true';
            else delete form.dataset.initialized;
            updateLoadError();
        })().catch(error => {
            if (!isCurrentInitialization()) return;
            [boardSelect, prioritySelect, ...dependentSelects].forEach(select => {
                if (select.options.length === 1 && select.options[0].textContent === 'Loading...') {
                    setLookupFailure(select, 'Unable to load ConnectWise data');
                }
            });
            delete form.dataset.initialized;
            markFailure('initialization', true);
            console.error('Error initializing ConnectWise fields', error);
        });
    }

    // Autosave restoration runs first so draft ConnectWise selections can be used
    // as lookup inputs. The event bubbles from the exact form that was restored.
    document.addEventListener('hookwise:autosave-ready', event => {
        if (event.target === document.getElementById('endpoint-form')) initFormSelects();
    });
    document.addEventListener('htmx:load', initFormSelects);
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initFormSelects, { once: true });
    } else {
        initFormSelects();
    }


