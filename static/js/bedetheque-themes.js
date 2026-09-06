let themeGroups = null;
const themeDetailCache = {};
let themeDetailItems = [];
let currentThemeSlug = null;

// "the data does not really change much so no need to update automatic. put only update
// manual in the enrichir page" - force=true (bouton "Actualiser", voir refreshThemesList)
// contourne à la fois ce cache client ET le cache serveur (?refresh=1 - voir list_themes,
// bedetheque/routes.py, désormais sans expiration automatique).
async function openThemesTab(force = false) {
    if (!force && themeGroups) { renderThemeGroups(); return; }
    const box = document.getElementById('themesGroupsList');
    box.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const data = await (await fetch('/api/bedetheque/themes' + (force ? '?refresh=1' : ''))).json();
        if (!data.success) throw Error(data.error);
        themeGroups = data.groups || [];
        renderThemeGroups();
    } catch (e) {
        box.innerHTML = `<p class="help-text">Erreur : ${escapeHtml(e.message)}</p>`;
    }
}

async function refreshThemesList(button) {
    const original = button ? button.innerHTML : null;
    if (button) { button.disabled = true; button.innerHTML = svgIcon('loader-circle', 'icon-spin'); }
    await openThemesTab(true);
    if (button) { button.disabled = false; button.innerHTML = original; }
}

function renderThemeGroups() {
    const query = window.themeSearchFilter || '';
    document.getElementById('themesGroupsList').innerHTML = `
        <div class="theme-search-row">
            <input type="text" id="theme-search-input" class="series-table-filter-input" placeholder="Rechercher un thème..." value="${escapeHtml(query)}" oninput="filterThemeGroups(this.value)">
        </div>
        ${(themeGroups || []).map(group => `
        <div class="theme-group">
            <h3 class="theme-group-title">${escapeHtml(group.name)}</h3>
            <div class="theme-group-buttons">
                ${group.themes.map(t => `<button class="btn-neutral-sm theme-button" data-theme-name="${escapeHtml(t.name.toLowerCase())}" onclick="openThemeDetail('${escapeForAttribute(t.slug)}', '${escapeForAttribute(t.name)}', '${escapeForAttribute(t.url)}')">${escapeHtml(t.name)}</button>`).join('')}
            </div>
        </div>
    `).join('')}
    `;
    filterThemeGroups(query);
    initClearableSearchInputs(document.getElementById('themesGroupsList'));
}

function filterThemeGroups(query) {
    window.themeSearchFilter = String(query || '');
    const needle = window.themeSearchFilter.trim().toLowerCase();
    document.querySelectorAll('#themesGroupsList .theme-group').forEach(groupEl => {
        let anyVisible = false;
        groupEl.querySelectorAll('.theme-button').forEach(btn => {
            const match = !needle || (btn.dataset.themeName || '').includes(needle);
            btn.style.display = match ? '' : 'none';
            if (match) anyVisible = true;
        });
        groupEl.style.display = anyVisible ? '' : 'none';
    });
}

function closeThemeDetail() {
    document.getElementById('themes-detail').style.display = 'none';
    document.getElementById('themes-groups').style.display = '';
}

async function openThemeDetail(slug, name, url, force = false) {
    currentThemeSlug = slug;
    document.getElementById('themes-groups').style.display = 'none';
    document.getElementById('themes-detail').style.display = '';
    document.getElementById('theme-detail-title').textContent = name;
    document.getElementById('theme-detail-source-link').href = url;
    const box = document.getElementById('themeDetailList');
    if (!force && themeDetailCache[slug]) {
        themeDetailItems = themeDetailCache[slug];
        renderThemeDetail();
        return;
    }
    box.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const fetchUrl = '/api/bedetheque/theme?slug=' + encodeURIComponent(slug) + (force ? '&refresh=1' : '');
        const data = await (await fetch(fetchUrl)).json();
        if (!data.success) throw Error(data.error);
        themeDetailItems = data.items || [];
        themeDetailCache[slug] = themeDetailItems;
        renderThemeDetail();
    } catch (e) {
        box.innerHTML = `<p class="help-text">Erreur : ${escapeHtml(e.message)}</p>`;
    }
}

// Bouton "Actualiser" de la vue détail (voir themes-detail dans templates/bedetheque-enrich.html) -
// currentThemeSlug/le titre/l'URL déjà affichés suffisent à rejouer openThemeDetail avec force=true.
async function refreshThemeDetail(button) {
    if (!currentThemeSlug) return;
    const original = button ? button.innerHTML : null;
    if (button) { button.disabled = true; button.innerHTML = svgIcon('loader-circle', 'icon-spin'); }
    await openThemeDetail(currentThemeSlug, document.getElementById('theme-detail-title').textContent, document.getElementById('theme-detail-source-link').href, true);
    if (button) { button.disabled = false; button.innerHTML = original; }
}

function filterThemeDetail() {
    const ownedFilter = window.themeDetailOwnedFilter || '';
    document.querySelectorAll('#themeDetailList tbody tr').forEach(row => {
        const owned = row.querySelector('.indispensable-owned')?.dataset.owned || '';
        row.hidden = !!ownedFilter && owned !== ownedFilter;
    });
}

function renderThemeDetail() {
    const rows = themeDetailItems;
    if (rows.length === 0) {
        document.getElementById('themeDetailList').innerHTML = '<p class="help-text">Aucune BD trouvée pour ce thème.</p>';
        return;
    }
    const ownedFilter = window.themeDetailOwnedFilter || '';
    document.getElementById('themeDetailList').innerHTML = `
        <table class="series-table series-table-compact series-table-with-covers">
            <!-- Voir le commentaire jumeau dans bdgest-top.js (renderBdgestTop) -
                 table-layout:fixed (media mobile, style.css) a besoin de ce <colgroup>
                 pour que les largeurs de colonnes soient garanties, pas juste indicatives. -->
            <colgroup>
                <col class="col-rank"><col class="col-cover"><col class="col-title">
                <col class="col-publisher"><col class="col-authors"><col class="col-note">
                <col class="col-owned"><col class="col-action">
            </colgroup>
            <thead>
                <tr>
                    <th class="volume-table-select-cell"><input type="checkbox" id="themeDetailSelectAll" aria-label="Sélectionner toutes les BD visibles" onchange="toggleAllThemeDetail(this.checked)"></th>
                    <th></th>
                    <th class="volume-table-sortable" onclick="sortThemeDetail('title')">Série <span class="indispensable-sort-arrow" data-sort-field="title">↕</span></th>
                    <th class="volume-table-sortable" onclick="sortThemeDetail('origin')">Éditeur <span class="indispensable-sort-arrow" data-sort-field="origin">↕</span></th>
                    <th class="volume-table-sortable" onclick="sortThemeDetail('authors')">Auteurs <span class="indispensable-sort-arrow" data-sort-field="authors">↕</span></th>
                    <th class="volume-table-sortable" onclick="sortThemeDetail('note')">Note <span class="indispensable-sort-arrow" data-sort-field="note">↕</span></th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">En bibliothèque</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par présence en bibliothèque" onchange="_syncFilterControlActive(this); window.themeDetailOwnedFilter=this.value; filterThemeDetail()">
                        <option value="">Tous</option>
                        <option value="1"${ownedFilter === '1' ? ' selected' : ''}>Oui</option>
                        <option value="0"${ownedFilter === '0' ? ' selected' : ''}>Non</option>
                    </select></span></div></th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                ${rows.map((x, i) => {
                    const titleCellHtml = (x.already_owned && x.series_id)
                        ? `<a class="indispensable-series-link" href="/series/${x.series_id}">${escapeHtml(x.title)}</a><a class="indispensable-source-link" href="${escapeHtml(x.url)}" target="_blank" rel="noopener" data-tooltip="Voir sur Bédéthèque">↗</a>`
                        : `<a class="indispensable-series-link" href="${escapeHtml(x.url)}" target="_blank" rel="noopener">${escapeHtml(x.title)}</a>`;
                    const actionHtml = x.already_owned
                        ? ''
                        : `<button class="btn-icon-only" onclick="addThemeSeries(${i}, this)" data-tooltip="Ajouter cette série" aria-label="Ajouter cette série">${svgIcon('plus')}</button>`;
                    return `<tr class="series-table-row${x.already_owned ? '' : ' indispensable-row-missing'}">
                        <td class="volume-table-select-cell"><input type="checkbox" class="theme-detail-checkbox" data-index="${i}" aria-label="Sélectionner ${escapeHtml(x.title)}" onchange="_updateThemeDetailBulkActionsBar()"${x.already_owned ? ' disabled' : ''}></td>
                        <td class="theme-cover-cell">${x.cover ? `<img src="${escapeHtml(x.cover)}" alt="" class="theme-cover-thumb" onclick="openCoverModal('${escapeForAttribute(x.cover)}', '${escapeForAttribute(x.title)}')" style="cursor:pointer;">` : ''}</td>
                        <td>${titleCellHtml}<p class="theme-summary">${escapeHtml(x.summary || '')}</p></td>
                        <td>${escapeHtml(x.origin || '-')}</td>
                        <td>${escapeHtml(x.authors || '-')}</td>
                        <td class="theme-note-cell">${escapeHtml((x.note || '').replace('Note : ', ''))}</td>
                        <td class="indispensable-owned" data-owned="${x.already_owned ? '1' : '0'}">${x.already_owned ? svgIcon('check') : svgIcon('x')}</td>
                        <td class="pantheon-action">${actionHtml}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
    `;
    filterThemeDetail();
    initClearableSearchInputs(document.getElementById('themeDetailList'));
}

const THEME_DETAIL_SORT_COLUMNS = { title: 2, origin: 3, authors: 4, note: 5 };
function sortThemeDetail(field) {
    const rows = [...document.querySelectorAll('#themeDetailList tbody tr')];
    const col = THEME_DETAIL_SORT_COLUMNS[field];
    const current = document.querySelector(`#themeDetailList .indispensable-sort-arrow[data-sort-field="${field}"]`)?.textContent;
    const dir = current === '↑' ? -1 : 1;
    rows.sort((a, b) => dir * a.cells[col].textContent.localeCompare(b.cells[col].textContent, 'fr', { numeric: true }));
    const body = document.querySelector('#themeDetailList tbody');
    rows.forEach(r => body.appendChild(r));
    document.querySelectorAll('#themeDetailList .indispensable-sort-arrow').forEach(a => a.textContent = a.dataset.sortField === field ? (dir === 1 ? '↑' : '↓') : '↕');
}

async function addThemeSeries(index, button) {
    const libraryId = indispensableLibraryId;
    const x = themeDetailItems[index];
    if (!libraryId) { notifyIndispensable('Choisis une bibliothèque.', 'error'); return; }
    if (!x || !button) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    notifyIndispensable(`${x.title} : ajout en cours…`, 'info');
    try {
        const r = await fetch('/api/bedetheque/add-series', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: x.url, library_id: libraryId }) });
        const d = await r.json();
        if (!r.ok || !d.success) throw Error(d.error || 'Échec');
        x.already_owned = true;
        button.innerHTML = svgIcon('check');
        button.setAttribute('data-tooltip', d.already_exists ? 'Déjà présente' : 'Ajoutée');
        button.classList.add('indispensable-added');
        notifyIndispensable(d.already_exists ? `La série « ${x.title} » est déjà présente dans la bibliothèque.` : `La série « ${x.title} » a été ajoutée à la bibliothèque.`, 'success');
    } catch (e) {
        button.disabled = false;
        button.classList.remove('indispensable-added');
        button.innerHTML = original;
        notifyIndispensable(`${x.title} : ${e.message}`, 'error');
    }
}

function toggleAllThemeDetail(checked) {
    document.querySelectorAll('.theme-detail-checkbox:not(:disabled)').forEach(cb => { cb.checked = checked; });
    _updateThemeDetailBulkActionsBar();
}

function _updateThemeDetailBulkActionsBar() {
    const bar = document.getElementById('theme-detail-bulk-actions-bar');
    if (!bar) return;
    const count = document.querySelectorAll('.theme-detail-checkbox:checked:not(:disabled)').length;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="volume-bulk-actions-count">${count} ${pluralize(count, 'série')} ${pluralize(count, 'sélectionnée')}</span>
        <button class="btn-neutral-sm" onclick="addSelectedThemeDetail()">${svgIcon('plus')} Ajouter la sélection</button>
        <button class="btn-neutral-sm" onclick="toggleAllThemeDetail(false); _updateThemeDetailBulkActionsBar()">Annuler la sélection</button>
    `;
}

async function addSelectedThemeDetail() {
    const selected = [...document.querySelectorAll('.theme-detail-checkbox:checked:not(:disabled)')];
    if (!selected.length) { notifyIndispensable('Sélectionne au moins une série.', 'info'); return; }
    for (const cb of selected) {
        const button = cb.closest('tr').querySelector('.pantheon-action button');
        if (button) await addThemeSeries(Number(cb.dataset.index), button);
    }
    _updateThemeDetailBulkActionsBar();
}
