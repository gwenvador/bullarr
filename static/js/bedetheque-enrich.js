function escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

function escapeForAttribute(text) {
    return String(text ?? '').replace(/&/g, '&amp;').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r/g, '\\r').replace(/\n/g, '\\n').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function switchTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
    document.getElementById(tabName).classList.add('active');
    event.target.classList.add('active');
}


// ============================================
// Onglet 3: Recherche par auteur (item #25 improvement.txt)
// ============================================

let databaseAuthors = null;
let databaseAuthorsLoading = null;

function toggleAuthorPicker() {
    const panel = document.getElementById('author-picker-panel');
    if (!panel) return;
    if (panel.style.display === 'none') {
        panel.style.display = 'block';
        const input = document.getElementById('author-search-input');
        input.value = '';
        loadDatabaseAuthors();
        input.focus();
        document.addEventListener('click', _closeAuthorPickerOnOutsideClick);
    } else {
        _closeAuthorPicker();
    }
}

function _closeAuthorPicker() {
    const panel = document.getElementById('author-picker-panel');
    if (panel) panel.style.display = 'none';
    document.removeEventListener('click', _closeAuthorPickerOnOutsideClick);
}

function _closeAuthorPickerOnOutsideClick(event) {
    if (!event.target.closest('#author-picker')) _closeAuthorPicker();
}

async function loadDatabaseAuthors() {
    if (databaseAuthors) { renderDatabaseAuthorDropdown(''); return; }
    if (!databaseAuthorsLoading) databaseAuthorsLoading = fetch('/api/bedetheque/authors/list').then(r => r.json()).then(data => { if (!data.success) throw Error(data.error || 'Erreur'); databaseAuthors = data.authors || []; });
    try { await databaseAuthorsLoading; renderDatabaseAuthorDropdown(''); } catch (e) { const resultsEl = document.getElementById('author-search-results'); resultsEl.innerHTML = `<div class="series-combobox-empty">Erreur : ${escapeHtml(e.message)}</div>`; }
}

// La base peut contenir des milliers d'auteurs: la liste est rendue par tranches et la suivante
// s'ajoute quand on approche du bas (un plafond fixe coupait le défilement après 100 auteurs).
const AUTHOR_PICKER_PAGE_SIZE = 100;
let authorPickerMatches = [];
let authorPickerRendered = 0;

function _appendAuthorPickerItems(resultsEl) {
    const slice = authorPickerMatches.slice(authorPickerRendered, authorPickerRendered + AUTHOR_PICKER_PAGE_SIZE);
    resultsEl.insertAdjacentHTML('beforeend', slice.map(index =>
        `<button type="button" class="series-combobox-item" onclick="selectDatabaseAuthor(${index})">${escapeHtml(databaseAuthors[index].name)}</button>`
    ).join(''));
    authorPickerRendered += slice.length;
}

function _onAuthorPickerScroll(event) {
    const list = event.currentTarget;
    if (authorPickerRendered >= authorPickerMatches.length) return;
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 80) _appendAuthorPickerItems(list);
}

function renderDatabaseAuthorDropdown(query) {
    if (!databaseAuthors) return;
    const resultsEl = document.getElementById('author-search-results');
    const needle = navNormalizeSearch(String(query || '').trim());
    authorPickerMatches = [];
    databaseAuthors.forEach((author, index) => {
        if (!needle || navNormalizeSearch(author.name).includes(needle)) authorPickerMatches.push(index);
    });
    authorPickerRendered = 0;
    resultsEl.scrollTop = 0;
    if (!resultsEl.dataset.scrollBound) {
        resultsEl.addEventListener('scroll', _onAuthorPickerScroll);
        resultsEl.dataset.scrollBound = '1';
    }
    if (!authorPickerMatches.length) {
        resultsEl.innerHTML = '<div class="series-combobox-empty">Aucun auteur trouvé.</div>';
        return;
    }
    resultsEl.innerHTML = '';
    _appendAuthorPickerItems(resultsEl);
}

function selectDatabaseAuthor(index) {
    const author = databaseAuthors?.[index];
    if (!author) return;
    document.getElementById('author-picker-label').textContent = author.name;
    _closeAuthorPicker();
    loadAuthorAlbumsInline(author.url, author.name);
}

// ============================================
// Modale couverture (Top 100 BDGest / détail d'un thème)
// ============================================

function openCoverModal(imageUrl, title) {
    const modal = document.getElementById('cover-image-modal');
    const image = document.getElementById('cover-image-modal-image');
    const caption = document.getElementById('cover-image-modal-caption');
    if (!modal || !image) return;
    image.src = imageUrl;
    image.alt = title || 'Couverture';
    if (caption) caption.textContent = title || '';
    modal.classList.add('active');
}

function closeCoverModal() {
    const modal = document.getElementById('cover-image-modal');
    const image = document.getElementById('cover-image-modal-image');
    if (!modal) return;
    modal.classList.remove('active');
    if (image) image.removeAttribute('src');
}

document.addEventListener('click', function(event) {
    const coverModal = document.getElementById('cover-image-modal');
    if (coverModal && event.target === coverModal) closeCoverModal();
});

document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
        const coverModal = document.getElementById('cover-image-modal');
        if (coverModal && coverModal.classList.contains('active')) closeCoverModal();
    }
});

// ============================================
// Init
// ============================================

window.addEventListener('load', () => {
    if (typeof openIndispensablesTab === 'function') openIndispensablesTab();
});


// ============================================
// Composants communs des onglets d'Enrichir (« ameliore toutes les tabs de enrichir ») :
// résumé avec progression + filtre segmenté, rang en pastille, statut lisible, boutons
// Ajouter / Fiche avec libellé. Le filtre segmenté pilote le <select> « présence en
// bibliothèque » déjà présent dans l'en-tête du tableau (même filtrage, une seule source).
// ============================================
function enrichRankHtml(rank) {
    const n = Number(rank);
    return `<span class="indisp-rank${n >= 1 && n <= 3 ? ` indisp-rank-${n}` : ''}">${escapeHtml(String(rank ?? ''))}</span>`;
}

function enrichStatusHtml(owned, okLabel = 'Possédée', noLabel = 'Absente') {
    return owned
        ? `<span class="indisp-status indisp-status-ok">${svgIcon('check')} ${okLabel}</span>`
        : `<span class="indisp-status">${noLabel}</span>`;
}

function enrichAddButtonHtml(onclick) {
    return `<button class="btn-icon-only enrich-add" onclick="${onclick}" data-tooltip="Ajouter cette série" aria-label="Ajouter cette série">${svgIcon('plus')}<span>Ajouter</span></button>`;
}

function enrichOpenHtml(seriesId, label = 'Fiche') {
    return seriesId ? `<a class="enrich-open" href="/series/${seriesId}" data-tooltip="Ouvrir la fiche">${svgIcon('library')}<span>${label}</span></a>` : '';
}

function enrichSummaryHtml(items, isOwned, currentFilter, label = 'séries possédées', missingLabel = 'À ajouter', ownedLabel = 'Possédées') {
    const total = items.length;
    const owned = items.filter(isOwned).length;
    const pct = total ? Math.round(100 * owned / total) : 0;
    const btn = (value, text, count) => `<button type="button" data-owned="${value}" class="${(currentFilter || '') === value ? 'active' : ''}" onclick="enrichSetOwnedFilter(this, '${value}')">${text} <span>${count}</span></button>`;
    return `<div class="indisp-summary enrich-summary">
            <div class="indisp-summary-main">
                <span class="indisp-summary-count"><strong>${owned}</strong> / ${total}</span>
                <span class="indisp-summary-label">${label}</span>
                <span class="indisp-summary-pct">${pct} %</span>
            </div>
            <div class="indisp-summary-bar"><span style="width:${pct}%"></span></div>
            <div class="indisp-summary-filters" role="group" aria-label="Afficher">
                ${btn('', 'Toutes', total)}${btn('0', missingLabel, total - owned)}${btn('1', ownedLabel, owned)}
            </div>
        </div>`;
}

// Bouton du filtre segmenté -> <select> de l'en-tête (son onchange existant filtre).
function enrichSetOwnedFilter(button, value) {
    const box = button.closest('.series-list');
    const select = box && box.querySelector('select[aria-label*="présence"]');
    if (!select) return;
    select.value = value;
    select.dispatchEvent(new Event('change', { bubbles: true }));
}

// Et inversement : le <select> de l'en-tête met à jour le filtre segmenté.
document.addEventListener('change', event => {
    const select = event.target;
    if (!(select instanceof HTMLSelectElement) || !/présence/.test(select.getAttribute('aria-label') || '')) return;
    const box = select.closest('.series-list');
    box?.querySelectorAll('.enrich-summary .indisp-summary-filters button')
        .forEach(b => b.classList.toggle('active', b.dataset.owned === select.value));
});

// Après un ajout réussi depuis une ligne : statut « Possédée » et bouton Fiche.
function enrichMarkAdded(button, seriesId) {
    const row = button.closest('tr');
    const cell = row?.querySelector('.indispensable-owned');
    if (cell) { cell.dataset.owned = '1'; cell.innerHTML = enrichStatusHtml(true); }
    row?.classList.remove('indispensable-row-missing');
    const checkbox = row?.querySelector('input[type="checkbox"]');
    if (checkbox) { checkbox.checked = false; checkbox.disabled = true; }
    button.outerHTML = seriesId ? enrichOpenHtml(seriesId, 'Ajoutée') : `<span class="indisp-status indisp-status-ok">${svgIcon('check')} Ajoutée</span>`;
}
