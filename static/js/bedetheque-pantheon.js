let pantheonItems = [];
let pantheonCategory = 'all';
const PANTHEON_CATEGORIES = ['all', 'franco-belge', 'comics', 'manga', 'fondateurs'];

// "dans enrichir. est-ce que les tableaux sont en cache? ... ca recharge tout à chaque
// fois" - même cache-par-catégorie qu'Indispensables (bedetheque-indispensables.js)/
// bdgestCache (bdgest-top.js): évite un aller-retour réseau + reset visible du tri/filtre
// à chaque retour sur un onglet/une catégorie déjà visitée dans la session.
const pantheonCache = {};
// "the data does not really change much so no need to update automatic. put only update
// manual in the enrichir page" - force=true (bouton "Actualiser", voir refreshPantheon)
// contourne à la fois ce cache client ET le cache serveur (?refresh=1 - voir
// pantheon_authors, bedetheque/routes.py, désormais sans expiration automatique).
async function loadPantheon(category = pantheonCategory, force = false) {
    pantheonCategory = category;
    if (!force && pantheonCache[category]) { pantheonItems = pantheonCache[category]; renderPantheon(); return; }
    const box = document.getElementById('pantheonList');
    box.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const url = '/api/bedetheque/pantheon?category=' + encodeURIComponent(pantheonCategory) + (force ? '&refresh=1' : '');
        const data = await (await fetch(url)).json();
        if (!data.success) throw Error(data.error);
        pantheonItems = data.items || [];
        pantheonCache[category] = pantheonItems;
        renderPantheon();
    } catch (e) {
        box.innerHTML = `<p class="help-text">Erreur : ${escapeHtml(e.message)}</p>`;
    }
}

async function refreshPantheon(button) {
    const original = button ? button.innerHTML : null;
    if (button) { button.disabled = true; button.innerHTML = svgIcon('loader-circle', 'icon-spin'); }
    await loadPantheon(pantheonCategory, true);
    if (button) { button.disabled = false; button.innerHTML = original; }
}

function _setPantheonCategoryCount(category, count) {
    const el = document.querySelector(`#pantheon-cat-tab-${category} .indispensable-cat-count`);
    if (el) el.textContent = ` (${count})`;
}

function _updatePantheonTabCount() {
    _setPantheonCategoryCount(pantheonCategory, pantheonItems.length);
    _loadOtherPantheonCategoryCounts();
}

let _pantheonCategoryCountsLoaded = false;
async function _loadOtherPantheonCategoryCounts() {
    if (_pantheonCategoryCountsLoaded) return;
    _pantheonCategoryCountsLoaded = true;
    for (const category of PANTHEON_CATEGORIES) {
        if (category === pantheonCategory) continue;
        try {
            const data = await (await fetch('/api/bedetheque/pantheon?category=' + encodeURIComponent(category))).json();
            if (!data.success) continue;
            _setPantheonCategoryCount(category, (data.items || []).length);
        } catch (e) { /* best-effort - une catégorie qui échoue garde juste un bouton sans compte */ }
    }
}

function renderPantheon() {
    const query = (window.pantheonHeaderFilter || '').toLowerCase().trim();
    const countryFilter = window.pantheonCountryFilter || '';
    const ownedFilter = window.pantheonOwnedFilter || '';
    const rows = pantheonItems;
    _updatePantheonTabCount();
    const countryOptions = [...new Set(rows.map(x => (x.country || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'fr'));
    document.getElementById('pantheonList').innerHTML = `
        <table class="series-table series-table-compact">
            <thead>
                <tr>
                    <th class="volume-table-sortable" onclick="sortPantheon('rank')"><div class="th-filterable-row"><span class="th-filterable-label"># <span class="indispensable-sort-arrow" data-sort-field="rank">↕</span></span></div></th>
                    <th class="volume-table-sortable" onclick="sortPantheon('name')"><div class="th-filterable-row"><span class="th-filterable-label">Auteur <span class="indispensable-sort-arrow" data-sort-field="name">↕</span></span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(query)}" placeholder="Filtrer..." aria-label="Filtrer les auteurs" oninput="_syncFilterControlActive(this); filterPantheonTable(this.value)"></span></div></th>
                    <th>Métier(s)</th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">Pays</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" id="pantheon-country-filter" aria-label="Filtrer par pays" onchange="_syncFilterControlActive(this); window.pantheonCountryFilter=this.value; filterPantheonTable()">
                        <option value="">Tous</option>
                        ${countryOptions.map(c => `<option value="${escapeHtml(c)}"${countryFilter === c ? ' selected' : ''}>${escapeHtml(c)}</option>`).join('')}
                    </select></span></div></th>
                    <th>Œuvres notables</th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">En bibliothèque</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" id="pantheon-owned-filter" aria-label="Filtrer par présence en bibliothèque" onchange="_syncFilterControlActive(this); window.pantheonOwnedFilter=this.value; filterPantheonTable()">
                        <option value="">Tous</option>
                        <option value="1"${ownedFilter === '1' ? ' selected' : ''}>Oui</option>
                        <option value="0"${ownedFilter === '0' ? ' selected' : ''}>Non</option>
                    </select></span></div></th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                ${rows.map(x => `<tr class="series-table-row${x.already_owned ? '' : ' indispensable-row-missing'}">
                        <td class="indispensable-rank">${x.rank}</td>
                        <td><a class="indispensable-series-link" href="${escapeHtml(x.url)}" target="_blank" rel="noopener">${x.photo ? `<img src="${escapeHtml(x.photo)}" alt="" class="pantheon-author-photo">` : ''}${escapeHtml(x.name)}</a>${x.dates ? ` <span class="pantheon-author-dates">${escapeHtml(x.dates)}</span>` : ''}</td>
                        <td>${escapeHtml(x.professions || '-')}</td>
                        <td class="indispensable-category">${escapeHtml(x.country || '-')}</td>
                        <td class="pantheon-works-cell">${escapeHtml(x.notable_works || '-')}</td>
                        <td class="indispensable-owned" data-owned="${x.already_owned ? '1' : '0'}">${x.already_owned ? svgIcon('check') : svgIcon('x')}</td>
                        <td class="pantheon-action"><button class="btn-icon-only" onclick="viewPantheonAuthorBibliography('${escapeForAttribute(x.url)}', '${escapeForAttribute(x.name)}')" data-tooltip="Voir la bibliographie de cet auteur" aria-label="Voir la bibliographie de cet auteur">${svgIcon('book-open')}</button></td>
                    </tr>`).join('')}
            </tbody>
        </table>
    `;
    filterPantheonTable(query);
    initClearableSearchInputs(document.getElementById('pantheonList'));
}

function filterPantheonTable(query) {
    if (query !== undefined) window.pantheonHeaderFilter = String(query || '');
    const needle = (window.pantheonHeaderFilter || '').trim().toLowerCase();
    const countryFilter = window.pantheonCountryFilter || '';
    const ownedFilter = window.pantheonOwnedFilter || '';
    document.querySelectorAll('#pantheonList tbody tr').forEach(row => {
        const name = row.querySelector('.indispensable-series-link')?.textContent.toLowerCase() || '';
        const country = row.querySelector('.indispensable-category')?.textContent.trim() || '';
        const owned = row.querySelector('.indispensable-owned')?.dataset.owned || '';
        row.hidden = (!!needle && !name.includes(needle))
            || (!!countryFilter && country !== countryFilter)
            || (!!ownedFilter && owned !== ownedFilter);
    });
}

function sortPantheon(field) {
    const rows = [...document.querySelectorAll('#pantheonList tbody tr')];
    const col = field === 'name' ? 1 : 0;
    const current = document.querySelector(`.indispensable-sort-arrow[data-sort-field="${field}"]`)?.textContent;
    const dir = current === '↑' ? -1 : 1;
    rows.sort((a, b) => dir * a.cells[col].textContent.localeCompare(b.cells[col].textContent, 'fr', { numeric: true }));
    const body = document.querySelector('#pantheonList tbody');
    rows.forEach(r => body.appendChild(r));
    document.querySelectorAll('.indispensable-sort-arrow').forEach(a => a.textContent = a.dataset.sortField === field ? (dir === 1 ? '↑' : '↓') : '↕');
}

// Même mécanique que _syncIndispensableCategoryTabGroupActive (bedetheque-indispensables.js):
// le soulignement actif est porté par le groupe bouton+flèche (.indispensable-category-tab-group),
// pas par .tab seul, posé explicitement en JS plutôt que via :has() (déjà signalé peu fiable
// en pratique pour ce même besoin côté Indispensables).
function _syncPantheonCategoryTabGroupActive() {
    document.querySelectorAll('#pantheon .indispensable-category-tab-group').forEach(group => {
        group.classList.toggle('indispensable-category-tab-group-active', !!group.querySelector('.tab.active'));
    });
}

function openPantheonTab() {
    const all = document.getElementById('pantheon-cat-tab-all');
    if (all) {
        document.querySelectorAll('#pantheon .indispensable-category-tabs .tab').forEach(b => b.classList.remove('active'));
        all.classList.add('active');
        pantheonCategory = 'all';
        loadPantheon('all');
    } else {
        loadPantheon('all');
    }
    _syncPantheonCategoryTabGroupActive();
}

function selectPantheonCategory(category, button) {
    pantheonCategory = category;
    document.querySelectorAll('#pantheon .indispensable-category-tabs .tab').forEach(b => b.classList.toggle('active', b === button));
    loadPantheon(category);
    _syncPantheonCategoryTabGroupActive();
}

// "Voir bibliographie" bascule sur l'onglet "Par auteur" déjà existant (author-albums.js)
// sans passer par toggleAuthorPicker/selectDatabaseAuthor (ceux-ci exigent que l'auteur
// soit déjà connu en base via bedetheque_author_links - un auteur du Panthéon pas encore
// possédé n'y figure pas) - appelle directement loadAuthorAlbumsInline avec l'URL
// Bédéthèque de l'auteur, déjà connue depuis le scrape du Panthéon.
function openByAuthorTab() {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tabs > .tab').forEach(el => el.classList.remove('active'));
    document.getElementById('by-author').classList.add('active');
    document.getElementById('tab-by-author')?.classList.add('active');
}

function viewPantheonAuthorBibliography(url, name) {
    openByAuthorTab();
    const label = document.getElementById('author-picker-label');
    if (label) label.textContent = name;
    loadAuthorAlbumsInline(url, name);
}
