// ===== Onglet "Sorties BDGest" (bedetheque-enrich.html) =====
// Calendrier des sorties de https://www.bdgest.com/sorties_bd?vue=liste, par catégorie et par
// mois (blueprints/bdgest/routes.py, /api/bdgest/sorties). Même logique que "Top 100 BDGest":
// colonne "En bibliothèque" (série reconnue par titre) et bouton + pour l'ajouter
// (indispensableLibraryId / notifyIndispensable viennent de bedetheque-indispensables.js).
let sortiesCategorie = 'bd';
let sortiesItems = [];
const sortiesCache = {};
const SORTIES_MONTHS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre'];

function populateSortiesMonths() {
    const select = document.getElementById('sorties-month-input');
    if (select.options.length) return;
    const now = new Date();
    let html = '<option value="">Ces jours-ci</option>';
    for (let offset = -2; offset <= 8; offset++) {
        const d = new Date(now.getFullYear(), now.getMonth() + offset, 1);
        const value = `${String(d.getMonth() + 1).padStart(2, '0')}/${d.getFullYear()}`;
        html += `<option value="${value}">${SORTIES_MONTHS[d.getMonth()]} ${d.getFullYear()}</option>`;
    }
    select.innerHTML = html;
}

function openSortiesTab() {
    populateSortiesMonths();
    // switchTab() retire la classe active de tous les .tab, sous-onglets compris.
    document.querySelectorAll('#sorties [data-categorie]').forEach(b => b.classList.toggle('active', b.dataset.categorie === sortiesCategorie));
    loadSorties();
}

function selectSortiesCategorie(categorie, button) {
    sortiesCategorie = categorie;
    document.querySelectorAll('#sorties .indispensable-category-tabs .tab').forEach(b => b.classList.toggle('active', b === button));
    loadSorties();
}

async function loadSorties(force = false) {
    const mois = document.getElementById('sorties-month-input').value;
    const key = `${sortiesCategorie}|${mois}`;
    const box = document.getElementById('sortiesList');
    if (!force && sortiesCache[key]) {
        sortiesItems = sortiesCache[key];
        renderSorties();
        return;
    }
    box.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const url = `/api/bdgest/sorties?categorie=${encodeURIComponent(sortiesCategorie)}&mois=${encodeURIComponent(mois)}` + (force ? '&refresh=1' : '');
        const data = await (await fetch(url)).json();
        if (!data.success) throw Error(data.error);
        sortiesItems = data.items || [];
        sortiesCache[key] = sortiesItems;
        renderSorties();
    } catch (e) {
        box.innerHTML = `<p class="help-text">Erreur : ${escapeHtml(e.message)}</p>`;
    }
}

async function refreshSorties(button) {
    const original = button ? button.innerHTML : null;
    if (button) { button.disabled = true; button.innerHTML = svgIcon('loader-circle', 'icon-spin'); }
    await loadSorties(true);
    if (button) { button.disabled = false; button.innerHTML = original; }
}

function _sortiesDateLabel(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short' });
}

function filterSorties() {
    const ownedFilter = window.sortiesOwnedFilter || '';
    document.querySelectorAll('#sortiesList tbody tr').forEach(row => {
        const owned = row.querySelector('.indispensable-owned')?.dataset.owned || '';
        row.hidden = !!ownedFilter && owned !== ownedFilter;
    });
}

function renderSorties() {
    const box = document.getElementById('sortiesList');
    if (sortiesItems.length === 0) {
        box.innerHTML = '<p class="help-text">Aucune sortie trouvée pour cette période.</p>';
        return;
    }
    const ownedFilter = window.sortiesOwnedFilter || '';
    box.innerHTML = `
        <table class="series-table series-table-compact series-table-with-covers">
            <thead>
                <tr>
                    <th>Date</th>
                    <th></th>
                    <th>Album</th>
                    <th>Éditeur</th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">En bibliothèque</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par présence en bibliothèque" onchange="_syncFilterControlActive(this); window.sortiesOwnedFilter=this.value; filterSorties()">
                        <option value="">Tous</option>
                        <option value="1"${ownedFilter === '1' ? ' selected' : ''}>Oui</option>
                        <option value="0"${ownedFilter === '0' ? ' selected' : ''}>Non</option>
                    </select></span></div></th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                ${sortiesItems.map((x, i) => {
                    const titleHtml = (x.already_owned && x.series_id)
                        ? `<a class="indispensable-series-link" href="/series/${x.series_id}">${escapeHtml(x.title)}</a><a class="indispensable-source-link" href="${escapeHtml(x.url)}" target="_blank" rel="noopener" data-tooltip="Voir sur Bédéthèque">${svgIcon('external-link')}</a>`
                        : `<a class="indispensable-series-link" href="${escapeHtml(x.url)}" target="_blank" rel="noopener">${escapeHtml(x.title)}</a>`;
                    const detail = [x.volume_label, x.subtitle].filter(Boolean).join(' · ');
                    const actionHtml = x.already_owned
                        ? ''
                        : `<button class="btn-icon-only" onclick="addSortieSeries(${i}, this)" data-tooltip="Ajouter cette série" aria-label="Ajouter cette série">${svgIcon('plus')}</button>`;
                    return `<tr class="series-table-row${x.already_owned ? '' : ' indispensable-row-missing'}">
                        <td class="theme-note-cell" data-sort="${escapeHtml(x.date)}">${escapeHtml(_sortiesDateLabel(x.date))}</td>
                        <td class="theme-cover-cell">${x.cover ? `<img src="${escapeHtml(x.cover)}" alt="" loading="lazy" class="theme-cover-thumb" onclick="openCoverModal('${escapeForAttribute(x.cover)}', '${escapeForAttribute(x.title)}')" style="cursor:pointer;">` : ''}</td>
                        <td>${titleHtml}${detail ? `<div class="pantheon-author-dates">${escapeHtml(detail)}</div>` : ''}${x.authors ? `<p class="theme-summary">${escapeHtml(x.authors)}</p>` : ''}</td>
                        <td>${escapeHtml(x.publisher || '-')}</td>
                        <td class="indispensable-owned" data-owned="${x.already_owned ? '1' : '0'}">${x.already_owned ? svgIcon('check') : svgIcon('x')}</td>
                        <td class="pantheon-action">${actionHtml}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
    `;
    filterSorties();
}

async function addSortieSeries(index, button) {
    const libraryId = indispensableLibraryId;
    const x = sortiesItems[index];
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
        // Toutes les sorties de la même série passent en "possédée".
        sortiesItems.forEach(item => {
            if (item.title === x.title) { item.already_owned = true; item.series_id = d.series_id || item.series_id; }
        });
        renderSorties();
        notifyIndispensable(d.already_exists ? `La série « ${x.title} » est déjà présente dans la bibliothèque.` : `La série « ${x.title} » a été ajoutée à la bibliothèque.`, 'success');
    } catch (e) {
        button.disabled = false;
        button.innerHTML = original;
        notifyIndispensable(`${x.title} : ${e.message}`, 'error');
    }
}
