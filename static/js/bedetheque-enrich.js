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
