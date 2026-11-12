// Page Lectures (groupe Suivi) : historique de lecture du compte courant (Komga synchronisé
// ou lecteur de Bullarr), filtres, export et import CSV pour sauvegarde.
// Données : GET /api/reading-history ; export : GET /api/reading-history/export.csv ;
// import : POST /api/reading-history/import (multipart « file ») ; synchronisation Komga :
// POST /api/komga/read-progress/sync.
let readingData = null;
const readingState = { status: 'all', query: '', library: '', user: '' };
let readingUsers = null;

// Administrateur : choix du compte dont on voit l'historique (?user=). Lecteur : le sien.
function _readingUserQuery(prefix) {
    return readingState.user ? `${prefix}user=${encodeURIComponent(readingState.user)}` : '';
}

async function loadReadingUsers() {
    if (document.body.dataset.role === 'reader') return;
    try {
        const data = await (await fetch('/api/reading-history/users')).json();
        if (!data.success || !data.users || data.users.length < 2) return;
        readingUsers = data.users;
        const params = new URLSearchParams(location.search);
        const wanted = params.get('user');
        if (wanted && readingUsers.some(u => u.user_key === wanted)) readingState.user = wanted;
    } catch (e) { /* liste facultative */ }
}

function readingSelectUser(value) {
    const me = readingUsers?.find(u => u.me);
    readingState.user = me && value === me.user_key ? '' : value;
    const url = new URL(location.href);
    if (readingState.user) url.searchParams.set('user', readingState.user); else url.searchParams.delete('user');
    history.replaceState(null, '', url);
    readingState.library = '';
    readingState.limit = 300;
    loadReadingHistory();
}

function _rdEsc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _rdDate(value, withTime) {
    if (!value) return '—';
    const date = new Date(String(value).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(value) || !/\d\d:\d\d/.test(value) ? '' : 'Z'));
    if (isNaN(date)) return value;
    return date.toLocaleString('fr-FR', withTime
        ? { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }
        : { day: 'numeric', month: 'short', year: 'numeric' });
}

async function loadReadingHistory() {
    try {
        const data = await (await fetch(`/api/reading-history${_readingUserQuery('?')}`)).json();
        if (!data.success) throw new Error(data.error || 'Erreur de chargement');
        readingData = data;
        const exportLink = document.getElementById('readingExportLink');
        if (exportLink) exportLink.href = `/api/reading-history/export.csv${_readingUserQuery('?')}`;
        renderReadingSource();
        renderReadingLibraries();
        renderReadingHistory();
    } catch (error) {
        document.getElementById('readingList').innerHTML = `<p class="upscaling-muted">${svgIcon('triangle-alert')} ${_rdEsc(error.message)}</p>`;
    }
}

function renderReadingSource() {
    const data = readingData;
    const sync = data.source !== 'local';
    const me = readingUsers?.find(u => u.me);
    const picker = readingUsers ? `<label class="reading-user"><span>Compte</span><select class="upscaling-input" onchange="readingSelectUser(this.value)">${readingUsers.map(u =>
        `<option value="${_rdEsc(u.user_key)}"${(readingState.user || me?.user_key) === u.user_key ? ' selected' : ''}>${_rdEsc(u.name)}${u.me ? ' (moi)' : ''}${u.role === 'reader' ? ' · lecteur' : ''}</option>`).join('')}</select></label>` : '';
    const foreign = !!readingState.user;
    document.getElementById('readingSource').innerHTML = `${picker}
        <span class="upscale-badge upscale-badge-ok">${svgIcon(sync ? 'refresh-cw' : 'book-open')} ${_rdEsc(data.source_label)}</span>
        ${sync && !foreign ? `<span class="upscaling-muted">Dernière synchronisation : ${_rdDate(data.last_sync, true)}</span>
            <button type="button" class="btn btn-ghost" id="readingSyncBtn" onclick="syncReadingHistory()">${svgIcon('refresh-cw')} Synchroniser</button>` : ''}`;
}

function renderReadingLibraries() {
    const select = document.getElementById('readingLibrary');
    const libraries = new Map();
    (readingData.items || []).forEach(item => { if (item.library_id) libraries.set(item.library_id, item.library_name || `#${item.library_id}`); });
    select.innerHTML = '<option value="">Toutes les bibliothèques</option>' + [...libraries]
        .map(([id, name]) => `<option value="${id}"${String(id) === readingState.library ? ' selected' : ''}>${_rdEsc(name)}</option>`).join('');
    select.closest('label').hidden = libraries.size < 2;
}

function _readingFiltered() {
    const query = readingState.query.trim().toLowerCase();
    return (readingData?.items || []).filter(item =>
        (readingState.status === 'all' || (readingState.status === 'done') === item.completed)
        && (!readingState.library || String(item.library_id) === readingState.library)
        && (!query || `${item.series_title} ${item.label} ${item.filename}`.toLowerCase().includes(query)));
}

function renderReadingHistory() {
    const all = readingData?.items || [];
    const done = all.filter(item => item.completed).length;
    document.getElementById('readingCountAll').textContent = all.length;
    document.getElementById('readingCountDone').textContent = done;
    document.getElementById('readingCountStarted').textContent = all.length - done;
    document.querySelectorAll('.reading-seg [data-status]').forEach(btn => btn.classList.toggle('active', btn.dataset.status === readingState.status));
    const items = _readingFiltered();
    const list = document.getElementById('readingList');
    if (!all.length) {
        list.innerHTML = `<p class="upscaling-muted">${svgIcon('book-open')} Aucune lecture pour l'instant. Les albums lus dans Komga ou dans le lecteur de Bullarr apparaissent ici.</p>`;
        return;
    }
    if (!items.length) {
        list.innerHTML = '<p class="upscaling-muted">Aucune lecture ne correspond aux filtres.</p>';
        return;
    }
    // Rendu limité pour garder la page fluide sur de très longs historiques.
    const shown = items.slice(0, readingState.limit || 300);
    list.innerHTML = `<table class="upscaling-table reading-table"><thead><tr>
            <th>Album</th><th>Progression</th><th>Dernière lecture</th><th class="upscaling-end">Statut</th>
        </tr></thead><tbody>${shown.map(item => {
        const pages = item.pages_count || 0;
        const page = item.completed ? (pages || item.page || 0) : (item.page || 0);
        const pct = item.completed ? 100 : (pages ? Math.min(100, Math.round(100 * page / pages)) : 0);
        const cover = item.cover_path ? `<img src="/${_rdEsc(item.cover_path)}" alt="" loading="lazy">` : '<span>📖</span>';
        return `<tr>
            <td><div class="reading-album"><span class="reading-cover">${cover}</span>
                <div class="upscaling-album"><a href="/series/${item.series_id}">${_rdEsc(item.series_title)}</a><span>${_rdEsc(item.label)}${item.library_name && readingData.items.some(i => i.library_id !== item.library_id) ? ` · ${_rdEsc(item.library_name)}` : ''}</span></div></div></td>
            <td><span class="upscaling-progress"><span class="upscale-progress reading-progress${item.completed ? ' done' : ''}"><span style="width:${pct}%"></span></span>
                <span class="upscale-progress-label upscaling-progress-label">${pages ? `${page}/${pages}` : `p. ${page}`}</span></span></td>
            <td class="upscaling-nowrap">${_rdDate(item.read_at, true)}</td>
            <td class="upscaling-end">${item.completed
                ? `<span class="upscale-badge upscale-badge-ok">${svgIcon('check')} Lu</span>`
                : `<span class="upscale-badge">${pct} %</span>`}</td></tr>`;
    }).join('')}</tbody></table>${items.length > shown.length
        ? `<div class="reading-more"><button type="button" class="btn" onclick="readingState.limit = ${shown.length + 300}; renderReadingHistory()">Afficher plus (${items.length - shown.length} restantes)</button></div>` : ''}`;
}

function readingSetStatus(status) {
    readingState.status = status;
    readingState.limit = 300;
    renderReadingHistory();
}

function readingApplyFilters() {
    readingState.query = document.getElementById('readingFilter').value;
    readingState.library = document.getElementById('readingLibrary').value;
    readingState.limit = 300;
    renderReadingHistory();
}

async function syncReadingHistory() {
    const btn = document.getElementById('readingSyncBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = `${svgIcon('loader-circle', 'icon-spin')} Synchronisation…`; }
    try {
        const data = await (await fetch('/api/komga/read-progress/sync', { method: 'POST' })).json();
        if (!data.success) throw new Error(data.error || 'Synchronisation impossible');
        showToast('reading-sync', 'Avancement relu dans Komga.', { icon: 'check', autoHideMs: 3000 });
    } catch (error) {
        alert('❌ ' + error.message);
    }
    loadReadingHistory();
}

async function readingImportFile(input) {
    const file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    const where = readingData?.source === 'local' ? 'dans Bullarr' : 'dans Komga';
    const who = readingState.user ? ` du compte « ${readingUsers?.find(u => u.user_key === readingState.user)?.name || readingState.user} »` : '';
    if (!confirm(`Importer « ${file.name} »${who} ?\n\nL'avancement de chaque album est remis ${where}. Un album déjà lu, ou lu plus loin que dans le fichier, n'est jamais ramené en arrière.`)) return;
    const result = document.getElementById('readingImportResult');
    result.className = 'message';
    result.textContent = 'Import en cours…';
    result.style.display = 'block';
    try {
        const body = new FormData();
        body.append('file', file);
        const data = await (await fetch(`/api/reading-history/import${_readingUserQuery('?')}`, { method: 'POST', body })).json();
        if (!data.success) throw new Error(data.error || 'Import impossible');
        const parts = [`${data.applied} album(s) mis à jour`, `${data.unchanged} déjà à jour`];
        if (data.unmatched) parts.push(`${data.unmatched} introuvable(s) dans la bibliothèque`);
        if (data.error_count) parts.push(`${data.error_count} en échec (ex. ${data.errors[0].serie || ''} ${data.errors[0].tome || ''} : ${data.errors[0].error})`);
        result.className = `message ${data.error_count ? 'warning' : 'success'}`;
        result.textContent = `${data.total} ligne(s) lue(s) : ${parts.join(', ')}.`;
        loadReadingHistory();
    } catch (error) {
        result.className = 'message error';
        result.textContent = '❌ ' + error.message;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-rd-icon]').forEach(el => { el.outerHTML = svgIcon(el.dataset.rdIcon); });
    loadReadingUsers().then(loadReadingHistory);
});
