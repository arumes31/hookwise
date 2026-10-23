(() => {
    const targets = document.getElementById('forwarding-targets');
    const add = document.getElementById('add-forwarding-target');
    const score = document.getElementById('score_enabled');
    if (!targets || !add || !score) return;

    const updateTargets = () => {
        const rows = targets.querySelectorAll('[data-target-row]');
        rows.forEach((row, index) => {
            row.querySelector('[name="target_url"]').setAttribute('aria-label', `Ziel-URL ${index + 1}`);
            const remove = row.querySelector('[data-remove-target]');
            remove.disabled = rows.length === 1;
            remove.setAttribute('aria-label', `Ziel-URL ${index + 1} entfernen`);
        });
        add.disabled = rows.length >= 10;
    };
    add.addEventListener('click', () => {
        if (targets.children.length >= 10) return;
        const row = targets.firstElementChild.cloneNode(true);
        row.querySelector('[name="target_slot"]').value = 'new';
        const input = row.querySelector('[name="target_url"]');
        input.value = '';
        input.placeholder = 'https://alarm.kunde.at/webhook';
        input.required = true;
        targets.append(row);
        updateTargets();
        input.focus();
    });
    targets.addEventListener('click', (event) => {
        const remove = event.target.closest('[data-remove-target]');
        if (!remove || targets.children.length <= 1) return;
        remove.closest('[data-target-row]').remove();
        updateTargets();
        add.focus();
    });
    score.addEventListener('change', () => {
        for (const [fieldId, inputId] of [['score-path-field', 'score_path'], ['score-minimum-field', 'minimum_score']]) {
            document.getElementById(fieldId).hidden = !score.checked;
            const input = document.getElementById(inputId);
            input.disabled = !score.checked;
            input.required = score.checked;
        }
        if (score.checked && !document.getElementById('score_path').value) {
            document.getElementById('score_path').value = '$.score';
        }
    });
    updateTargets();
})();
