// Bibliographie d'un auteur (item #25 improvement.txt: "same in the album page click on
// author will open a modal for other album from the same author") - deux présentations
// possibles selon le contexte, mais un seul appel réseau/cache/logique d'ajout partagés
// (jamais deux implémentations séparées, voir CLAUDE.md "Unify search implementations",
// même philosophie appliquée ici à un composant d'ajout plutôt qu'à une recherche):
// - openAuthorAlbumsModal: modale, pour l'auteur cliquable de la fiche série (library.js) -
//   contexte de survol rapide, pas une page dédiée à ce sujet.
// - loadAuthorAlbumsInline: tableau directement dans la page, pour l'onglet "Par auteur"
//   de /bedetheque-enrich (bedetheque-enrich.js) - "plus de fenêtre modale. uniquement un
//   tableau dans la page", cet onglet EST la page dédiée à la recherche par auteur.
//
// "in bedetheque one author can have album in different language. keep the one in
// french" - le filtre français est appliqué CÔTÉ SERVEUR (voir GET
// /api/bedetheque/authors/albums), les deux ne reçoivent donc déjà que des séries
// francophones.

let _authorAlbumsCache = [];
// Lu par _updateAuthorAlbumsBulkActionsBar (défini plus loin) au moment de construire le
// bouton "Ajouter" de la barre - la seule bibliothèque possible quand il n'y a pas de
// <select> affiché (voir soleLibraryId dans _renderAuthorAlbumsTable).
let _authorAlbumsSoleLibraryId = null;

function _ensureAuthorAlbumsModal() {
    if (document.getElementById('author-albums-modal')) return;
    const div = document.createElement('div');
    div.className = 'modal';
    div.id = 'author-albums-modal';
    div.innerHTML = `
        <div class="modal-content settings-modal-content author-albums-modal-content">
            <span class="close-modal" onclick="closeAuthorAlbumsModal()">&times;</span>
            <div class="settings-section">
                <div class="section-header">
                    <div class="author-albums-modal-heading">
                        <span id="author-albums-modal-photo" class="author-albums-modal-photo-slot"></span>
                        <!-- "le lien bedetheque est trop a droite. peu visible. met le lien
                             sur le nom de l'auteur" - le nom lui-même est le lien vers sa
                             fiche BÉDÉTHÈQUE (authorUrl), plus une flèche séparée poussée à
                             droite du titre. Rempli par openAuthorAlbumsModal ci-dessous. -->
                        <h2 id="author-albums-modal-title">Albums de <a id="author-albums-modal-bedetheque-link" href="#" target="_blank" rel="noopener" class="missing-series-link" data-tooltip="Voir la fiche auteur sur Bédéthèque"></a></h2>
                    </div>
                    <p class="section-description">Séries en français trouvées sur Bédéthèque pour cet auteur - sélectionnez celles à ajouter à votre bibliothèque.</p>
                </div>
                <div id="author-albums-modal-body"></div>
            </div>
        </div>
    `;
    document.body.appendChild(div);
}

function closeAuthorAlbumsModal() {
    const modal = document.getElementById('author-albums-modal');
    if (modal) modal.classList.remove('active');
}

async function loadAuthorAlbumsModalPhoto(authorUrl) {
    try {
        const data = await (await fetch('/api/bedetheque/authors/photos?url=' + encodeURIComponent(authorUrl))).json();
        if (!data.success) return;
        const path = (data.photos || {})[authorUrl];
        const slot = document.getElementById('author-albums-modal-photo');
        if (path && slot) slot.innerHTML = `<img src="/${path}" alt="" class="author-albums-modal-photo">`;
    } catch (e) { /* best-effort - une photo manquante n'affecte que l'affichage */ }
}

async function openAuthorAlbumsModal(authorUrl, authorName) {
    _ensureAuthorAlbumsModal();
    const bedethequeLink = document.getElementById('author-albums-modal-bedetheque-link');
    bedethequeLink.textContent = authorName;
    bedethequeLink.href = authorUrl;
    // Réinitialisé à chaque ouverture - sinon la photo du précédent auteur consulté
    // resterait affichée pendant le chargement de celui-ci (l'élément est réutilisé,
    // pas recréé, voir _ensureAuthorAlbumsModal).
    document.getElementById('author-albums-modal-photo').innerHTML = '';
    const body = document.getElementById('author-albums-modal-body');
    body.innerHTML = '<div class="loading"><div class="spinner"></div><p>Chargement de la bibliographie...</p></div>';
    document.getElementById('author-albums-modal').classList.add('active');
    loadAuthorAlbumsModalPhoto(authorUrl);

    try {
        const response = await fetch(`/api/bedetheque/authors/albums?url=${encodeURIComponent(authorUrl)}`);
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        _authorAlbumsCache = data.albums || [];
        await _renderAuthorAlbumsList();
    } catch (error) {
        body.innerHTML = `<div class="error-box">Erreur: ${escapeHtml(error.message)}</div>`;
    }
}

async function _renderAuthorAlbumsList() {
    const body = document.getElementById('author-albums-modal-body');
    if (_authorAlbumsCache.length === 0) {
        body.innerHTML = '<div class="no-data"><p>Aucune série en français trouvée pour cet auteur.</p></div>';
        return;
    }

    let libraries = [];
    try {
        const response = await fetch('/api/libraries');
        const data = await response.json();
        libraries = Array.isArray(data) ? data : [];
    } catch (e) {
        libraries = [];
    }

    const librarySelectHtml = libraries.length > 1
        ? `<div class="form-group">
               <label for="author-albums-library-select">Bibliothèque</label>
               <select id="author-albums-library-select">
                   ${libraries.map(l => `<option value="${l.id}">${escapeHtml(l.name)}</option>`).join('')}
               </select>
           </div>`
        : '';
    const soleLibraryId = libraries.length === 1 ? libraries[0].id : null;

    _authorAlbumsSoleLibraryId = soleLibraryId;
    body.innerHTML = `
        <input type="text" id="author-albums-modal-search" class="search-input search-box" placeholder="Filtrer les séries..." oninput="_filterAuthorAlbumsModal(this.value)" style="width:100%; margin-bottom:12px;">
        ${librarySelectHtml}
        <label class="author-albums-select-all-row">
            <input type="checkbox" id="author-albums-modal-select-all" onchange="_toggleAllAuthorAlbums(this.checked)">
            Tout sélectionner (${_authorAlbumsCache.length})
        </label>
        <div id="author-albums-bulk-actions-bar" class="volume-bulk-actions-bar" style="display:none; margin-bottom:10px;"></div>
        <div id="author-albums-modal-no-match" class="no-data" style="display:none;"><p>Aucune série ne correspond à ce filtre.</p></div>
        <div class="author-albums-list">
            ${_authorAlbumsCache.map((a, i) => `
                <label class="overview-row author-album-row${a.already_in_library ? ' author-album-row-owned' : ''}" data-author-album-title="${escapeHtml(a.title.toLowerCase())}">
                    <input type="checkbox" class="author-album-checkbox" data-index="${i}" onchange="_updateAuthorAlbumsBulkActionsBar()" style="width:18px; height:18px; cursor:pointer; flex-shrink:0;">
                    <div class="overview-info">
                        <div class="overview-title">${escapeHtml(a.title)}</div>
                        <div class="overview-tags">
                            ${a.year_start ? `<span class="overview-tag">${a.year_start}${a.year_end && a.year_end !== a.year_start ? '–' + a.year_end : ''}</span>` : ''}
                            ${a.already_in_library ? `<span class="overview-tag author-album-owned-tag" data-tooltip="Déjà dans votre bibliothèque">${svgIcon('check')} En bibliothèque</span>` : ''}
                        </div>
                    </div>
                    <div class="overview-actions">
                        <a href="${escapeHtml(a.bedetheque_url)}" target="_blank" rel="noopener" class="poster-action-btn poster-action-btn-light" data-tooltip="Voir sur Bédéthèque" onclick="event.stopPropagation()"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:16px; height:16px; vertical-align:middle;"></a>
                    </div>
                </label>
            `).join('')}
        </div>
    `;
    initClearableSearchInputs(body);
}

// Filtre live de la modale "Albums de l'auteur" (_renderAuthorAlbumsList ci-dessus) -
// masque les .author-album-row dont le titre ne correspond pas, insensible à la casse.
// Ne touche pas à _authorAlbumsCache (les index data-index des cases à cocher doivent
// rester stables pour _addSelectedAuthorAlbums, qui lit par index dans le cache
// original) - un filtre display:none sur la ligne, jamais un nouveau rendu de liste.
function _filterAuthorAlbumsModal(value) {
    const needle = String(value || '').trim().toLowerCase();
    const rows = document.querySelectorAll('#author-albums-modal-body .author-album-row');
    let visibleCount = 0;
    rows.forEach(row => {
        const match = !needle || (row.dataset.authorAlbumTitle || '').includes(needle);
        row.style.display = match ? '' : 'none';
        if (match) visibleCount++;
    });
    const noMatch = document.getElementById('author-albums-modal-no-match');
    if (noMatch) noMatch.style.display = (visibleCount === 0 && rows.length > 0) ? '' : 'none';
}

async function loadAuthorAlbumsInline(authorUrl, authorName) {
    const containerEl = document.getElementById('author-albums-table-container');
    const titleEl = document.getElementById('author-albums-table-title');
    if (!containerEl) return;
    containerEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>Chargement de la bibliographie...</p></div>';
    if (titleEl) titleEl.textContent = '';

    try {
        const response = await fetch(`/api/bedetheque/authors/albums?url=${encodeURIComponent(authorUrl)}`);
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        _authorAlbumsCache = data.albums || [];
        await _renderAuthorAlbumsTable(containerEl, titleEl, authorName, authorUrl);
    } catch (error) {
        containerEl.innerHTML = `<div class="error-box">Erreur: ${escapeHtml(error.message)}</div>`;
    }
}

async function _renderAuthorAlbumsTable(containerEl, titleEl, authorName, authorUrl) {
    if (_authorAlbumsCache.length === 0) {
        if (titleEl) titleEl.textContent = `Aucune série en français trouvée pour ${authorName}.`;
        containerEl.innerHTML = '';
        return;
    }
    const authorLinkHtml = authorUrl
        ? ` <a href="${escapeHtml(authorUrl)}" target="_blank" rel="noopener" data-tooltip="Voir « ${escapeHtml(authorName)} » sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:16px; height:16px; vertical-align:middle;"></a>`
        : '';
    if (titleEl) titleEl.innerHTML = `${escapeHtml(`${_authorAlbumsCache.length} ${pluralize(_authorAlbumsCache.length, 'série')} de ${authorName} en français`)}${authorLinkHtml}`;

    let libraries = [];
    try {
        const response = await fetch('/api/libraries');
        const data = await response.json();
        libraries = Array.isArray(data) ? data : [];
    } catch (e) {
        libraries = [];
    }

    const librarySelectHtml = libraries.length > 1
        ? `<div class="form-group" style="margin-bottom:10px;">
               <label for="author-albums-library-select">Bibliothèque</label>
               <select id="author-albums-library-select">
                   ${libraries.map(l => `<option value="${l.id}">${escapeHtml(l.name)}</option>`).join('')}
               </select>
           </div>`
        : '';
    const soleLibraryId = libraries.length === 1 ? libraries[0].id : null;
    _authorAlbumsSoleLibraryId = soleLibraryId;

    window.authorAlbumsTitleFilter = '';
    window.authorAlbumsYearFilter = '';
    window.authorAlbumsOwnedFilter = '';
    const yearLabel = a => a.year_start ? `${a.year_start}${a.year_end && a.year_end !== a.year_start ? '–' + a.year_end : ''}` : '—';
    const yearOptions = [...new Set(_authorAlbumsCache.map(yearLabel))].sort((a, b) => a.localeCompare(b, 'fr', { numeric: true }));

    containerEl.innerHTML = `
        ${librarySelectHtml}
        <!-- "quand je clique checkbox dans les tableaux de enrichir ca doit ouvrir une
             nouvelle ligne au dessus du tableau pour matcher, ajouter, etc" - même pattern
             .volume-bulk-actions-bar que l'onglet "Indispensables BD"/"Matching incorrect"
             (voir bedetheque-indispensables.js/bedetheque-enrich.js): remplace le bouton
             "Ajouter la sélection" auparavant toujours visible sous le tableau. -->
        <div id="author-albums-bulk-actions-bar" class="volume-bulk-actions-bar" style="display:none; margin-bottom:12px;"></div>
        <table class="series-table series-table-compact">
            <thead>
                <tr>
                    <th class="volume-table-select-cell"><input type="checkbox" id="author-albums-select-all" aria-label="Sélectionner tous les albums" onchange="_toggleAllAuthorAlbums(this.checked)"></th>
                    <th class="volume-table-sortable" onclick="sortAuthorAlbums('title')"><div class="th-filterable-row"><span class="th-filterable-label">Série <span class="indispensable-sort-arrow" data-sort-field="title">↕</span></span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterAuthorAlbumsTable('title', this.value)"></span></div></th>
                    <th class="author-album-year-cell volume-table-sortable" onclick="sortAuthorAlbums('year')"><div class="th-filterable-row"><span class="th-filterable-label">Année <span class="indispensable-sort-arrow" data-sort-field="year">↕</span></span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par année" onchange="_syncFilterControlActive(this); filterAuthorAlbumsTable('year', this.value)">
                        <option value="">Toutes</option>
                        ${yearOptions.map(y => `<option value="${escapeHtml(y)}">${escapeHtml(y)}</option>`).join('')}
                    </select></span></div></th>
                    <th class="author-album-action-cell"><div class="th-filterable-row"><span class="th-filterable-label">Action</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par présence en bibliothèque" onchange="_syncFilterControlActive(this); filterAuthorAlbumsTable('owned', this.value)">
                        <option value="">Tous</option>
                        <option value="1">En bibliothèque</option>
                        <option value="0">Pas en bibliothèque</option>
                    </select></span></div></th>
                </tr>
            </thead>
            <tbody>
                ${_authorAlbumsCache.map((a, i) => `
                    <tr class="series-table-row${a.already_in_library ? ' author-album-row-owned' : ''}">
                        <td class="volume-table-select-cell"><input type="checkbox" class="author-album-checkbox" data-index="${i}" aria-label="Sélectionner ${escapeHtml(a.title)}" onchange="_updateAuthorAlbumsBulkActionsBar()"></td>
                        <td><a class="author-album-link" href="${escapeHtml(a.bedetheque_url)}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a></td>
                        <td class="author-album-year-cell">${yearLabel(a)}</td>
                        <td class="author-album-action-cell" data-owned="${a.already_in_library ? '1' : '0'}">${a.already_in_library
                            ? `<span class="author-album-owned-tag" data-tooltip="Déjà dans votre bibliothèque">${svgIcon('check')} En bibliothèque</span>`
                            : `<a href="${escapeHtml(a.bedetheque_url)}" target="_blank" rel="noopener" class="btn-icon-only" data-tooltip="Voir sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:16px; height:16px; vertical-align:middle;"></a>`}</td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
    initClearableSearchInputs(containerEl);
}

const AUTHOR_ALBUMS_SORT_COLUMNS = { title: 1, year: 2 };
function sortAuthorAlbums(field) {
    const body = document.querySelector('#author-albums-table-container tbody');
    if (!body) return;
    const rows = [...body.querySelectorAll('tr')];
    const col = AUTHOR_ALBUMS_SORT_COLUMNS[field];
    const current = document.querySelector(`#author-albums-table-container .indispensable-sort-arrow[data-sort-field="${field}"]`)?.textContent;
    const dir = current === '↑' ? -1 : 1;
    rows.sort((a, b) => dir * a.cells[col].textContent.localeCompare(b.cells[col].textContent, 'fr', { numeric: true }));
    rows.forEach(r => body.appendChild(r));
    document.querySelectorAll('#author-albums-table-container .indispensable-sort-arrow').forEach(a => a.textContent = a.dataset.sortField === field ? (dir === 1 ? '↑' : '↓') : '↕');
}

// Filtre par colonne (Série/Année/Action) - même principe ET-combiné que
// filterMissingSeriesTable/filterVolumesTableRows: une ligne reste visible seulement si
// elle correspond à TOUS les filtres actifs à la fois.
function filterAuthorAlbumsTable(field, value) {
    if (field === 'title') window.authorAlbumsTitleFilter = String(value || '');
    else if (field === 'year') window.authorAlbumsYearFilter = String(value || '');
    else if (field === 'owned') window.authorAlbumsOwnedFilter = String(value || '');
    const needle = (window.authorAlbumsTitleFilter || '').trim().toLowerCase();
    const yearFilter = window.authorAlbumsYearFilter || '';
    const ownedFilter = window.authorAlbumsOwnedFilter || '';
    document.querySelectorAll('#author-albums-table-container .series-table-row').forEach(row => {
        const title = row.querySelector('.author-album-link')?.textContent?.toLowerCase() || '';
        const year = row.querySelector('.author-album-year-cell')?.textContent?.trim() || '';
        const owned = row.querySelector('.author-album-action-cell')?.dataset.owned || '';
        row.hidden = (!!needle && !title.includes(needle))
            || (!!yearFilter && year !== yearFilter)
            || (!!ownedFilter && owned !== ownedFilter);
    });
}

function _toggleAllAuthorAlbums(checked) {
    document.querySelectorAll('.author-album-checkbox').forEach(cb => { cb.checked = checked; });
    _updateAuthorAlbumsBulkActionsBar();
}

function _updateAuthorAlbumsBulkActionsBar() {
    const bar = document.getElementById('author-albums-bulk-actions-bar');
    if (!bar) return;
    const count = document.querySelectorAll('.author-album-checkbox:checked').length;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="volume-bulk-actions-count">${count} ${pluralize(count, 'série')} ${pluralize(count, 'sélectionnée')}</span>
        <button class="btn-neutral-sm" onclick="_addSelectedAuthorAlbums(${_authorAlbumsSoleLibraryId ?? 'null'})">${svgIcon('plus')} Ajouter la sélection</button>
        <button class="btn-neutral-sm" onclick="_toggleAllAuthorAlbums(false)">Annuler la sélection</button>
    `;
}

// Séquentiel (pas Promise.all) - même raisonnement que le pattern d'action groupée déjà
// utilisé sur /verification et les tomes d'une fiche série (voir CLAUDE.md): l'ajout
// scrape chaque fiche Bédéthèque avec son propre délai anti-bot, les lancer en parallèle
// depuis le client ne serait pas plus rapide, juste plus de requêtes en vol à la fois.
async function _addSelectedAuthorAlbums(soleLibraryId) {
    const checkboxes = Array.from(document.querySelectorAll('.author-album-checkbox:checked'));
    if (checkboxes.length === 0) {
        showToast('author-albums-add', 'Sélectionnez au moins une série.', { icon: 'triangle-alert', autoHideMs: 4000 });
        return;
    }
    let libraryId = soleLibraryId;
    if (!libraryId) {
        const select = document.getElementById('author-albums-library-select');
        libraryId = select ? parseInt(select.value, 10) : null;
    }
    if (!libraryId) {
        showToast('author-albums-add', 'Bibliothèque requise.', { icon: 'triangle-alert', autoHideMs: 4000 });
        return;
    }

    showToast('author-albums-add', `Ajout de ${checkboxes.length} ${pluralize(checkboxes.length, 'série')}...`, { icon: 'loader-circle' });

    let failed = 0;
    const addedSeries = [];
    for (const cb of checkboxes) {
        const album = _authorAlbumsCache[parseInt(cb.dataset.index, 10)];
        try {
            const response = await fetch('/api/bedetheque/add-series', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: album.bedetheque_url, library_id: libraryId })
            });
            const data = await response.json();
            if (data.success) addedSeries.push({ id: data.series_id, title: album.title }); else failed++;
        } catch (e) {
            failed++;
        }
    }
    const added = addedSeries.length;
    showToast(
        'author-albums-add',
        `${added} ${pluralize(added, 'série')} ${pluralize(added, 'ajoutée')}${failed ? `, ${failed} ${pluralize(failed, 'échec')}` : ''}.`,
        {
            icon: failed ? 'triangle-alert' : 'check',
            autoHideMs: 6000,
            href: added === 1 ? `/series/${addedSeries[0].id}` : undefined
        }
    );
}
