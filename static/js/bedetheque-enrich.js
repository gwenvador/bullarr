function escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

function escapeForAttribute(text) {
    return text.replace(/'/g, "\\'").replace(/"/g, '&quot;');
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

function renderDatabaseAuthorDropdown(query) {
    if (!databaseAuthors) return;
    const resultsEl = document.getElementById('author-search-results');
    const needle = navNormalizeSearch(String(query || '').trim());
    const matches = databaseAuthors.filter(a => !needle || navNormalizeSearch(a.name).includes(needle)).slice(0, 100);
    resultsEl.innerHTML = matches.length ? matches.map((a, i) => `<button type="button" class="series-combobox-item" onclick="selectDatabaseAuthor(${databaseAuthors.indexOf(a)})">${escapeHtml(a.name)}</button>`).join('') : '<div class="series-combobox-empty">Aucun auteur trouvé.</div>';
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
