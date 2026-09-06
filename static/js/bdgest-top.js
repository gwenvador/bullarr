// ===== Onglet "Top 100 BDGest" (bedetheque-enrich.html) =====
// "de meme pour les top 100 par annee: https://www.bdgest.com/top/annuel?annee=2026&
// Origine=1" - même gabarit que Panthéon/Thèmes (bedetheque-*.js): scrape+cache 6h côté
// serveur (blueprints/bdgest/routes.py, site DIFFÉRENT de bedetheque.com), tableau avec
// détection "déjà possédé" et action "Ajouter" réutilisant indispensableLibraryId/
// notifyIndispensable (bedetheque-indispensables.js, déjà chargé sur cette page).
let bdgestOrigine = 'general';
let bdgestItems = [];
const bdgestCache = {};

function _bdgestYearBounds() {
    const now = new Date().getFullYear();
    return { min: 2000, max: now + 1 };
}

function populateBdgestYearOptions(selectedYear) {
    const select = document.getElementById('bdgest-year-input');
    const { min, max } = _bdgestYearBounds();
    const years = [];
    for (let y = max; y >= min; y--) years.push(y);
    select.innerHTML = years.map(y => `<option value="${y}"${y === selectedYear ? ' selected' : ''}>${y}</option>`).join('');
}

function openBdgestTopTab() {
    const select = document.getElementById('bdgest-year-input');
    if (!select.options.length) populateBdgestYearOptions(new Date().getFullYear());
    loadBdgestTop();
}

function changeBdgestYear(delta) {
    const select = document.getElementById('bdgest-year-input');
    const { min, max } = _bdgestYearBounds();
    const newYear = Math.min(max, Math.max(min, (parseInt(select.value, 10) || new Date().getFullYear()) + delta));
    select.value = String(newYear);
    loadBdgestTop();
}

function selectBdgestOrigine(origine, button) {
    bdgestOrigine = origine;
    document.querySelectorAll('#bdgest-top .indispensable-category-tabs .tab').forEach(b => b.classList.toggle('active', b === button));
    loadBdgestTop();
}

// "the data does not really change much so no need to update automatic. put only update
// manual in the enrichir page" - force=true (bouton "Actualiser", voir refreshBdgestTop)
// contourne à la fois ce cache client ET le cache serveur (?refresh=1 - voir top_annuel,
// bdgest/routes.py, désormais sans expiration automatique).
async function loadBdgestTop(force = false) {
    const year = parseInt(document.getElementById('bdgest-year-input').value, 10) || new Date().getFullYear();
    const key = `${year}-${bdgestOrigine}`;
    const box = document.getElementById('bdgestTopList');
    if (!force && bdgestCache[key]) {
        bdgestItems = bdgestCache[key];
        renderBdgestTop();
        return;
    }
    box.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const url = `/api/bdgest/top-annuel?annee=${year}&origine=${encodeURIComponent(bdgestOrigine)}` + (force ? '&refresh=1' : '');
        const data = await (await fetch(url)).json();
        if (!data.success) throw Error(data.error);
        bdgestItems = data.items || [];
        bdgestCache[key] = bdgestItems;
        renderBdgestTop();
    } catch (e) {
        box.innerHTML = `<p class="help-text">Erreur : ${escapeHtml(e.message)}</p>`;
    }
}

async function refreshBdgestTop(button) {
    const original = button ? button.innerHTML : null;
    if (button) { button.disabled = true; button.innerHTML = svgIcon('loader-circle', 'icon-spin'); }
    await loadBdgestTop(true);
    if (button) { button.disabled = false; button.innerHTML = original; }
}

function filterBdgestTop() {
    const ownedFilter = window.bdgestTopOwnedFilter || '';
    document.querySelectorAll('#bdgestTopList tbody tr').forEach(row => {
        const owned = row.querySelector('.indispensable-owned')?.dataset.owned || '';
        row.hidden = !!ownedFilter && owned !== ownedFilter;
    });
}

function renderBdgestTop() {
    const rows = bdgestItems;
    const box = document.getElementById('bdgestTopList');
    if (rows.length === 0) {
        box.innerHTML = '<p class="help-text">Aucun classement trouvé pour cette année/origine.</p>';
        return;
    }
    const ownedFilter = window.bdgestTopOwnedFilter || '';
    box.innerHTML = `
        <table class="series-table series-table-compact series-table-with-covers">
            <!-- "still too small" (mobile) - table-layout:auto par défaut ne traite width
                 sur .theme-cover-cell/.theme-cover-thumb que comme un INDICE, écrasable par
                 le contenu des autres colonnes (voir style.css: table-layout:fixed n'est
                 posé qu'en media mobile). Un <colgroup> est nécessaire pour que ce mode
                 fixe ait des largeurs à respecter - voir .col-cover et les classes soeurs
                 (style.css), inertes tant que table-layout reste "auto" (desktop). -->
            <colgroup>
                <col class="col-rank"><col class="col-cover"><col class="col-title">
                <col class="col-publisher"><col class="col-date"><col class="col-votes">
                <col class="col-owned"><col class="col-action">
            </colgroup>
            <thead>
                <tr>
                    <th class="volume-table-sortable" onclick="sortBdgestTop('rank')"># <span class="indispensable-sort-arrow" data-sort-field="rank">↕</span></th>
                    <th></th>
                    <th class="volume-table-sortable" onclick="sortBdgestTop('title')">Série <span class="indispensable-sort-arrow" data-sort-field="title">↕</span></th>
                    <th class="volume-table-sortable" onclick="sortBdgestTop('publisher')">Éditeur <span class="indispensable-sort-arrow" data-sort-field="publisher">↕</span></th>
                    <th class="volume-table-sortable" onclick="sortBdgestTop('release_date')">Parution <span class="indispensable-sort-arrow" data-sort-field="release_date">↕</span></th>
                    <th class="volume-table-sortable" onclick="sortBdgestTop('votes')">Votes <span class="indispensable-sort-arrow" data-sort-field="votes">↕</span></th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">En bibliothèque</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par présence en bibliothèque" onchange="_syncFilterControlActive(this); window.bdgestTopOwnedFilter=this.value; filterBdgestTop()">
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
                        : `<button class="btn-icon-only" onclick="addBdgestSeries(${i}, this)" data-tooltip="Ajouter cette série" aria-label="Ajouter cette série">${svgIcon('plus')}</button>`;
                    return `<tr class="series-table-row${x.already_owned ? '' : ' indispensable-row-missing'}">
                        <td class="indispensable-rank">${x.rank}</td>
                        <td class="theme-cover-cell">${x.cover ? `<img src="${escapeHtml(x.cover)}" alt="" class="theme-cover-thumb" onclick="openCoverModal('${escapeForAttribute(x.cover)}', '${escapeForAttribute(x.title)}')" style="cursor:pointer;">` : ''}</td>
                        <td>${titleCellHtml}${x.volume_label ? `<div class="pantheon-author-dates">${escapeHtml(x.volume_label)}</div>` : ''}<p class="theme-summary">${escapeHtml(x.summary || '')}</p></td>
                        <td>${escapeHtml(x.publisher || '-')}</td>
                        <td class="theme-note-cell">${escapeHtml(x.release_date || '-')}</td>
                        <td class="theme-note-cell">${escapeHtml(x.votes || '-')}</td>
                        <td class="indispensable-owned" data-owned="${x.already_owned ? '1' : '0'}">${x.already_owned ? svgIcon('check') : svgIcon('x')}</td>
                        <td class="pantheon-action">${actionHtml}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
    `;
    filterBdgestTop();
    initClearableSearchInputs(document.getElementById('bdgestTopList'));
}

const BDGEST_TOP_SORT_COLUMNS = { rank: 0, title: 2, publisher: 3, release_date: 4, votes: 5 };
function sortBdgestTop(field) {
    const rows = [...document.querySelectorAll('#bdgestTopList tbody tr')];
    const col = BDGEST_TOP_SORT_COLUMNS[field];
    const current = document.querySelector(`#bdgestTopList .indispensable-sort-arrow[data-sort-field="${field}"]`)?.textContent;
    const dir = current === '↑' ? -1 : 1;
    rows.sort((a, b) => dir * a.cells[col].textContent.localeCompare(b.cells[col].textContent, 'fr', { numeric: true }));
    const body = document.querySelector('#bdgestTopList tbody');
    rows.forEach(r => body.appendChild(r));
    document.querySelectorAll('#bdgestTopList .indispensable-sort-arrow').forEach(a => a.textContent = a.dataset.sortField === field ? (dir === 1 ? '↑' : '↓') : '↕');
}

async function addBdgestSeries(index, button) {
    const libraryId = indispensableLibraryId;
    const x = bdgestItems[index];
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
