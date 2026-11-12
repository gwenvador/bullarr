let indispensableItems = [];
let indispensableLibraryId = null;
let indispensableCategory = 'all';
const indispensableCache = {};
async function loadIndispensableLibraries() { const data=await (await fetch('/api/libraries')).json(); indispensableLibraryId = data?.[0]?.id || null; }
function escapeHtml(v) { return String(v || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
// "the data does not really change much so no need to update automatic. put only update
// manual in the enrichir page" - force=true (bouton "Actualiser", voir refreshIndispensables)
// contourne à la fois ce cache client ET le cache serveur (?refresh=1 - voir
// indispensable_series, bedetheque/routes.py, désormais sans expiration automatique).
async function loadIndispensables(category=indispensableCategory, force=false) {
    indispensableCategory=category;
    if (!force && indispensableCache[category]) { indispensableItems = indispensableCache[category]; renderIndispensables(); return; }
    const box=document.getElementById('indispensablesList'); box.innerHTML='<div class="loading"><div class="spinner"></div></div>';
    try { const url = '/api/bedetheque/indispensables?category='+encodeURIComponent(indispensableCategory)+(force?'&refresh=1':''); const data=await (await fetch(url)).json(); if(!data.success) throw Error(data.error); indispensableItems=data.items||[]; indispensableCache[category]=indispensableItems; renderIndispensables(); } catch(e) { box.innerHTML='<p class="help-text">Erreur : '+escapeHtml(e.message)+'</p>'; }
}

async function refreshIndispensables(button) {
    const original = button ? button.innerHTML : null;
    if (button) { button.disabled = true; button.innerHTML = svgIcon('loader-circle', 'icon-spin'); }
    // Les 3 autres catégories ont pu être préchargées en tâche de fond
    // (_loadOtherIndispensableCategoryCounts) - forcer un nouveau scrape pour la
    // catégorie affichée seulement, pas les 4, pour un rafraîchissement rapide et ciblé.
    await loadIndispensables(indispensableCategory, true);
    if (button) { button.disabled = false; button.innerHTML = original; }
}
const INDISPENSABLE_CATEGORIES = ['all', 'franco-belge', 'comics', 'manga'];

function _setIndispensableCategoryCount(category, owned, total) {
    const el = document.querySelector(`#indispensable-cat-tab-${category} .indispensable-cat-count`);
    if (el) { el.textContent = `${owned}/${total}`; el.classList.toggle('is-complete', total > 0 && owned === total); }
}

// Catégorie affichée: recalculé à chaque rendu (changement de catégorie, série
// ajoutée/supprimée) à partir de indispensableItems déjà en mémoire, sans requête
// supplémentaire.
function _updateIndispensablesTabCount() {
    const owned = indispensableItems.filter(x => x.already_in_library).length;
    _setIndispensableCategoryCount(indispensableCategory, owned, indispensableItems.length);
    _loadOtherIndispensableCategoryCounts();
}

// Les 3 autres catégories: chargées une seule fois en tâche de fond (résultats mis en
// cache 6h côté serveur, voir _INDISPENSABLES_CACHE/routes.py - un aller-retour réseau
// par catégorie, jamais response bloquante pour l'affichage de la catégorie active) pour
// que leur bouton affiche aussi un compte sans attendre que l'utilisateur clique dessus.
let _indispensableCategoryCountsLoaded = false;
async function _loadOtherIndispensableCategoryCounts() {
    if (_indispensableCategoryCountsLoaded) return;
    _indispensableCategoryCountsLoaded = true;
    for (const category of INDISPENSABLE_CATEGORIES) {
        if (category === indispensableCategory) continue;
        try {
            const data = await (await fetch('/api/bedetheque/indispensables?category=' + encodeURIComponent(category))).json();
            if (!data.success) continue;
            const items = data.items || [];
            _setIndispensableCategoryCount(category, items.filter(x => x.already_in_library).length, items.length);
        } catch (e) { /* best-effort - une catégorie qui échoue garde juste un bouton sans compte */ }
    }
}

function renderIndispensables() {
    const query = (window.indispensableHeaderFilter || '').toLowerCase().trim();
    const genreQuery = window.indispensableGenreFilter || '';
    const ownedFilter = window.indispensableOwnedFilter || '';
    const rows = indispensableItems;
    _updateIndispensablesTabCount();
    const ownedCount = rows.filter(x => x.already_in_library).length;
    const missingCount = rows.length - ownedCount;
    const pct = rows.length ? Math.round(100 * ownedCount / rows.length) : 0;
    document.getElementById('indispensablesList').innerHTML = `
        <div class="indisp-summary">
            <div class="indisp-summary-main">
                <span class="indisp-summary-count"><strong>${ownedCount}</strong> / ${rows.length}</span>
                <span class="indisp-summary-label">séries possédées</span>
                <span class="indisp-summary-pct">${pct} %</span>
            </div>
            <div class="indisp-summary-bar"><span style="width:${pct}%"></span></div>
            <div class="indisp-summary-filters" role="group" aria-label="Afficher">
                <button type="button" data-owned="" class="${ownedFilter === '' ? 'active' : ''}" onclick="setIndispensableOwnedFilter('')">Toutes <span>${rows.length}</span></button>
                <button type="button" data-owned="0" class="${ownedFilter === '0' ? 'active' : ''}" onclick="setIndispensableOwnedFilter('0')">À ajouter <span>${missingCount}</span></button>
                <button type="button" data-owned="1" class="${ownedFilter === '1' ? 'active' : ''}" onclick="setIndispensableOwnedFilter('1')">Possédées <span>${ownedCount}</span></button>
            </div>
        </div>
        <div id="indispensables-bulk-actions-bar" class="volume-bulk-actions-bar" style="display:none;"></div>
        <table class="series-table series-table-compact indisp-table">
            <thead>
                <tr>
                    <th class="volume-table-select-cell"><input type="checkbox" id="indispensablesSelectAll" aria-label="Sélectionner toutes les séries visibles" onchange="toggleAllIndispensables(this.checked)"></th>
                    <th class="volume-table-sortable" onclick="sortIndispensables('rank')"><div class="th-filterable-row"><span class="th-filterable-label"># <span class="indispensable-sort-arrow" data-sort-field="rank">↕</span></span></div></th>
                    <th class="volume-table-sortable" onclick="sortIndispensables('title')"><div class="th-filterable-row"><span class="th-filterable-label">Série <span class="indispensable-sort-arrow" data-sort-field="title">↕</span></span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(query)}" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterIndispensablesTable(this.value)"></span></div></th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">Genre</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(genreQuery)}" placeholder="Filtrer..." aria-label="Filtrer par genre" oninput="_syncFilterControlActive(this); window.indispensableGenreFilter=this.value; filterIndispensablesTable()"></span></div></th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">En bibliothèque</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" id="indispensables-owned-filter" aria-label="Filtrer par présence en bibliothèque" onchange="_syncFilterControlActive(this); setIndispensableOwnedFilter(this.value)">
                        <option value="">Tous</option>
                        <option value="1"${ownedFilter === '1' ? ' selected' : ''}>Oui</option>
                        <option value="0"${ownedFilter === '0' ? ' selected' : ''}>Non</option>
                    </select></span></div></th>
                    <th class="indispensable-action-head">Action</th>
                </tr>
            </thead>
            <tbody>
                ${rows.map(x => {
                    const i = indispensableItems.indexOf(x);
                    // "dans indispensables retire le bouton supprimer dans action. ne met
                    // rien" - une série déjà possédée n'affiche plus rien en Action (le ✓/✗
                    // de possession vit dans sa propre colonne, voir ci-dessous); supprimer
                    // une série depuis cet écran se fait maintenant depuis sa fiche série,
                    // pas ce tableau de découverte.
                    const actionHtml = x.already_in_library
                        ? (x.series_id ? `<a class="indisp-open" href="/series/${x.series_id}" data-tooltip="Ouvrir la fiche">${svgIcon('library')}<span>Fiche</span></a>` : '')
                        : `<button class="btn-icon-only indisp-add" onclick="addIndispensable(${i},this)" data-tooltip="Ajouter cette série" aria-label="Ajouter cette série">${svgIcon('plus')}<span>Ajouter</span></button>`;
                    const href = (x.already_in_library && x.series_id) ? `/series/${x.series_id}` : escapeHtml(x.url);
                    const external = !(x.already_in_library && x.series_id);
                    const cover = x.cover_path
                        ? `<img src="/${escapeHtml(x.cover_path)}" alt="" loading="lazy">`
                        : `<span class="indisp-cover-empty">${escapeHtml((x.title || '?').replace(/^(Les?|La|L'|Un|Une)\s+/i, '').slice(0, 1).toUpperCase())}</span>`;
                    const titleCellHtml = `<div class="indisp-series"><a class="indisp-cover" href="${href}"${external ? ' target="_blank" rel="noopener"' : ''} tabindex="-1">${cover}</a>
                        <div class="indisp-series-text"><a class="indispensable-series-link" href="${href}"${external ? ' target="_blank" rel="noopener"' : ''}>${escapeHtml(x.title)}</a><a class="indispensable-source-link" href="${escapeHtml(x.url)}" target="_blank" rel="noopener" data-tooltip="Voir sur Bédéthèque">↗</a></div></div>`;
                    const genres = String(x.genre || '').split(/\s*,\s*/).filter(g => g && g !== '-');
                    const genreHtml = genres.length ? genres.map(g => `<span class="indisp-genre">${escapeHtml(g)}</span>`).join('') : '<span class="indisp-none">—</span>';
                    return `<tr class="series-table-row${x.already_in_library ? '' : ' indispensable-row-missing'}">
                        <td class="volume-table-select-cell"><input type="checkbox" class="indispensable-checkbox" data-index="${i}" aria-label="Sélectionner ${escapeHtml(x.title)}" onchange="_updateIndispensablesBulkActionsBar()"${x.already_in_library ? ' disabled' : ''}></td>
                        <td class="indispensable-rank"><span class="indisp-rank${x.rank <= 3 ? ` indisp-rank-${x.rank}` : ''}">${x.rank}</span></td>
                        <td>${titleCellHtml}</td>
                        <td class="indispensable-category">${genreHtml}</td>
                        <td class="indispensable-owned" data-owned="${x.already_in_library ? '1' : '0'}">${x.already_in_library
                            ? `<span class="indisp-status indisp-status-ok">${svgIcon('check')} Possédée</span>`
                            : '<span class="indisp-status">Absente</span>'}</td>
                        <td class="indispensable-action">${actionHtml}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
    `;
    filterIndispensablesTable(query);
    _updateIndispensablesBulkActionsBar();
    initClearableSearchInputs(document.getElementById('indispensablesList'));
}

function notifyIndispensable(text, type = 'info') {
    const icon = type === 'error' ? 'triangle-alert' : type === 'success' ? 'check' : 'info';
    showToast('indispensable-notify', text, { icon, autoHideMs: type === 'error' ? 6000 : 4500 });
}

// query omis (appelé depuis les <select> Genre/Possédé) -> ne touche pas au filtre Série
// déjà en place, ne fait que ré-appliquer les 3 filtres combinés (ET logique, même
// principe que filterVolumesTableRows/filterSeriesTableRows).
// query omis (appelé depuis le filtre Genre/le <select> En bibliothèque) -> ne touche
// pas au filtre Série déjà en place, ne fait que ré-appliquer les 3 filtres combinés.
function filterIndispensablesTable(query) {
    if (query !== undefined) window.indispensableHeaderFilter = String(query || '');
    const needle = (window.indispensableHeaderFilter || '').trim().toLowerCase();
    const genreNeedle = (window.indispensableGenreFilter || '').trim().toLowerCase();
    const ownedFilter = window.indispensableOwnedFilter || '';
    document.querySelectorAll('#indispensablesList tbody tr').forEach(row => {
        const title = row.querySelector('.indispensable-series-link')?.textContent.toLowerCase() || '';
        const genre = row.querySelector('.indispensable-category')?.textContent.trim().toLowerCase() || '';
        const owned = row.querySelector('.indispensable-owned')?.dataset.owned || '';
        row.hidden = (!!needle && !title.includes(needle))
            || (!!genreNeedle && !genre.includes(genreNeedle))
            || (!!ownedFilter && owned !== ownedFilter);
    });
}

function setIndispensableOwnedFilter(value) {
    window.indispensableOwnedFilter = value;
    const select = document.getElementById('indispensables-owned-filter');
    if (select) { select.value = value; if (typeof _syncFilterControlActive === 'function') _syncFilterControlActive(select); }
    document.querySelectorAll('.indisp-summary-filters button').forEach(b => b.classList.toggle('active', b.dataset.owned === value));
    filterIndispensablesTable();
}

function _updateIndispensablesBulkActionsBar() {
    const bar = document.getElementById('indispensables-bulk-actions-bar');
    if (!bar) return;
    const count = document.querySelectorAll('.indispensable-checkbox:checked:not(:disabled)').length;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="volume-bulk-actions-count">${count} ${pluralize(count, 'série')} ${pluralize(count, 'sélectionnée')}</span>
        <button class="btn-neutral-sm" onclick="addSelectedIndispensables()">${svgIcon('plus')} Ajouter la sélection</button>
        <button class="btn-neutral-sm" onclick="toggleAllIndispensables(false); _updateIndispensablesBulkActionsBar()">Annuler la sélection</button>
    `;
}

function toggleAllIndispensables(checked) { document.querySelectorAll('.indispensable-checkbox:not(:disabled)').forEach(cb => { cb.checked = checked; }); _updateIndispensablesBulkActionsBar(); }
function _syncIndispensableCategoryTabGroupActive() {
    document.querySelectorAll('.indispensable-category-tab-group').forEach(group => {
        group.classList.toggle('indispensable-category-tab-group-active', !!group.querySelector('.tab.active'));
    });
}
function openIndispensablesTab() { const all=document.querySelector('.indispensable-category-tabs .tab'); if (all) { document.querySelectorAll('.indispensable-category-tabs .tab').forEach(b=>b.classList.remove('active')); all.classList.add('active'); indispensableCategory='all'; loadIndispensables('all'); } else loadIndispensables('all'); _syncIndispensableCategoryTabGroupActive(); }
function selectIndispensableCategory(category, button) { indispensableCategory=category; document.querySelectorAll('.indispensable-category-tabs .tab').forEach(b=>b.classList.toggle('active', b===button)); loadIndispensables(category); _syncIndispensableCategoryTabGroupActive(); }

async function addSelectedIndispensables() { const selected=[...document.querySelectorAll('.indispensable-checkbox:checked:not(:disabled)')]; if(!selected.length){notifyIndispensable('Sélectionne au moins une série.','info');return;} for(const cb of selected){ const button=cb.closest('tr').querySelector('button'); if (button) await addIndispensable(Number(cb.dataset.index),button); } }
async function addIndispensable(index,button) {
    const libraryId=indispensableLibraryId;
    const x=indispensableItems[index];
    if(!libraryId){notifyIndispensable('Choisis une bibliothèque.','error');return;}
    if(!x||!button)return;
    const original=button.innerHTML;
    const rowCheckbox = button.closest('tr')?.querySelector('.indispensable-checkbox');
    button.disabled=true;
    button.innerHTML=svgIcon('loader-circle', 'icon-spin');
    notifyIndispensable(`${x.title} : ajout en cours…`,'info');
    try {
        const r=await fetch('/api/bedetheque/add-series',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:x.url,library_id:libraryId})});
        const d=await r.json();
        if(!r.ok||!d.success) throw Error(d.error||'Échec');
        x.already_in_library=true;
        x.series_id = d.series_id || x.series_id;
        const seriesHref = x.series_id ? `/series/${x.series_id}` : null;
        if (seriesHref) {
            // Après ajout, conserver un repère permanent et un accès direct à la
            // fiche interne plutôt qu'un bouton « + » qui disparaît au prochain
            // rendu du tableau.
            button.outerHTML = `<a class="indisp-open indispensable-added" href="${seriesHref}" data-tooltip="Ouvrir la fiche">${svgIcon('check')}<span>Ajoutée</span></a>`;
        } else {
            button.innerHTML='Ajouté';
            button.setAttribute('data-tooltip', 'Ajoutée');
            button.classList.add('indispensable-added');
        }
        if (rowCheckbox) { rowCheckbox.checked = false; rowCheckbox.disabled = true; }
        const ownedCell = rowCheckbox?.closest('tr')?.querySelector('.indispensable-owned');
        if (ownedCell) { ownedCell.dataset.owned = '1'; ownedCell.innerHTML = `<span class="indisp-status indisp-status-ok">${svgIcon('check')} Possédée</span>`; ownedCell.closest('tr').classList.remove('indispensable-row-missing'); }
        _updateIndispensablesTabCount();
        notifyIndispensable(d.already_exists ? `La série « ${x.title} » est déjà présente dans la bibliothèque.` : `La série « ${x.title} » a été ajoutée à la bibliothèque.`,'success');
    } catch(e){
        button.disabled=false;
        button.classList.remove('indispensable-added');
        button.innerHTML=original;
        notifyIndispensable(`${x.title} : ${e.message}`,'error');
    }
}

// Le repli "!document.getElementById('indispensables') -> loadIndispensables('all')" ne
// servait qu'à l'ancienne page autonome /bedetheque-indispensables (supprimée, doublon de
// cet onglet) - seul appelant restant de ce script désormais, openIndispensablesTab()
// charge déjà la liste au clic sur l'onglet, pas besoin de la précharger ici.
document.addEventListener('DOMContentLoaded', () => { loadIndispensableLibraries(); });

function sortIndispensables(field) { const rows=[...document.querySelectorAll("#indispensablesList tbody tr")]; const col=field==="title"?2:1; const current=document.querySelector(".indispensable-sort-arrow[data-sort-field="+field+"]")?.textContent; const dir=current==="↑"?-1:1; rows.sort((a,b)=>dir*a.cells[col].textContent.localeCompare(b.cells[col].textContent,"fr",{numeric:true})); const body=document.querySelector("#indispensablesList tbody"); rows.forEach(r=>body.appendChild(r)); document.querySelectorAll(".indispensable-sort-arrow").forEach(a=>a.textContent=a.dataset.sortField===field?(dir===1?"↑":"↓"):"↕"); }
