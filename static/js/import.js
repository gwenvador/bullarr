async function _fetchWithTimeout(url, options = {}, timeoutMs = 20000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(timer);
    }
}

let importFiles = [];
let incompatibleFolders = [];
let currentFileIndices = [];
let allLibraries = [];
let librariesSeriesMap = {};
// Distingue "le tout premier rendu de la page n'a pas encore eu lieu" de "rendu, et rien
// à montrer" - mis à true inconditionnellement dès window.addEventListener('load', ...)
// (voir plus bas), plus par un scan lui-même: "for import there should be a database of
// all the downloads... do not display what files are on disk" - il n'y a plus de scan de
// répertoire automatique au chargement, seulement le cache local + les téléchargements
// actifs/en attente en base, un scan ne se déclenche plus que via le bouton Actualiser.
let hasScannedOnce = false;
let hasCheckedActiveDownloadsOnce = false;
// Tri par en-tête cliquable ("tous les tableaux doivent pouvoir etre ordonné en cliquant
// sur leur header") - column: null revient à l'ordre par défaut (regroupement par série
// devinée puis numéro de tome, voir plus bas) plutôt qu'un tri de colonne explicite.
let importTableSort = { column: null, direction: 'asc' };
let importNameFilter = '';
let importAlbumFilter = '';
let importTypeFilter = '';
let importVolumeFilter = '';
let importExtensionFilter = '';

function _importFileExtension(file) {
    const match = /\.([a-z0-9]+)$/i.exec(file.filename || '');
    return match ? match[1].toLowerCase() : '';
}
// Select comme Type/Volume (voir leurs commentaires) - _importFileExtension renvoie
// toujours une extension en minuscules, la valeur du <option> DOIT rester dans la même
// casse que ce qui est comparé (_importExtensionMatches) pour ne pas reproduire le bug
// "ca marche pas" du tout premier essai - seul le texte affiché est mis en majuscules
// (cohérent avec le style de la cellule Ext., text-transform:uppercase).
function _importExtensionFilterHeaderHtml(availableExtensions) {
    const extensions = new Set(availableExtensions);
    if (importExtensionFilter) extensions.add(importExtensionFilter);
    const optionsHtml = [...extensions].sort((a, b) => a.localeCompare(b, 'fr'))
        .map(ext => `<option value="${escapeHtml(ext)}"${importExtensionFilter === ext ? ' selected' : ''}>${escapeHtml(ext.toUpperCase())}</option>`)
        .join('');
    return `<th><div class="th-filterable-row">` +
        `<span class="th-filterable-label">Ext.</span>` +
        `<span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>` +
        `<select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par extension" onchange="_syncFilterControlActive(this); setImportExtensionFilter(this.value)"><option value="">Tous</option>${optionsHtml}</select></span>` +
        `</div></th>`;
}
function setImportExtensionFilter(value) { importExtensionFilter = value || ''; displayImportFiles(); }
function _importExtensionMatches(value) { return !importExtensionFilter || value === importExtensionFilter; }

function _importFileSortValue(file, column) {
    switch (column) {
        case 'client': return file.client || '';
        case 'name': return (file.filename || '').toLowerCase();
        case 'date': return Number(file.mtime || 0);
        case 'volume': {
            const parsed = file.parsed || {};
            const raw = parsed.volume != null ? parsed.volume : file.volume_number;
            if (raw != null && raw !== '') {
                const number = Number(raw);
                return Number.isFinite(number) ? number : String(raw);
            }
            if (parsed.is_integral) return 1000000 + Number(parsed.integral_number || 0);
            if (parsed.is_hs) return 2000000 + Number(parsed.hs_number || 0);
            if (parsed.is_episode) return 3000000 + Number(parsed.episode_number || 0);
            return 9000000;
        }
        case 'status': return file.destination ? 1 : 0;
        default: return 0;
    }
}

function _compareImportFiles(a, b, column, direction) {
    const va = _importFileSortValue(a, column);
    const vb = _importFileSortValue(b, column);
    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    if (cmp !== 0) return direction === 'asc' ? cmp : -cmp;
    const nameCmp = String(a.filename || '').localeCompare(String(b.filename || ''), 'fr', { numeric: true, sensitivity: 'base' });
    return direction === 'asc' ? nameCmp : -nameCmp;
}

function _compareImportDateRows(a, b, direction) {
    const aItem = a && a.item ? a.item : a;
    const bItem = b && b.item ? b.item : b;
    const va = Date.parse(aItem?.created_at || '') || 0;
    const vb = Date.parse(bItem?.created_at || '') || 0;
    if (va !== vb) return direction === 'asc' ? va - vb : vb - va;
    return String(aItem?.name || aItem?.title || '').localeCompare(
        String(bItem?.name || bItem?.title || ''), 'fr', { numeric: true, sensitivity: 'base' }
    );
}

function _compareImportDownloadRows(a, b, direction) {
    const aItem = a && a.item ? a.item : a;
    const bItem = b && b.item ? b.item : b;
    const va = _importFileSortValue({
        parsed: { volume: aItem?.volume_number },
        filename: aItem?.name || aItem?.title || ''
    }, 'volume');
    const vb = _importFileSortValue({
        parsed: { volume: bItem?.volume_number },
        filename: bItem?.name || bItem?.title || ''
    }, 'volume');
    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    if (cmp !== 0) return direction === 'asc' ? cmp : -cmp;
    const nameCmp = String(aItem?.name || aItem?.title || '').localeCompare(
        String(bItem?.name || bItem?.title || ''), 'fr', { numeric: true, sensitivity: 'base' }
    );
    return direction === 'asc' ? nameCmp : -nameCmp;
}

function setImportTableSort(column) {
    if (importTableSort.column === column) {
        importTableSort.direction = importTableSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        importTableSort.column = column;
        importTableSort.direction = 'asc';
    }
    displayImportFiles();
}

// Style en ligne plutôt que .volume-table-sortable/.volume-table-sort-active
// (style-library-search.css): import.html ne charge pas cette feuille (seulement
// style-settings.css/style.css), ces classes n'y avaient donc aucun effet (curseur
// normal, pas de surbrillance sur la colonne triée) - même traitement inline que
// history.js/ebdz-latest.js, qui ont le même besoin sur des pages sans cette feuille.
function _importTableHeaderHtml(column, label) {
    const active = importTableSort.column === column;
    const arrow = active ? (importTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    const style = `cursor:pointer; user-select:none;${active ? ' color:var(--color-accent); font-weight:600;' : ''}`;
    return `<th style="${style}" onclick="setImportTableSort('${column}')">${label}${arrow}</th>`;
}

function _importNameFilterHeaderHtml() {
    const active = importTableSort.column === 'name';
    const arrow = active ? (importTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    return `<th${active ? ' style="color:var(--color-accent); font-weight:600;"' : ''}><div class="th-filterable-row">` +
        `<span class="th-filterable-label" style="cursor:pointer; user-select:none;" onclick="setImportTableSort('name')">Nom${arrow}</span>` +
        `<span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>` +
        `<input type="text" class="series-table-filter-input import-name-filter-input th-filterable-control" value="${escapeHtml(importNameFilter)}" placeholder="Filtrer..." aria-label="Filtrer par nom" oninput="_syncFilterControlActive(this); setImportNameFilter(this.value)"></span>` +
        `</div></th>`;
}
function setImportNameFilter(value) {
    const input = document.activeElement;
    const cursor = input && input.selectionStart != null ? input.selectionStart : String(value || '').length;
    importNameFilter = value || '';
    displayImportFiles();
    requestAnimationFrame(() => {
        const next = document.querySelector('.import-files-table .import-name-filter-input');
        if (next) { next.focus(); next.setSelectionRange(cursor, cursor); }
    });
}
function _importNameMatches(value) { const q = String(importNameFilter || '').trim().toLocaleLowerCase(); return !q || String(value || '').toLocaleLowerCase().includes(q); }

// Type (client): select comme "Possédé"/"Format" dans les tableaux de la bibliothèque,
// pas un champ texte libre. "type should only display what is currently available not
// all the options in clients" - CLIENT_LABELS liste TOUS les clients supportés par l'app
// (8: qBittorrent/rTorrent/Deluge/aMule/Telegram/fourtoutici/Shelfmark/torrent partagé),
// presque toujours plus large que ce qui est réellement présent dans la file d'import à
// un instant donné - availableClientKeys (calculé par l'appelant sur importFiles/
// activeDownloads/pending, voir displayImportFiles) restreint les options à ce qui existe
// vraiment. Calculé sur les données NON filtrées par Nom/Album/Volume - un dropdown Type
// qui rétrécirait tout seul selon un AUTRE filtre actif serait déroutant. La valeur
// actuellement sélectionnée reste toujours proposée même si elle a entre-temps disparu
// des données (évite un select qui "oublie" silencieusement le filtre actif).
function _importTypeFilterHeaderHtml(availableClientKeys) {
    const active = importTableSort.column === 'client';
    const arrow = active ? (importTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    const keys = new Set(availableClientKeys);
    if (importTypeFilter) keys.add(importTypeFilter);
    const optionsHtml = [...keys]
        .sort((a, b) => (CLIENT_LABELS[a] || a).localeCompare(CLIENT_LABELS[b] || b, 'fr'))
        .map(key => `<option value="${escapeHtml(key)}"${importTypeFilter === key ? ' selected' : ''}>${escapeHtml(CLIENT_LABELS[key] || key)}</option>`)
        .join('');
    return `<th${active ? ' style="color:var(--color-accent); font-weight:600;"' : ''}><div class="th-filterable-row">` +
        `<span class="th-filterable-label" style="cursor:pointer; user-select:none;" onclick="setImportTableSort('client')">Type${arrow}</span>` +
        `<span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>` +
        `<select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par type" onchange="_syncFilterControlActive(this); setImportTypeFilter(this.value)"><option value="">Tous</option>${optionsHtml}</select></span>` +
        `</div></th>`;
}
function setImportTypeFilter(value) { importTypeFilter = value || ''; displayImportFiles(); }
function _importTypeMatches(value) { return !importTypeFilter || value === importTypeFilter; }

// Album: pas de tri associé (aucune colonne "album" dans _importFileSortValue, le tri par
// défaut regroupe déjà par série devinée - voir fileGroupTitle/_assignStableOrder), juste
// un filtre texte comme Nom.
function _importAlbumFilterHeaderHtml() {
    return `<th><div class="th-filterable-row">` +
        `<span class="th-filterable-label">Album</span>` +
        `<span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>` +
        `<input type="text" class="series-table-filter-input import-album-filter-input th-filterable-control" value="${escapeHtml(importAlbumFilter)}" placeholder="Filtrer..." aria-label="Filtrer par album" oninput="_syncFilterControlActive(this); setImportAlbumFilter(this.value)"></span>` +
        `</div></th>`;
}
function setImportAlbumFilter(value) {
    const input = document.activeElement;
    const cursor = input && input.selectionStart != null ? input.selectionStart : String(value || '').length;
    importAlbumFilter = value || '';
    displayImportFiles();
    requestAnimationFrame(() => {
        const next = document.querySelector('.import-files-table .import-album-filter-input');
        if (next) { next.focus(); next.setSelectionRange(cursor, cursor); }
    });
}
function _importAlbumMatches(value) { const q = String(importAlbumFilter || '').trim().toLocaleLowerCase(); return !q || String(value || '').toLocaleLowerCase().includes(q); }
// Série devinée/assignée pour Album (voir albumHtml côté rendu de ligne, même logique):
// le destination.series_title déjà choisi prime, sinon la même série devinée depuis le
// nom de fichier/dossier qu'utilise le regroupement/tri par défaut.
function _importFileAlbumText(file) { return (file.destination && file.destination.series_title) || fileGroupTitle(file); }

function _importVolumeFilterHeaderHtml(availableVolumeLabels) {
    const active = importTableSort.column === 'volume';
    const arrow = active ? (importTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    const labels = new Set(availableVolumeLabels);
    if (importVolumeFilter) labels.add(importVolumeFilter);
    const optionsHtml = [...labels]
        .sort((a, b) => a.localeCompare(b, 'fr', { numeric: true, sensitivity: 'base' }))
        .map(label => `<option value="${escapeHtml(label)}"${importVolumeFilter === label ? ' selected' : ''}>${escapeHtml(label)}</option>`)
        .join('');
    return `<th${active ? ' style="color:var(--color-accent); font-weight:600;"' : ''}><div class="th-filterable-row">` +
        `<span class="th-filterable-label" style="cursor:pointer; user-select:none;" onclick="setImportTableSort('volume')">Volume${arrow}</span>` +
        `<span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>` +
        `<select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par volume" onchange="_syncFilterControlActive(this); setImportVolumeFilter(this.value)"><option value="">Tous</option>${optionsHtml}</select></span>` +
        `</div></th>`;
}
function setImportVolumeFilter(value) { importVolumeFilter = value || ''; displayImportFiles(); }
function _importVolumeMatches(value) { return !importVolumeFilter || value === importVolumeFilter; }
// Même libellé que la cellule Volume affichée (_volumeCellHtml) pour un fichier détecté
// comme intégrale/HS/épisode - "Tome N" sinon, à partir du même repli parsed.volume/
// file.volume_number que _importFileSortValue('volume').
function _importFileVolumeText(file) {
    const parsed = file.parsed || {};
    const raw = parsed.volume != null ? parsed.volume : file.volume_number;
    if (raw != null && raw !== '') return `Tome ${raw}`;
    if (parsed.is_integral) return `Intégrale${parsed.integral_number != null ? ' ' + parsed.integral_number : ''}`;
    if (parsed.is_hs) return `Hors-série${parsed.hs_number != null ? ' ' + parsed.hs_number : ''}`;
    if (parsed.is_episode) return `Épisode${parsed.episode_number != null ? ' ' + parsed.episode_number : ''}`;
    return '';
}
function _importDateValue(value) {
    if (value == null || value === '') return '';
    const d = typeof value === 'number' ? new Date(value < 100000000000 ? value * 1000 : value) : parseDbUtcDate(value);
    return !d || Number.isNaN(d.getTime()) ? '' : d.toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short', timeZone: BULLARR_DISPLAY_TZ });
}
function _importDateForFile(file) {
    const id = file?.destination?.tracking_id;
    const pending = id != null ? pendingDownloads.find(p => Number(p.id) === Number(id)) : null;
    return _importDateValue(pending?.created_at || file?.mtime);
}
function _importDateForPending(pending) { return _importDateValue(pending?.created_at); }
function _importDateForActive(item) {
    const pending = item?.tracking_id != null ? pendingDownloads.find(p => Number(p.id) === Number(item.tracking_id)) : null;
    return _importDateValue(item?.created_at || pending?.created_at);
}

const CLIENT_LOGOS = {
    qbittorrent: '/static/img/qbittorrent-logo.svg',
    rtorrent: '/static/img/rtorrent-logo.svg',
    deluge: '/static/img/deluge-logo.svg',
    amule: '/static/img/emule-logo.svg',
    telegram: '/static/img/telegram-logo.svg',
    fourtoutici: '/static/img/fourtoutici-favicon.svg',
    shelfmark: '/static/img/annas-archive-favicon.ico'
};
const CLIENT_LABELS = {
    qbittorrent: 'qBittorrent', rtorrent: 'rTorrent', deluge: 'Deluge', amule: 'aMule',
    telegram: 'Telegram', fourtoutici: 'fourtoutici', shelfmark: 'Shelfmark',
    // '/torrents' est partagé entre qBittorrent/rTorrent/Deluge - un fichier déjà sur
    // disque ne peut pas être rattaché avec certitude à l'un des trois (voir client côté
    // serveur, scan_import_directory)
    torrent: 'Torrent'
};

// Icône seule, pas le nom du client ("pour le type met juste l'icone pas besoin du nom") -
// le nom reste consultable via le tooltip au survol.
// "dans import when it is manual process the torrent icon is strange. keep it as a
// torrent" - un fichier déjà sur disque dans le dossier '/torrents' PARTAGÉ (voir
// CLIENT_LABELS ci-dessus) tombait sur l'icône flèche générique svgIcon('download'), faute
// de logo de marque assignable à un client précis (qBittorrent/rTorrent/Deluge, tous les
// trois possibles) - une icône aimant/torrent générique (déjà définie dans icons.js,
// jusqu'ici inutilisée) est un repère bien plus clair que la flèche "téléchargement" (qui
// ne dit rien du TYPE de source) pour ce cas précis. EBDZ n'a pas
// besoin de sa propre entrée ici: un fichier EBDZ passe par eMule/aMule (client='amule',
// déjà son propre logo) - jamais un client 'ebdz' distinct à ce niveau.
function clientBadgeHtml(clientKey) {
    const label = CLIENT_LABELS[clientKey] || clientKey;
    const logo = CLIENT_LOGOS[clientKey];
    const iconHtml = logo
        ? `<img src="${logo}" alt="" class="torrent-client-logo">`
        : clientKey === 'torrent' ? svgIcon('magnet') : svgIcon('download');
    return `<span data-tooltip="${escapeHtml(label)}">${iconHtml}</span>`;
}

// Vocabulaire de statut par client - qBittorrent/Deluge/aMule renvoient des clés/mots
// bruts (souvent en anglais), rTorrent traduit déjà côté serveur (activity/routes.py)
const CLIENT_STATE_LABELS = {
    downloading: 'Téléchargement', stalledDL: 'En attente de pairs', pausedDL: 'En pause',
    queuedDL: 'En file d’attente', checkingDL: 'Vérification', metaDL: 'Récupération métadonnées',
    error: 'Erreur', missingFiles: 'Fichiers manquants',
    Downloading: 'Téléchargement', Paused: 'En pause', Queued: 'En file d’attente',
    Checking: 'Vérification', Error: 'Erreur', Allocating: 'Allocation',
    Waiting: 'En attente', Hashing: 'Vérification'
};

let activeDownloads = [];

let selectedPendingIds = new Set();
let selectedActiveKeys = new Set();
let selectedIncompatibleFolderKeys = new Set();

function _incompatibleFolderKey(folder) {
    return `${folder.import_root}::${folder.relative_path}`;
}

let expandedPackIds = new Set();

let expandedPackSubfolders = new Set();

function togglePackSubfolder(key) {
    if (expandedPackSubfolders.has(key)) expandedPackSubfolders.delete(key);
    else expandedPackSubfolders.add(key);
    displayImportFiles();
}

function toggleSubfolderSelection(pendingId, subfolder, checked) {
    const group = _pendingPackGroups().find(g => g.pending.id === pendingId);
    if (!group) return;
    const bySubfolder = _groupPackMembersBySubfolder(group.fileMatches, group.folderMatches);
    const target = bySubfolder.find(([sf]) => sf === subfolder);
    if (!target) return;
    target[1].files.forEach(({ file }) => { if (file.destination) file.selected = checked; });
    displayImportFiles();
}

// "when adding a new file whatever source. it should be automatically added to import.
// then you can poll to get its status" - distinct de activeDownloads (progression réelle
// sondée en direct chez un client): pendingDownloads vient de active_downloads côté Flask
// (voir mark_download_pending), posé dès qu'un bouton "Ajouter/Télécharger" réussit, avant
// même qu'un sondage client ou un scan de fichiers n'ait eu l'occasion de le découvrir -
// couvre en particulier Telegram, qui n'a par ailleurs aucune ligne dans activeDownloads
// (pas de client à sonder, juste un thread qui tourne).
let pendingDownloads = [];

let anyImportInProgress = false;

let currentlyProcessingFile = null;

async function loadActiveDownloads() {
    try {
        // Le serveur renvoie un instantané unique contenant la progression du client,
        // active_downloads et les fichiers suivis. Ne pas combiner dans le navigateur
        // des réponses obtenues à des instants différents.
        const response = await _fetchWithTimeout('/api/import/state', {}, 30000);
        const data = await response.json();
        dismissToast('activity-status-timeout');
        activeDownloads = [];
        pendingDownloads = [];
        if (data.success) {
            for (const client of data.clients) {
                for (const item of client.items) {
                    activeDownloads.push({ clientKey: client.client, item });
                }
            }
            pendingDownloads = data.pending || [];

            const oldByPath = new Map(importFiles.map(file => [file.filepath, file]));
            importFiles = (data.files || []).map(file => {
                const old = oldByPath.get(file.filepath);
                if (!old) return file;
                return Object.assign(file, {
                    selected: old.selected,
                    _bulkSelected: old._bulkSelected,
                    // Préserver une association en cours de modification uniquement si
                    // le serveur n'a pas fourni une destination plus récente pour ce chemin.
                    destination: file.destination || old.destination,
                    manual_override: file.manual_override || old.manual_override,
                });
            });
            incompatibleFolders = data.incompatible_folders || [];
            currentlyProcessingFile = data.currently_processing || null;
            hasScannedOnce = true;
        }
    } catch (error) {
        if (error.name === 'AbortError') {
            showToast('activity-status-timeout', "Statut des téléchargements lent à répondre - dernières données connues affichées", { icon: 'triangle-alert', autoHideMs: 6000 });
        } else {
            activeDownloads = [];
            pendingDownloads = [];
            currentlyProcessingFile = null;
        }
    }
    hasCheckedActiveDownloadsOnce = true;
    renderCurrentlyProcessingBanner();
    displayImportFiles();
}

function renderCurrentlyProcessingBanner() {
    const container = document.getElementById('currently-processing-banner');
    if (!container) return;
    if (!currentlyProcessingFile) {
        container.innerHTML = '';
        container.style.display = 'none';
        return;
    }
    const elapsed = currentlyProcessingFile.elapsed_seconds ?? 0;
    const isStuck = elapsed > 300;
    const color = isStuck ? '#dc3545' : '#e67e22';
    const label = isStuck
        ? `⚠️ Bloqué depuis ${elapsed}s (au-delà du délai normal) : `
        : `🔄 En cours de traitement (${elapsed}s) : `;
    container.style.display = 'block';
    container.innerHTML = `
        <div style="padding:8px 12px; margin-bottom:10px; border-radius:6px; background:${isStuck ? '#f8d7da' : '#fff3cd'}; border:1px solid ${color}; color:${color}; font-weight:600;">
            ${svgIcon('loader-circle', 'icon-spin')} ${label}${escapeHtml(currentlyProcessingFile.filename)}
        </div>
    `;
}

function _pendingVolumeLabel(pending) {
    if (pending.volume_number && pending.volume_number !== 'null') return `Tome ${escapeHtml(String(pending.volume_number))}`;
    if (pending.is_integral) return `Intégrale${pending.integral_number != null ? ' ' + pending.integral_number : ''}`;
    if (pending.is_hs) return `Hors-série${pending.hs_number != null ? ' ' + pending.hs_number : ''}`;
    if (pending.is_episode) return `Épisode${pending.episode_number != null ? ' ' + pending.episode_number : ''}`;
    return '—';
}

// Factorisé pour être réutilisé par refreshDownloadingProgress (mise à jour EN PLACE
// toutes les 5s d'un téléchargement Telegram encore actif, sans reconstruire toute la
// ligne) - même contenu affiché, un seul endroit à faire évoluer. Voir l'équivalent côté
// activeDownloads: _activeDownloadProgressLabelHtml.
function _pendingProgressLabelHtml(pending) {
    const downloaded = pending.bytes_downloaded || 0;
    const progressPct = Math.min(100, Math.max(0, downloaded * 100 / pending.bytes_total));
    return `${progressPct.toFixed(0)}% · ${formatBytes(downloaded)} / ${formatBytes(pending.bytes_total)}`;
}

function _pendingDownloadRowHtml(pending) {
    const hasProgress = !!pending.bytes_total;
    const progressPct = hasProgress ? Math.min(100, Math.max(0, (pending.bytes_downloaded || 0) * 100 / pending.bytes_total)) : 0;
    const statusHtml = pending.status === 'importing'
            ? `<div style="font-size:0.85em;">${svgIcon('loader-circle', 'icon-spin')} Import en cours...</div>`
            // "completed currently appears as Prêt even when not actually importable
            // automatically" - needs_volume_correction (get_pending_downloads,
            // downloader.py) distingue un fichier réellement arrivé ET dont le tome est
            // identifié d'un fichier arrivé mais dont le numéro n'a pu être ni suivi ni
            // reparsé depuis le titre - "✓ Prêt" mentait sur ce second cas, l'import
            // automatique ne pouvant de toute façon jamais s'en charger seul. Le crayon
            // (openTrackingEditModal, déjà existant) reste le seul moyen de le corriger.
            : pending.status === 'completed' && pending.needs_volume_correction
            ? `<span class="import-download-error" data-tooltip="Le numéro de tome n'a pas pu être identifié - correction manuelle nécessaire (crayon)">${svgIcon('alert-triangle')} Tome à préciser</span>`
            : pending.status === 'completed' && pending.needs_series_correction
            ? `<span class="import-download-error" data-tooltip="Aucune série identifiée pour ce téléchargement - le scan automatique ne peut pas le reprendre, rattachement manuel nécessaire (crayon)">${svgIcon('alert-triangle')} Série à préciser</span>`
            : pending.status === 'completed'
            ? `<span style="color:#28a745; font-weight:600;" data-tooltip="Téléchargement terminé - repris automatiquement au prochain scan (quelques secondes), ou cliquez sur ✓ pour ne pas attendre">${svgIcon('check')} Prêt — scan automatique sous peu</span>`
            : pending.exhausted
                ? `<span class="import-download-error">${svgIcon('alert-triangle')} Échec — relance manuelle</span>`
            : hasProgress
                ? `
                    <div style="height:6px; background:var(--color-surface-alt); border:1px solid var(--color-border); border-radius:3px; overflow:hidden;">
                        <div class="pending-download-progress-fill" style="height:100%; width:${progressPct}%; background:var(--color-accent);"></div>
                    </div>
                    <div class="pending-download-progress-label" style="font-size:0.8em; margin-top:2px;">${_pendingProgressLabelHtml(pending)}</div>
                `
                : pending.client === 'telegram'
                    ? `<div style="font-size:0.85em;">${svgIcon('loader-circle', 'icon-spin')} En attente de téléchargement...</div>`
                    : `<div style="font-size:0.85em;">${svgIcon('loader-circle', 'icon-spin')} En attente...</div>`;
    return `
        <tr${hasProgress ? ` data-pending-key="${pending.id}"` : ''}>
            <td><input type="checkbox" class="import-file-select" ${selectedPendingIds.has(pending.id) ? 'checked' : ''} onchange="togglePendingRowSelection(${pending.id}, this.checked)" data-tooltip="Sélectionner pour supprimer en masse"></td>
            <td class="import-date-cell">${escapeHtml(_importDateForPending(pending) || '—')}</td>
            <td>${clientBadgeHtml(pending.client)}</td>
            <td>
                <div style="font-weight:600;" data-tooltip="${escapeHtml(pending.title)}">${escapeHtml(pending.title)}</div>
            </td>
            <td>${pending.series_title ? (pending.series_id ? `<a href="/series/${pending.series_id}" class="import-series-link" title="Voir la fiche de cette série">${escapeHtml(pending.series_title)}</a>` : escapeHtml(pending.series_title)) : '—'}</td>
            <td>${_pendingVolumeLabel(pending)}</td>
            <td style="text-align:center; text-transform:uppercase; color:var(--color-text-muted); font-size:0.85em;">${escapeHtml(_importFileExtension({ filename: pending.title }))}</td>
            <td style="text-align:center; min-width:110px;">
                ${statusHtml}
                ${pending.status === 'completed' ? `<button class="btn-icon-only" onclick="importPendingDownload(${pending.id}, this)" data-tooltip="Importer ce fichier maintenant">${svgIcon('check')}</button>` : ''}
                ${pending.exhausted ? `<button class="btn-icon-only" onclick="retryTelegramDownload(${pending.id}, this)" data-tooltip="Relancer manuellement (budget de tentatives automatiques épuisé)">${svgIcon('refresh-cw')}</button>` : ''}
                <button class="btn-icon-only" onclick="openTrackingEditModal(${pending.id}, ${pending.series_id ?? 'null'}, ${pending.volume_number ?? 'null'}, '${escapeForAttribute(pending.title)}')" data-tooltip="Corriger la série/le tome suivis pour ce téléchargement">${svgIcon('pencil')}</button>
                <button class="btn-icon-only" onclick="removePendingDownload(${pending.id}, this)" data-tooltip="Retirer et annuler le téléchargement chez le client (si retrouvé)">${svgIcon('trash-2')}</button>
            </td>
        </tr>
    `;
}

function togglePendingRowSelection(id, checked) {
    if (checked) selectedPendingIds.add(id); else selectedPendingIds.delete(id);
    const group = _pendingPackGroups().find(g => g.pending.id === id);
    if (group) {
        group.fileMatches.forEach(({ file }) => { file.selected = checked; });
        displayImportFiles();
        return;
    }
    updateImportStats();
}

function toggleIncompatibleFolderSelection(folderKey, checked) {
    if (checked) selectedIncompatibleFolderKeys.add(folderKey); else selectedIncompatibleFolderKeys.delete(folderKey);
    updateImportStats();
}

// Un client par clé (voir CLIENT_LABELS) - endpoint dédié par client puisque chacun a son
// propre protocole de suppression (REST/XML-RPC/JSON-RPC/CLI, voir les routes /remove
// correspondantes) ; Telegram n'a pas d'entrée ici, un téléchargement Telegram n'a pas de
// vraie progression interrogeable (juste présent/absent, voir pendingDownloads/
// _pendingDownloadRowHtml plus haut - pas un item de activeDownloads, donc jamais
// concerné par la suppression qui suppose un id de client réel).
const CLIENT_REMOVE_ENDPOINTS = {
    qbittorrent: '/api/qbittorrent/remove',
    rtorrent: '/api/rtorrent/remove',
    deluge: '/api/deluge/remove',
    amule: '/api/emule/remove'
};

async function deleteActiveDownload(clientKey, id, trackingId, button) {
    const endpoint = CLIENT_REMOVE_ENDPOINTS[clientKey];
    if (!endpoint || !id) return;
    if (!confirm('Supprimer ce téléchargement en cours (et ses données déjà téléchargées) ?')) return;

    if (button) button.disabled = true;
    try {
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            if (button) button.disabled = false;
            return;
        }
        if (trackingId != null) {
            await fetch('/api/activity/pending/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: trackingId })
            }).catch(() => {});
        }
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        if (button) button.disabled = false;
    }
}

async function removePendingDownload(downloadId, button) {
    if (!confirm('Retirer cette ligne et annuler le téléchargement chez le client (fichiers partiels supprimés) ?')) return;
    if (button) button.disabled = true;

    try {
        const response = await fetch('/api/activity/pending/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: downloadId })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            if (button) button.disabled = false;
            return;
        }
        if (!data.cancelled_at_client) {
            showToast('pending-remove-' + downloadId, 'Ligne retirée - aucun téléchargement correspondant retrouvé chez le client (probablement déjà terminé/annulé)', { icon: 'info', autoHideMs: 6000 });
        }
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        if (button) button.disabled = false;
    }
}

// "instead of waiting to import there should be a pret icon [...] and i cannot click
// import in the bottom part to import it" - le bouton "Importer" du pied de tableau
// n'opère que sur importFiles (voir executeImport), jamais sur pendingDownloads (juste
// une ligne de suivi active_downloads, sans filepath/destination complets) : une ligne
// 'completed' (voir pending.status, get_pending_downloads côté downloader.py) n'avait
// donc aucun moyen d'être réellement importée depuis /import, malgré le "✓ Prêt" affiché.
// Un seul scan CIBLÉ ici, déclenché par un clic explicite (pas un sondage automatique -
// voir la suppression de knownTrackedDownloadIds) pour retrouver le fichier réel
// correspondant (destination.tracking_id, voir scan_import_directory/
// find_active_download_destination, posé exactement pour ce cas), puis import immédiat de
// CE seul fichier via la même route que l'import normal (executeImport/POST
// /api/import/execute).
async function importPendingDownload(downloadId, button) {
    if (button) button.disabled = true;
    try {
        const scanResponse = await fetch('/api/import/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const scanData = await scanResponse.json();
        if (!scanData.success) {
            alert('❌ Erreur: ' + (scanData.error || 'Erreur inconnue'));
            return;
        }
        const file = (scanData.files || []).find(f => f.destination && f.destination.tracking_id === downloadId);
        if (!file || !file.destination) {
            alert("⚠️ Fichier introuvable sur le disque pour ce téléchargement - il a peut-être déjà été importé, déplacé ou supprimé. Actualisez la page pour mettre à jour l'affichage.");
            return;
        }
        if (!confirm(`Importer "${file.filename}" maintenant ?`)) return;

        const importResponse = await fetch('/api/import/execute', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files: [file] })
        });
        const importData = await importResponse.json();
        if (!importData.success || importData.failed_count > 0) {
            const errorDetail = (importData.failures && importData.failures[0] && importData.failures[0].error) || importData.error || 'Erreur inconnue';
            alert('❌ Erreur: ' + errorDetail);
            return;
        }

        showToast('komga-scan', 'Import terminé - scan Komga demandé', { icon: 'radio', autoHideMs: 4000 });
        await loadAllLibraries();
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    } finally {
        if (button) button.disabled = false;
    }
}

async function _deleteImportFileRequest(file) {
    const response = await fetch('/api/import/file', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            import_root: file.import_root,
            relative_path: file.relative_path,
            filename: file.filename,
            client: file.client
        })
    });
    return response.json();
}

async function _deleteIncompatibleFolderRequest(importRoot, relativePath) {
    const response = await fetch('/api/import/incompatible-folder', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ import_root: importRoot, relative_path: relativePath })
    });
    return response.json();
}

async function removePendingPack(downloadId, fileIndices, folderIndices, button) {
    const totalItems = fileIndices.length + folderIndices.length;
    // Figés par RÉFÉRENCE ici, avant le premier await - un scan de fond
    // (refreshImportFilesQuietly, sur un minuteur indépendant) peut reconstruire
    // importFiles/incompatibleFolders pendant que cette fonction attend une réponse
    // réseau entre deux suppressions ; ré-indexer par fileIndices[i]/importFiles[index] à
    // ce moment-là supprimerait potentiellement un AUTRE fichier que celui confirmé par
    // l'utilisateur (les entrées survivantes gardent leur identité d'objet à travers un
    // merge, voir Object.assign(f, ...) dans refreshImportFilesQuietly, donc retrouver ces
    // mêmes objets par référence plus bas reste fiable même après un merge entre-temps).
    const filesToDelete = fileIndices.map(i => importFiles[i]).filter(Boolean);
    const foldersToDelete = folderIndices.map(i => incompatibleFolders[i]).filter(Boolean);
    const namesToDelete = [
        ...filesToDelete.map(f => f.filename),
        ...foldersToDelete.map(f => f.folder_name),
    ];
    if (!confirm(`Supprimer ce pack ET ${totalItems} ${pluralize(totalItems, 'fichier')}/${pluralize(totalItems, 'dossier')} déjà arrivé${pluralize(totalItems, '', 's')} sur disque ? Cette action est irréversible.\n\n${namesToDelete.join('\n')}`)) return;
    if (button) button.disabled = true;

    try {
        const response = await fetch('/api/activity/pending/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: downloadId })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            if (button) button.disabled = false;
            return;
        }

        const failures = [];
        for (const file of filesToDelete) {
            try {
                const fdata = await _deleteImportFileRequest(file);
                if (fdata.success) {
                    const idx = importFiles.indexOf(file);
                    if (idx !== -1) importFiles.splice(idx, 1);
                } else failures.push(file.filename);
            } catch (e) {
                failures.push(file.filename);
            }
        }
        for (const folder of foldersToDelete) {
            try {
                const fdata = await _deleteIncompatibleFolderRequest(folder.import_root, folder.relative_path);
                if (fdata.success) {
                    // incompatibleFolders est REMPLACÉ en bloc (pas fusionné par référence
                    // comme importFiles) par refreshImportFilesQuietly - indexOf peut donc
                    // ne plus retrouver cet objet précis après un scan de fond entre-temps ;
                    // rien de grave, un scan/rafraîchissement suivant réaligne l'affichage.
                    const idx = incompatibleFolders.indexOf(folder);
                    if (idx !== -1) incompatibleFolders.splice(idx, 1);
                } else failures.push(folder.relative_path);
            } catch (e) {
                failures.push(folder.relative_path);
            }
        }

        if (failures.length > 0) {
            alert(`⚠️ Pack retiré, mais ${failures.length} ${pluralize(failures.length, 'élément')} n'${pluralize(failures.length, "a", "ont")} pas pu être ${pluralize(failures.length, 'supprimé')}:\n${failures.join('\n')}`);
        }

        updateImportStats();
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        if (button) button.disabled = false;
    }
}

// "3-retry budget ? do something if this exceed the amount. like manual retry in
// import" - une ligne exhausted (voir get_pending_downloads/downloader.py) ne sera plus
// jamais retentée automatiquement, ce bouton est son seul recours. Repart avec un budget
// de tentatives neuf côté serveur (voir retry_telegram_download, telegram_channels/
// routes.py).
async function retryTelegramDownload(downloadId, button) {
    if (button) button.disabled = true;

    try {
        const response = await fetch(`/api/telegram-channels/retry/${downloadId}`, { method: 'POST' });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            if (button) button.disabled = false;
            return;
        }
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        if (button) button.disabled = false;
    }
}

function toggleActiveRowSelection(clientKey, id, checked) {
    const key = `${clientKey}:${id}`;
    if (checked) selectedActiveKeys.add(key); else selectedActiveKeys.delete(key);
    updateImportStats();
}

// Factorisé hors de _activeDownloadRowHtml pour être réutilisé par
// refreshDownloadingProgress (mise à jour EN PLACE toutes les 5s, sans reconstruire toute
// la ligne) - même contenu affiché, un seul endroit à faire évoluer.
function _activeDownloadSizeText(item) {
    return item.size_bytes
        ? `${formatBytes(item.downloaded_bytes || 0)} / ${formatBytes(item.size_bytes)}`
        : (item.downloaded_bytes ? formatBytes(item.downloaded_bytes) : null);
}

function _activeDownloadSizeLineHtml(item) {
    const sizeText = _activeDownloadSizeText(item);
    return sizeText || item.speed_bytes_s
        ? `${sizeText || ''}${item.speed_bytes_s ? `${sizeText ? ' · ' : ''}${formatBytes(item.speed_bytes_s)}/s` : ''}`
        : '';
}

function _activeDownloadProgressLabelHtml(item) {
    const stateLabel = CLIENT_STATE_LABELS[item.state] || item.state || '—';
    const progress = Math.min(100, Math.max(0, item.progress || 0));
    const etaText = formatEta(item.eta_seconds);
    return `${progress.toFixed(1)}% · ${escapeHtml(stateLabel)}${etaText ? ` · ${etaText}` : ''}`;
}

function _activeDownloadKey(clientKey, item) {
    return item.id != null ? `${clientKey}:${item.id}` : null;
}

function _activeDownloadRowHtml({ clientKey, item }) {
    const progress = Math.min(100, Math.max(0, item.progress || 0));
    const deleteButtonHtml = item.id
        ? `<button class="btn-icon-only" onclick="deleteActiveDownload('${escapeForAttribute(clientKey)}', '${escapeForAttribute(String(item.id))}', ${item.tracking_id ?? 'null'}, this)" data-tooltip="Supprimer ce téléchargement">${svgIcon('trash-2')}</button>`
        : '';
    const editButtonHtml = item.tracking_id != null
        ? `<button class="btn-icon-only" onclick="openTrackingEditModal(${item.tracking_id}, ${item.series_id ?? 'null'}, ${item.volume_number ?? 'null'}, '${escapeForAttribute(item.name)}')" data-tooltip="Corriger la série/le tome suivis pour ce téléchargement">${svgIcon('pencil')}</button>`
        : '';
    const activeKey = _activeDownloadKey(clientKey, item);
    const selectCheckboxHtml = activeKey
        ? `<input type="checkbox" class="import-file-select" ${selectedActiveKeys.has(activeKey) ? 'checked' : ''} onchange="toggleActiveRowSelection('${escapeForAttribute(clientKey)}', '${escapeForAttribute(String(item.id))}', this.checked)" data-tooltip="Sélectionner pour supprimer en masse">`
        : '';
    // data-active-key: "make the update for the download a 5s but only for the
    // downloading files" - identifiant stable retrouvé par refreshDownloadingProgress
    // pour mettre à jour SEULEMENT la barre/le texte de progression de cette ligne
    // précise, sans reconstruire toute la ligne ni le tableau (voir son commentaire).
    return `
        <tr${activeKey ? ` data-active-key="${escapeHtml(activeKey)}"` : ''}>
            <td>${selectCheckboxHtml}</td>
            <td class="import-date-cell">${escapeHtml(_importDateForActive(item) || '—')}</td>
            <td>${clientBadgeHtml(clientKey)}</td>
            <td>
                <div style="font-weight:600;" data-tooltip="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
                <div class="active-download-size-line" style="font-size:0.9em;">${_activeDownloadSizeLineHtml(item)}</div>
            </td>
            <td>${item.series_title ? (item.series_id ? `<a href="/series/${item.series_id}" class="import-series-link" title="Voir la fiche de cette série">${escapeHtml(item.series_title)}</a>` : escapeHtml(item.series_title)) : '—'}</td>
            <td>${item.volume_number ? `Tome ${escapeHtml(String(item.volume_number))}` : '—'}</td>
            <td style="text-align:center; text-transform:uppercase; color:var(--color-text-muted); font-size:0.85em;">${escapeHtml(_importFileExtension({ filename: item.name }))}</td>
            <td style="text-align:center; min-width:110px;">
                <div style="height:6px; background:var(--color-surface-alt); border:1px solid var(--color-border); border-radius:3px; overflow:hidden;">
                    <div class="active-download-progress-fill" style="height:100%; width:${progress}%; background:var(--color-accent);"></div>
                </div>
                <div class="active-download-progress-label" style="font-size:0.8em; margin-top:2px;">${_activeDownloadProgressLabelHtml(item)}</div>
                ${editButtonHtml}
                ${deleteButtonHtml}
            </td>
        </tr>
    `;
}

let trackingEditId = null;

function openTrackingEditModal(trackingId, seriesId, volumeNumber, filename) {
    trackingEditId = trackingId;
    document.getElementById('tracking-edit-file-name').textContent = `Fichier: ${filename}`;
    _populateTrackingEditModal(seriesId, volumeNumber);
    document.getElementById('tracking-edit-modal').classList.add('active');
    _refreshLibrariesInBackground(
        'tracking-edit-modal', 'tracking-edit-library', 'tracking-edit-series',
        () => _populateTrackingEditModal(seriesId, volumeNumber)
    );
}

function _populateTrackingEditModal(seriesId, volumeNumber) {
    const librarySelect = document.getElementById('tracking-edit-library');
    librarySelect.innerHTML = '<option value="">-- Sélectionner une bibliothèque --</option>' +
        allLibraries.map(lib => `<option value="${lib.id}">${escapeHtml(lib.name)}</option>`).join('');
    document.getElementById('tracking-edit-volume').value = volumeNumber != null ? volumeNumber : '';
    document.getElementById('tracking-edit-type').value = 'volume';
    _onTrackingEditTypeChange();

    document.getElementById('tracking-edit-library-group').style.display = allLibraries.length === 1 ? 'none' : '';

    // Pré-sélectionne la bibliothèque/série déjà suivies (si connues) - retrouve la
    // bibliothèque via librariesSeriesMap plutôt qu'un appel réseau, déjà chargé en
    // mémoire au chargement de la page (voir loadAllLibraries).
    if (seriesId != null) {
        const libraryId = Object.keys(librariesSeriesMap).find(
            libId => (librariesSeriesMap[libId] || []).some(s => s.id === seriesId)
        ) || (allLibraries.length === 1 ? String(allLibraries[0].id) : null);
        if (libraryId) {
            librarySelect.value = libraryId;
            _loadTrackingEditSeries();
            document.getElementById('tracking-edit-series').value = String(seriesId);
            syncSeriesComboboxDisplay('tracking-edit-series-search', 'tracking-edit-series');
            _loadTrackingEditVolumes();
        }
    } else if (allLibraries.length === 1) {
        librarySelect.value = allLibraries[0].id;
        _loadTrackingEditSeries();
    }
}

function closeTrackingEditModal() {
    document.getElementById('tracking-edit-modal').classList.remove('active');
    document.getElementById('tracking-edit-library').value = '';
    document.getElementById('tracking-edit-series').innerHTML = '<option value="">-- Sélectionner une série --</option>';
    document.getElementById('tracking-edit-series-search').value = '';
    _closeSeriesCombobox();
    document.getElementById('tracking-edit-new-series-name-group').style.display = 'none';
    document.getElementById('tracking-edit-new-series-name').value = '';
    document.getElementById('tracking-edit-volume-select').innerHTML = '<option value="">-- Tomes connus de la série --</option>';
    document.getElementById('tracking-edit-type').value = 'volume';
    document.getElementById('tracking-edit-volume').value = '';
    document.getElementById('tracking-edit-volume-group').style.display = '';
    trackingEditId = null;
}

function _loadTrackingEditSeries() {
    const libraryId = document.getElementById('tracking-edit-library').value;
    const seriesSelect = document.getElementById('tracking-edit-series');
    const series = librariesSeriesMap[libraryId] || [];
    // "toujours pas creer une nouvelle série. est-ce que c'est 2 implementations
    // différentes?" - même option "__new__" que #select-destination-modal (voir
    // loadLibrarySeries), un téléchargement encore en cours peut très bien viser une
    // série qui n'existe pas encore.
    seriesSelect.innerHTML = '<option value="">-- Sélectionner une série --</option>' +
        '<option value="__new__">+ Créer une nouvelle série</option>' +
        series.map(s => `<option value="${s.id}">${escapeHtml(s.title)}</option>`).join('');
    document.getElementById('tracking-edit-volume-select').innerHTML = '<option value="">-- Tomes connus de la série --</option>';
    document.getElementById('tracking-edit-new-series-name-group').style.display = 'none';
    document.getElementById('tracking-edit-new-series-name').value = '';
    // seriesSelect.innerHTML vient d'être reconstruit (donc remis à '' - "Sélectionner
    // une série") - garde le champ texte visible du combobox (voir openSeriesCombobox)
    // aligné dessus, même raisonnement que loadLibrarySeries côté select-destination-modal.
    syncSeriesComboboxDisplay('tracking-edit-series-search', 'tracking-edit-series');
}

// Bascule l'affichage du champ "Nom de la nouvelle série" (voir _loadTrackingEditSeries)
// et charge les tomes connus sinon - même rôle que le onchange programmatique de
// #destination-series (loadLibrarySeries côté select-destination-modal), mais câblé en
// dur ici puisque #tracking-edit-series n'a pas besoin du reste de cette logique
// (pas de "nouvelle série de ce lot", pas de tome override).
function _onTrackingEditSeriesChange() {
    const value = document.getElementById('tracking-edit-series').value;
    const newSeriesGroup = document.getElementById('tracking-edit-new-series-name-group');
    if (value === '__new__') {
        newSeriesGroup.style.display = 'block';
        document.getElementById('tracking-edit-volume-select').innerHTML = '<option value="">-- Tomes connus de la série --</option>';
    } else {
        newSeriesGroup.style.display = 'none';
        _loadTrackingEditVolumes();
    }
}

// Choisir un tome connu ne fait que pré-remplir le champ numéro libre ci-dessous, qui
// reste la seule valeur réellement envoyée par saveTrackingEdit - garde le champ éditable
// pour un tome pas encore listé (ex: hors-série tout juste annoncé). N'existe que pour
// des tomes "classiques" (voir _loadTrackingEditVolumes) - sélectionner un tome connu
// remet donc toujours le type sur "volume".
function _applyTrackingEditVolumeSelection() {
    const select = document.getElementById('tracking-edit-volume-select');
    if (select.value !== '') {
        document.getElementById('tracking-edit-volume').value = select.value;
        document.getElementById('tracking-edit-type').value = 'volume';
    }
}

function _onTrackingEditTypeChange() {
    const type = document.getElementById('tracking-edit-type').value;
    document.getElementById('tracking-edit-volume-select').style.display = type === 'volume' ? '' : 'none';
}

async function _loadTrackingEditVolumes() {
    const seriesId = document.getElementById('tracking-edit-series').value;
    const select = document.getElementById('tracking-edit-volume-select');
    const group = document.getElementById('tracking-edit-volume-group');
    select.innerHTML = '<option value="">-- Tomes connus de la série --</option>';
    if (!seriesId) {
        group.style.display = '';
        return;
    }

    const allSeries = Object.values(librariesSeriesMap).flat();
    const series = allSeries.find(s => s.id === parseInt(seriesId, 10));
    if (series && series.is_oneshot) {
        group.style.display = 'none';
        document.getElementById('tracking-edit-volume').value = '';
        return;
    }
    group.style.display = '';

    try {
        const response = await fetch(`/api/series/${seriesId}/volumes`);
        const volumes = await response.json();
        const plainVolumes = volumes.filter(v => v.volume_number != null && !v.is_integral && !v.is_hs && !v.is_episode);
        select.innerHTML = '<option value="">-- Tomes connus de la série --</option>' +
            plainVolumes.map(v => `<option value="${v.volume_number}">${escapeHtml(_bdVolumeOptionLabel(v))}</option>`).join('');
    } catch (error) {
        // Repli silencieux: le champ numéro libre reste utilisable même si la liste des
        // tomes n'a pas pu être chargée
    }
}

async function saveTrackingEdit() {
    const seriesValue = document.getElementById('tracking-edit-series').value;
    if (!trackingEditId || !seriesValue) {
        alert('⚠️ Veuillez sélectionner une série');
        return;
    }
    const volumeRaw = document.getElementById('tracking-edit-volume').value.trim();
    const volumeNumber = volumeRaw ? parseInt(volumeRaw) : null;
    const trackingType = document.getElementById('tracking-edit-type').value;
    const typePayload = trackingType === 'integral' ? { is_integral: true, integral_number: volumeNumber }
        : trackingType === 'hs' ? { is_hs: true, hs_number: volumeNumber }
        : trackingType === 'episode' ? { is_episode: true, episode_number: volumeNumber }
        : {};

    let seriesId;
    if (seriesValue === '__new__') {
        const newTitle = document.getElementById('tracking-edit-new-series-name').value.trim();
        const libraryId = document.getElementById('tracking-edit-library').value;
        if (!newTitle || !libraryId) {
            alert('⚠️ Veuillez indiquer le nom de la nouvelle série');
            return;
        }
        try {
            const createResponse = await fetch('/api/series/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    library_id: libraryId,
                    title: newTitle,
                    bedetheque_url: getBedethequeMatchForTitle(newTitle)
                })
            });
            const createData = await createResponse.json();
            if (!createData.success) {
                alert('❌ Erreur création série: ' + (createData.error || 'Erreur inconnue'));
                return;
            }
            seriesId = createData.series_id;
        } catch (error) {
            alert('❌ Erreur de connexion: ' + error.message);
            return;
        }
    } else {
        seriesId = parseInt(seriesValue, 10);
    }

    try {
        const response = await fetch('/api/activity/update-tracking', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                tracking_id: trackingEditId, series_id: seriesId,
                volume_number: trackingType === 'volume' ? volumeNumber : null,
                ...typePayload
            })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            return;
        }
        closeTrackingEditModal();
        if (seriesValue === '__new__') await loadAllLibraries();
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// ===== FONCTION UTILITAIRE POUR NORMALISER LES TITRES =====
// Uniquement pour la COMPARAISON (matching import <-> séries existantes), jamais pour
// l'affichage: sans retrait des accents/articles, "L'Adoption" (fichier importé, déjà mis
// dans l'ordre naturel par parse_filename côté serveur - voir scanner.py) ne matchait pas
// "Adoption" ou "l adoption" (variantes de casse/accentuation de la série déjà en
// bibliothèque), forçant une re-sélection manuelle à chaque import alors qu'une série
// correspondante existait déjà.
function normalizeTitle(title) {
    return title
        .toLowerCase()
        .normalize('NFD').replace(/[̀-ͯ]/g, '')  // Retirer les accents (é->e, à->a...)
        .replace(/[._-]/g, ' ')  // Remplacer points, underscores, tirets par espaces
        .replace(/^(le|la|les|l['’])\b\s*/, '')  // Retirer l'article de tête (apostrophe
                                                  // droite ou typographique, insensible à
                                                  // la casse via toLowerCase). \b est
                                                  // indispensable: sans lui "Légendaires"
                                                  // (-> "legendaires" après retrait des
                                                  // accents) matchait le préfixe "le" et
                                                  // devenait "gendaires"
        .replace(/\s+/g, ' ')    // Réduire espaces multiples
        .trim();
}

// Identité de groupe d'un fichier importé: le dossier qui le contient directement quand il
// y en a un (voir folder_name côté serveur - des fichiers rangés ensemble dans un même
// dossier appartiennent à la même série, peu importe leur nom de fichier), sinon le titre
// extrait de son nom de fichier. Utilisé partout où on doit reconnaître "les autres fichiers
// du même groupe qu'un fichier donné" (regroupement à l'affichage, assignation en masse,
// suppression de groupe, auto-match) - garder tous ces usages alignés sur le même critère.
function fileGroupTitle(file) {
    return file.folder_name || file.parsed.title || 'Sans titre';
}

// "when I have all the information the album will move to the bottom. keep it at its
// place" - le tri par défaut (voir displayImportFiles) regroupait par fileGroupTitle puis
// par NUMÉRO DE VOLUME recalculé à chaque rendu: corriger le tome d'un fichier (nouveau
// sélecteur, voir _volumeCellHtml) changeait donc sa position au sein de son groupe, en
// plein milieu d'une correction manuelle. Un ordre figé une seule fois, à l'arrivée du
// fichier (scan ou fusion silencieuse, voir refreshImportFilesQuietly), n'est plus jamais
// recalculé ensuite - une correction ne fait donc plus bouger la ligne. Un nouveau fichier
// pas encore vu obtient le numéro suivant (ajouté en fin de liste plutôt qu'inséré au
// milieu de son groupe), le tri live par colonne (clic sur un en-tête) reste, lui,
// entièrement dynamique et n'est pas concerné.
let _nextStableOrder = 0;

function _assignStableOrder(files) {
    const newOnes = files.filter(f => f._order == null);
    if (newOnes.length === 0) return;
    newOnes.sort((a, b) => {
        const groupCompare = fileGroupTitle(a).localeCompare(fileGroupTitle(b));
        if (groupCompare !== 0) return groupCompare;
        return (a.parsed.volume || 0) - (b.parsed.volume || 0);
    });
    newOnes.forEach(f => { f._order = _nextStableOrder++; });
}

async function loadAllLibraries() {
    try {
        const response = await fetch('/api/libraries');
        allLibraries = await response.json();
        
        // Charger les séries de toutes les bibliothèques en parallèle
        await Promise.all(allLibraries.map(async (lib) => {
            const seriesResponse = await fetch(`/api/library/${lib.id}/series`);
            librariesSeriesMap[lib.id] = await seriesResponse.json();
        }));
    } catch (error) {
        console.error('Erreur chargement bibliothèques:', error);
    }
}


async function scanImportDirectory() {
    const resultsSection = document.getElementById('scan-results');
    const container = document.getElementById('import-files-container');

    resultsSection.style.display = 'block';
    container.innerHTML = '<div class="loading"><div class="spinner"></div><p>Scan en cours...</p></div>';

    try {
        const response = await _fetchWithTimeout('/api/import/scan', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        }, 20000);

        const data = await response.json();
        hasScannedOnce = true;

        if (data.success) {
            importFiles = data.files;
            incompatibleFolders = data.incompatible_folders || [];
            await autoMatchAll();
        } else {
            container.innerHTML = `
                <div class="no-data">
                    <h3>Erreur</h3>
                    <p>${escapeHtml(String(data.error || 'Erreur inconnue'))}</p>
                </div>
            `;
        }
    } catch (error) {
        hasScannedOnce = true;
        const timedOut = error.name === 'AbortError';
        container.innerHTML = `
            <div class="no-data">
                <h3>${timedOut ? 'Le scan met trop de temps' : 'Erreur de connexion'}</h3>
                <p>${timedOut
                    ? "Probablement dû à un gros lot de téléchargements en cours de traitement côté serveur. Réessayez dans quelques instants."
                    : escapeHtml(String(error.message))}</p>
                <button class="btn btn-secondary" onclick="scanImportDirectory()">${svgIcon('refresh-cw')} Réessayer</button>
            </div>
        `;
    }
}

// Rafraîchit la liste de fichiers, SANS reconstruire importFiles depuis zéro comme
// scanImportDirectory - appelée uniquement sur clic explicite "Actualiser" (voir
// refreshImportsEnCours) depuis que le sondage automatique a été retiré ("i don't want
// to have a re render instead it is manually done"). Un rescan complet écraserait les
// destinations/tomes déjà assignés à la main par l'utilisateur - on fusionne donc juste
// les nouveaux fichiers (identifiés par filepath, unique par fichier sur disque) et on
// retire ceux qui ont disparu (déjà importés/supprimés ailleurs), sans toucher aux
// entrées déjà présentes. autoMatchAll() est sûr à rappeler ici: il saute déjà les
// fichiers avec une destination existante (`if (file.destination) continue`).
async function refreshImportFilesQuietly() {
    if (!hasScannedOnce) return; // Le tout premier scan n'a pas encore eu lieu, rien à fusionner
    // La modale de destination référence les fichiers en cours d'édition par INDEX brut
    // dans importFiles (currentFileIndices, voir openDestinationModal/assignDestination) -
    // le filter()+concat() ci-dessous retire les entrées disparues et décale donc les
    // index de tout ce qui suit. Si un fichier plus tôt dans la liste disparaît
    // (auto-importé par le scheduler entre-temps) PENDANT que l'utilisateur a la modale
    // ouverte sur un fichier plus loin, valider la modale assignerait la destination
    // choisie au mauvais fichier (celui qui a glissé dans l'ancien slot). On saute donc
    // cette fusion tant que la modale est ouverte plutôt que de risquer ça - le prochain
    // appel explicite (clic "Actualiser", modale fermée entre-temps) rattrape la fusion
    // normalement.
    if (currentFileIndices.length > 0) return;

    try {
        const response = await _fetchWithTimeout('/api/import/state', {}, 30000);
        const data = await response.json();
        if (!data.success) return;

        const stillPresentPaths = new Set(data.files.map(f => f.filepath));
        const existingPaths = new Set(importFiles.map(f => f.filepath));
        const newFiles = data.files.filter(f => !existingPaths.has(f.filepath));
        const anyDisappeared = importFiles.some(f => !stillPresentPaths.has(f.filepath));

        // "pourquoi toujours Import en cours" alors que le réglage a changé entre-temps -
        // un fichier déjà connu (déjà présent avant ce cycle) gardait tel quel son objet
        // ENTIER d'origine, jamais rafraîchi par un scan ultérieur : auto_import_skip_reason
        // (recalculé côté serveur à CHAQUE scan) restait donc figé sur sa toute première
        // valeur indéfiniment, même après avoir désactivé l'import automatique dans les
        // paramètres. Uniquement ces champs purement diagnostiques (jamais édités
        // localement, contrairement à `destination` qui peut être une assignation
        // manuelle en cours - voir plus haut pourquoi ce fichier n'est jamais totalement
        // réécrasé) sont donc réalignés sur la réponse fraîche à chaque cycle.
        const freshByPath = new Map(data.files.map(f => [f.filepath, f]));
        importFiles = importFiles
            .filter(f => stillPresentPaths.has(f.filepath))
            .map(f => {
                const fresh = freshByPath.get(f.filepath);
                return fresh
                    ? Object.assign(f, fresh, {
                        // Préserver une association locale en cours uniquement si le
                        // serveur n'a pas renvoyé une destination plus récente pour ce chemin.
                        destination: fresh.destination || f.destination
                    })
                    : f;
            })
            .concat(newFiles);
        incompatibleFolders = data.incompatible_folders || [];
        activeDownloads = [];
        for (const client of (data.clients || [])) {
            for (const item of (client.items || [])) activeDownloads.push({ clientKey: client.client, item });
        }
        pendingDownloads = data.pending || [];

        if (newFiles.length > 0) {
            await autoMatchAll();
        } else {
            updateImportStats();
            displayImportFiles();
        }
        if (anyDisappeared) loadImportHistorySection(true);
    } catch (error) {
        // Rafraîchissement silencieux en arrière-plan: une erreur ponctuelle ne doit pas
        // interrompre la page, le prochain passage réessaiera de lui-même.
    }
}

function updateImportStats() {
    const readyFiles = importFiles.filter(f => f.destination);
    const selectedCount = readyFiles.filter(_isFileSelected).length;
    const importBtn = document.getElementById('import-btn');
    const label = document.getElementById('assign-selected-count');
    // #import-btn n'existe que depuis le premier rendu de displayImportFiles() (bouton
    // rendu dans le pied du tableau, plus dans le HTML statique du template) - certains
    // appelants ("autoMatchAll" au tout premier scan) appellent updateImportStats() AVANT
    // le tout premier displayImportFiles(), donc l'élément n'existe pas encore. Sans ce
    // garde, "Cannot set properties of null (setting 'disabled')" cassait le tout premier
    // chargement de la page en permanence.
    if (importBtn) importBtn.disabled = selectedCount === 0;
    if (label) label.textContent = selectedCount > 0 ? `${selectedCount} fichier${selectedCount > 1 ? 's' : ''} sélectionné${selectedCount > 1 ? 's' : ''}` : '';

    const autoSelectable = readyFiles.filter(_hasKnownVolume);
    const autoSelectedCount = autoSelectable.filter(_isFileSelected).length;
    // _visiblePendingDownloads: une ligne "pending" masquée (voir son commentaire, fichier
    // déjà retrouvé comme vrai fichier importFiles) ne doit pas non plus compter dans
    // "tout sélectionner"/le total sélectionnable - elle n'a plus de case à cocher visible.
    const visiblePending = _visiblePendingDownloads();
    const pendingSelectedCount = visiblePending.filter(p => selectedPendingIds.has(p.id)).length;
    const selectableActiveDownloads = activeDownloads.filter(({ item }) => item.id != null);
    const activeSelectedCount = selectableActiveDownloads.filter(({ clientKey, item }) => selectedActiveKeys.has(`${clientKey}:${item.id}`)).length;
    const folderSelectedCount = incompatibleFolders.filter(f => selectedIncompatibleFolderKeys.has(_incompatibleFolderKey(f))).length;
    const totalSelectable = autoSelectable.length + visiblePending.length + selectableActiveDownloads.length + incompatibleFolders.length;
    const totalSelected = autoSelectedCount + pendingSelectedCount + activeSelectedCount + folderSelectedCount;
    const selectAll = document.getElementById('import-select-all');
    if (selectAll) {
        selectAll.checked = totalSelectable > 0 && totalSelected === totalSelectable;
        selectAll.indeterminate = totalSelected > 0 && totalSelected < totalSelectable;
    }

    const deleteBtn = document.getElementById('delete-selected-btn');
    if (deleteBtn) {
        const selectedCount = importFiles.filter(_isFileCheckedForDeletion).length + selectedPendingIds.size + selectedActiveKeys.size + selectedIncompatibleFolderKeys.size;
        deleteBtn.disabled = selectedCount === 0;
    }

    const convertBtn = document.getElementById('convert-selected-btn');
    if (convertBtn) {
        convertBtn.disabled = importFiles.filter(_isFileCheckedForConversion).length === 0;
    }
}

function _isFileCheckedForDeletion(file) {
    return file.destination ? _isFileSelected(file) : !!file._bulkSelected;
}


// Même pattern "sélection groupée séquentielle + un seul toast" que _runBulkVolumeAction
// (library.js)/le bloc Vérification (CLAUDE.md), étendu aux 4 types de lignes - chacun
// garde son propre endpoint de suppression déjà existant (deleteImportFile/
// removePendingDownload/deleteActiveDownload/deleteIncompatibleFolder) plutôt qu'un
// endpoint bulk séparé. "i want to delete with check everything but i can't select
// bonus and int" - dossiers incompatibles ajoutés comme 4e groupe.
async function bulkDeleteSelectedImportFiles() {
    const filesToDelete = importFiles.filter(_isFileCheckedForDeletion);
    const pendingToDelete = pendingDownloads.filter(p => selectedPendingIds.has(p.id));
    const activeToDelete = activeDownloads.filter(({ clientKey, item }) => item.id != null && selectedActiveKeys.has(`${clientKey}:${item.id}`));
    const foldersToDelete = incompatibleFolders.filter(f => selectedIncompatibleFolderKeys.has(_incompatibleFolderKey(f)));
    const total = filesToDelete.length + pendingToDelete.length + activeToDelete.length + foldersToDelete.length;
    if (total === 0) return;
    const namesToDelete = [
        ...filesToDelete.map(f => f.filename),
        ...pendingToDelete.map(p => p.title),
        ...activeToDelete.map(({ item }) => item.name),
        ...foldersToDelete.map(f => f.folder_name),
    ];
    if (!confirm(`Supprimer définitivement ${total} ${pluralize(total, 'élément')} ? Cette action est irréversible.\n\n${namesToDelete.join('\n')}`)) {
        return;
    }

    const toastId = 'bulk-delete-import-files';
    const failedFiles = [];
    const failureMessages = [];
    let done = 0;

    for (const file of filesToDelete) {
        done++;
        showToast(toastId, `Suppression... (${done}/${total})`);
        try {
            const response = await fetch('/api/import/file', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    import_root: file.import_root,
                    relative_path: file.relative_path,
                    filename: file.filename,
                    client: file.client
                })
            });
            const data = await response.json();
            if (!data.success) {
                failedFiles.push(file);
                failureMessages.push(`${file.filename}: ${data.error || 'erreur inconnue'}`);
            }
        } catch (error) {
            failedFiles.push(file);
            failureMessages.push(`${file.filename}: ${error.message}`);
        }
    }

    for (const pending of pendingToDelete) {
        done++;
        showToast(toastId, `Suppression... (${done}/${total})`);
        try {
            const response = await fetch('/api/activity/pending/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: pending.id })
            });
            const data = await response.json();
            if (!data.success) failureMessages.push(`${pending.title}: ${data.error || 'erreur inconnue'}`);
        } catch (error) {
            failureMessages.push(`${pending.title}: ${error.message}`);
        }
        selectedPendingIds.delete(pending.id);
    }

    for (const { clientKey, item } of activeToDelete) {
        done++;
        showToast(toastId, `Suppression... (${done}/${total})`);
        const endpoint = CLIENT_REMOVE_ENDPOINTS[clientKey];
        try {
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: item.id })
            });
            const data = await response.json();
            if (!data.success) failureMessages.push(`${item.name}: ${data.error || 'erreur inconnue'}`);
        } catch (error) {
            failureMessages.push(`${item.name}: ${error.message}`);
        }
        selectedActiveKeys.delete(`${clientKey}:${item.id}`);
    }

    const failedFolders = [];
    for (const folder of foldersToDelete) {
        done++;
        showToast(toastId, `Suppression... (${done}/${total})`);
        try {
            const response = await fetch('/api/import/incompatible-folder', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ import_root: folder.import_root, relative_path: folder.relative_path })
            });
            const data = await response.json();
            if (!data.success) {
                failedFolders.push(folder);
                failureMessages.push(`${folder.folder_name}: ${data.error || 'erreur inconnue'}`);
            }
        } catch (error) {
            failedFolders.push(folder);
            failureMessages.push(`${folder.folder_name}: ${error.message}`);
        }
        selectedIncompatibleFolderKeys.delete(_incompatibleFolderKey(folder));
    }

    // Ne retire du tableau local que ce qui a réellement été supprimé côté serveur -
    // repéré par référence d'objet (stable même si les index ont bougé entre-temps, même
    // raisonnement que toggleUnassignedSelection plus bas).
    importFiles = importFiles.filter(f => !filesToDelete.includes(f) || failedFiles.includes(f));
    incompatibleFolders = incompatibleFolders.filter(f => !foldersToDelete.includes(f) || failedFolders.includes(f));

    showToast(toastId, failureMessages.length ? `Terminé avec ${failureMessages.length} ${pluralize(failureMessages.length, 'erreur')}` : 'Terminé',
        { icon: failureMessages.length ? 'triangle-alert' : 'check', autoHideMs: 4000 });
    if (failureMessages.length) alert('❌ Échecs:\n' + failureMessages.join('\n'));

    // Réinterroge les clients immédiatement (comme le fait déjà chaque suppression
    // individuelle) plutôt que d'attendre le prochain sondage périodique - rafraîchit
    // aussi bien pending/active que le tableau des fichiers (loadActiveDownloads appelle
    // displayImportFiles en interne).
    await loadActiveDownloads();

    updateImportStats();
    displayImportFiles();
}

function _isFileCheckedForConversion(file) {
    return !!file.convertible && _isFileCheckedForDeletion(file);
}

// Même pattern séquentiel + un seul toast que bulkDeleteSelectedImportFiles ci-dessus -
// une conversion pdf/zip->cbz est un travail CPU/IO non négligeable par fichier (rendu
// des pages, recompression), les lancer en parallèle depuis le client n'accélèrerait rien
// côté serveur (mono-thread de toute façon pour ce genre de traitement), juste plus de
// requêtes en vol à la fois.
// "convertir to pdf only works if i am on the same page. if i quit the page it does not
// go background" - l'ancienne version pilotait la séquence de conversions DEPUIS CETTE
// boucle JS (un fetch par fichier, le suivant attendait la réponse du précédent) :
// fermer l'onglet/naviguer ailleurs arrêtait la boucle elle-même, laissant tout fichier
// pas encore lancé jamais converti. Un seul appel à /api/import/convert-batch (qui lance
// SON PROPRE thread côté serveur pour traiter toute la liste - voir sa docstring,
// routes.py) remplace cette boucle : la page peut être fermée immédiatement après, la
// conversion continue sans elle. Pas de résultat par fichier à afficher en retour (fire-
// and-forget) - le toast persistant (pas d'autoHideMs, voir showToast/nav.js) explique
// qu'un "Actualiser" ultérieur est nécessaire pour voir les .cbz résultants, plutôt que
// de laisser croire à tort que la conversion est déjà terminée.
async function bulkConvertSelectedImportFiles() {
    const filesToConvert = importFiles.filter(_isFileCheckedForConversion);
    const total = filesToConvert.length;
    if (total === 0) return;

    const toastId = 'bulk-convert-import-files';
    try {
        const response = await fetch('/api/import/convert-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                files: filesToConvert.map(f => ({ import_root: f.import_root, relative_path: f.relative_path }))
            })
        });
        const data = await response.json();
        if (!data.success) {
            showToast(toastId, `❌ Erreur: ${data.error || 'erreur inconnue'}`, { icon: 'triangle-alert', autoHideMs: 6000 });
            return;
        }
        showToast(toastId, `Conversion... (${total} ${pluralize(total, 'fichier')})`, { icon: 'loader-circle', autoHideMs: 4000 });
    } catch (error) {
        showToast(toastId, `❌ Erreur de connexion: ${error.message}`, { icon: 'triangle-alert', autoHideMs: 6000 });
    }
}

function _incompatibleFolderRowHtml(folder, index) {
    const extensions = folder.sample_extensions.join(', ');
    const rowId = `incompatible-folder-${index}`;
    const folderKey = _incompatibleFolderKey(folder);
    return `
        <tr style="border-bottom:1px solid #f0f0f0;">
            <td><input type="checkbox" class="import-file-select" ${selectedIncompatibleFolderKeys.has(folderKey) ? 'checked' : ''} onchange="toggleIncompatibleFolderSelection('${escapeForAttribute(folderKey)}', this.checked)" data-tooltip="Sélectionner pour supprimer en masse"></td>
            <td class="import-date-cell">—</td>
            <td>${svgIcon('ban')}</td>
            <td class="import-files-table-filename">
                <div style="font-weight:600;" data-tooltip="${escapeHtml(folder.folder_name)}">${escapeHtml(folder.folder_name)}</div>
                <div class="import-auto-skip-explanation">${svgIcon('ban')} Format non compatible - ${folder.file_count} ${pluralize(folder.file_count, 'fichier')} sans extension supportée (${escapeHtml(extensions)}). Ce pack n'est pas empaqueté en .cbz/.cbr et ne peut pas être importé tel quel.</div>
            </td>
            <td>—</td>
            <td>—</td>
            <td style="text-align:center; text-transform:uppercase; color:var(--color-text-muted); font-size:0.85em;">${escapeHtml(extensions)}</td>
            <td style="text-align:center;">
                <div style="color:#dc3545; font-weight:600;">${svgIcon('x')} Incompatible</div>
                <button class="btn-icon-only incompatible-folder-eye-btn" data-files-row-id="${rowId}-files" onclick="toggleIncompatibleFolderFiles('${rowId}', '${escapeForAttribute(folder.import_root)}', '${escapeForAttribute(folder.relative_path)}')" data-tooltip="Voir les fichiers de ce dossier">${svgIcon('eye')}</button>
                <button class="btn-icon-only" onclick="convertIncompatibleFolderToCbz('${escapeForAttribute(folder.import_root)}', '${escapeForAttribute(folder.relative_path)}', this)" data-tooltip="Empaqueter les images de ce dossier en .cbz (un par album détecté)">${svgIcon('package')}</button>
                <button class="btn-icon-only" onclick="deleteIncompatibleFolder('${escapeForAttribute(folder.import_root)}', '${escapeForAttribute(folder.relative_path)}', this)" data-tooltip="Supprimer ce dossier">${svgIcon('trash-2')}</button>
            </td>
        </tr>
        <tr id="${rowId}-files" style="display:none;"><td colspan="8" style="padding:0 10px 10px 30px; background:var(--color-surface-alt);"></td></tr>
    `;
}

// "options pour convertir une liste de fichier .jpg en .cbz. aussi il faudra faire
// attention que les noms des fichiers ne soient pas differents albums" - un dossier
// "incompatible" (pages scannées en vrac, voir juste au-dessus) est souvent récupérable
// en le repackageant en .cbz. Preview d'abord (regroupement par album détecté, voir
// _group_loose_images_by_album côté Flask) pour confirmation AVANT d'écrire quoi que ce
// soit - un dossier peut mélanger plusieurs albums, mieux vaut que l'utilisateur
// confirme le découpage détecté qu'un empaquetage silencieux potentiellement faux.
async function convertIncompatibleFolderToCbz(importRoot, relativePath, button) {
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="btn-icon">${svgIcon('loader-circle', 'icon-spin')}</span>`;
    const resetButton = () => { button.disabled = false; button.innerHTML = originalHtml; };

    try {
        const previewResponse = await fetch(
            `/api/import/incompatible-folder/convert-to-cbz/preview?import_root=${encodeURIComponent(importRoot)}&relative_path=${encodeURIComponent(relativePath)}`
        );
        const preview = await previewResponse.json();
        if (!preview.success) {
            alert('❌ ' + (preview.error || 'Erreur inconnue'));
            resetButton();
            return;
        }

        const packaging = await openCbzPackagingModeModal(preview.groups);
        if (packaging === null) {
            resetButton();
            return;
        }

        const response = await fetch('/api/import/incompatible-folder/convert-to-cbz', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                import_root: importRoot, relative_path: relativePath,
                manual_groups: packaging.manualGroups,
            })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ Erreur: ' + (data.errors || []).join('\n') || 'Erreur inconnue');
        }
        if (data.created && data.created.length) {
            showToast('convert-jpg-cbz', `✅ ${data.created.length} ${pluralize(data.created.length, 'fichier')} .cbz ${pluralize(data.created.length, 'créé')}`, { icon: 'package', autoHideMs: 4000 });
        }
        // Le dossier "incompatible" a disparu (remplacé par des .cbz normaux) - un
        // rescan complet le fait ressortir comme des fichiers importables normaux.
        await loadActiveDownloads();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        resetButton();
    }
}

// "le mieux ce serait dans la premiere fenêtre de faire le match manuel dans un
// tableau" - un seul tableau éditable dès l'ouverture (renommer/fusionner/ignorer un
// groupe), plus de choix single/multiple/manuel séparé qui renvoyait vers une DEUXIÈME
// modale pour l'option manuelle: ce tableau couvre déjà "un seul .cbz" (bouton "Tout
// fusionner", voir mergeAllCbzManualGroups) et "un .cbz par groupe détecté" (ne rien
// toucher) sans détour. Résout en Promise<{manualGroups}|null> - null si annulé.
let _cbzPackagingModeResolve = null;
let _cbzPackagingGroups = null;

function openCbzPackagingModeModal(groups) {
    return new Promise((resolve) => {
        _cbzPackagingModeResolve = resolve;
        _cbzPackagingGroups = groups;

        document.getElementById('cbz-packaging-groups-count').textContent = groups.length > 1
            ? `${groups.length} groupes détectés :`
            : `Un seul groupe détecté (${groups[0].file_count} ${pluralize(groups[0].file_count, 'page')}) :`;

        document.getElementById('cbz-packaging-groups-toolbar').style.display = groups.length > 1 ? '' : 'none';
        document.getElementById('cbz-manual-select-all').checked = false;

        // "ecrit d'abord les fichiers qui se ressemblent avec le nom du fichier cbz
        // empaqueté. une checkbox pour pouvoir les fusionner" - chaque fichier du groupe
        // sur sa propre ligne (plus lisible qu'une liste condensée par des virgules dans
        // une modale exiguë), le nom du .cbz résultant juste au-dessus comme titre de
        // bloc, et une checkbox de sélection pour la fusion groupée (barre d'actions du
        // haut) plutôt qu'un menu déroulant par ligne à choisir une par une.
        document.getElementById('cbz-packaging-groups-list').innerHTML = groups.map((g, i) => `
            <div class="cbz-manual-group-row" data-group-index="${i}">
                <input type="checkbox" class="cbz-manual-group-select" aria-label="Sélectionner ce groupe" onchange="_updateCbzManualSelectionBar()">
                <input type="text" class="cbz-manual-group-label" value="${escapeHtml(g.label)}" aria-label="Nom du fichier .cbz">
                <span class="cbz-manual-group-count">${g.file_count} ${pluralize(g.file_count, 'page')}</span>
                <span class="cbz-manual-group-status"></span>
                <div class="cbz-manual-group-samples">${g.sample_files.map(f => escapeHtml(f)).join('<br>')}</div>
            </div>
        `).join('');

        document.getElementById('cbz-packaging-mode-modal').classList.add('active');
    });
}

function toggleAllCbzManualGroups(checked) {
    document.querySelectorAll('#cbz-packaging-groups-list .cbz-manual-group-select').forEach(cb => {
        if (!cb.closest('.cbz-manual-group-row').dataset.mergedInto) cb.checked = checked;
    });
    _updateCbzManualSelectionBar();
}

function _updateCbzManualSelectionBar() {
    const count = document.querySelectorAll('#cbz-packaging-groups-list .cbz-manual-group-select:checked').length;
    document.getElementById('cbz-manual-selected-count').textContent = count;
    document.getElementById('cbz-manual-merge-btn').disabled = count < 2;
    document.getElementById('cbz-manual-ignore-btn').disabled = count < 1;
}

// Fusionne tous les groupes cochés dans le PREMIER d'entre eux (ordre d'affichage) - son
// nom éditable fait foi, les autres sont marqués "Fusionné" et grisés (leur input reste
// tel quel mais n'est plus lu, voir confirmCbzPackagingMode) plutôt que supprimés du DOM,
// pour rester réversible en réouvrant simplement la modale (relance depuis le bouton).
function mergeCbzManualSelection() {
    const checked = [...document.querySelectorAll('#cbz-packaging-groups-list .cbz-manual-group-select:checked')];
    if (checked.length < 2) return;
    const rows = checked.map(cb => cb.closest('.cbz-manual-group-row'));
    const [targetRow, ...mergedRows] = rows;
    const targetIndex = targetRow.dataset.groupIndex;
    const targetLabel = targetRow.querySelector('.cbz-manual-group-label').value.trim();

    mergedRows.forEach(row => {
        row.dataset.mergedInto = targetIndex;
        row.querySelector('.cbz-manual-group-select').checked = false;
        row.querySelector('.cbz-manual-group-select').disabled = true;
        row.querySelector('.cbz-manual-group-label').disabled = true;
        row.querySelector('.cbz-manual-group-status').innerHTML = `${svgIcon('git-merge')} Fusionné dans « ${escapeHtml(targetLabel)} »`;
        row.classList.add('cbz-manual-group-row-merged');
    });
    targetRow.querySelector('.cbz-manual-group-select').checked = false;
    _updateCbzManualSelectionBar();
}

// Marque les groupes cochés comme à ignorer (leurs pages restent en vrac, jamais
// empaquetées) - même logique réversible que la fusion (grisé, pas supprimé).
function ignoreCbzManualSelection() {
    const checked = [...document.querySelectorAll('#cbz-packaging-groups-list .cbz-manual-group-select:checked')];
    checked.forEach(cb => {
        const row = cb.closest('.cbz-manual-group-row');
        row.dataset.ignored = '1';
        cb.checked = false;
        cb.disabled = true;
        row.querySelector('.cbz-manual-group-label').disabled = true;
        row.querySelector('.cbz-manual-group-status').innerHTML = `${svgIcon('ban')} Ignoré (non empaqueté)`;
        row.classList.add('cbz-manual-group-row-ignored');
    });
    _updateCbzManualSelectionBar();
}

// Raccourci: sélectionne tout puis fusionne, équivalent à l'ancien mode "single".
function mergeAllCbzManualGroups() {
    toggleAllCbzManualGroups(true);
    mergeCbzManualSelection();
}

function closeCbzPackagingModeModal() {
    document.getElementById('cbz-packaging-mode-modal').classList.remove('active');
    if (_cbzPackagingModeResolve) {
        const resolve = _cbzPackagingModeResolve;
        _cbzPackagingModeResolve = null;
        resolve(null);
    }
}

// Résout les fusions déclarées (data-merged-into pointe directement vers l'index cible,
// jamais de chaîne à remonter: mergeCbzManualSelection fusionne toujours DANS un groupe
// qui n'est lui-même jamais déjà fusionné ailleurs - un groupe fusionné a sa case à
// cocher désactivée, donc jamais sélectionnable comme cible d'une fusion suivante) puis
// construit la liste `manual_groups` envoyée au serveur - chaque entrée référence ses
// groupes source par INDEX (group_indices), jamais par liste de fichiers (voir
// convert_incompatible_folder_to_cbz côté Flask, qui recalcule les fichiers réels à
// l'exécution).
function confirmCbzPackagingMode() {
    const rows = [...document.querySelectorAll('#cbz-packaging-groups-list .cbz-manual-group-row')];
    const byTarget = new Map();

    rows.forEach(row => {
        const i = Number(row.dataset.groupIndex);
        if (row.dataset.ignored) return;
        const root = row.dataset.mergedInto ? Number(row.dataset.mergedInto) : i;
        if (!byTarget.has(root)) byTarget.set(root, []);
        byTarget.get(root).push(i);
    });

    const manualGroups = [...byTarget.entries()].map(([root, indices]) => ({
        label: document.querySelector(`.cbz-manual-group-row[data-group-index="${root}"] .cbz-manual-group-label`).value.trim(),
        group_indices: indices,
    }));

    if (manualGroups.length === 0) {
        alert('Tous les groupes sont ignorés, rien à empaqueter.');
        return;
    }

    document.getElementById('cbz-packaging-mode-modal').classList.remove('active');
    const resolve = _cbzPackagingModeResolve;
    _cbzPackagingModeResolve = null;
    if (resolve) resolve({ manualGroups });
}

const incompatibleFolderFilesCache = {};

async function toggleIncompatibleFolderFiles(rowId, importRoot, relativePath) {
    const detailRow = document.getElementById(`${rowId}-files`);
    if (!detailRow) return;

    if (detailRow.style.display !== 'none') {
        detailRow.style.display = 'none';
        return;
    }

    const cell = detailRow.querySelector('td');
    detailRow.style.display = 'table-row';

    const cacheKey = `${importRoot}|${relativePath}`;
    if (incompatibleFolderFilesCache[cacheKey]) {
        cell.innerHTML = incompatibleFolderFilesCache[cacheKey];
        return;
    }

    cell.innerHTML = `<div style="padding:8px; color:#666;">${svgIcon('loader-circle', 'icon-spin')} Chargement des fichiers...</div>`;
    try {
        const params = new URLSearchParams({ import_root: importRoot, relative_path: relativePath });
        const response = await fetch(`/api/import/incompatible-folder/files?${params}`);
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');

        const remainingCount = data.total_count - data.files.length;
        const truncatedNote = data.truncated
            ? `<div style="padding:6px 8px; font-style:italic;">… et ${remainingCount} ${pluralize(remainingCount, 'autre')} ${pluralize(remainingCount, 'fichier')} non ${pluralize(remainingCount, 'affiché')}</div>` : '';
        const html = `
            <div style="max-height:300px; overflow-y:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <tbody>
                        ${data.files.map(f => `
                            <tr style="border-bottom:1px solid #eee;">
                                <td style="padding:6px 8px;">${escapeHtml(f.name)}</td>
                                <td style="padding:6px 8px; white-space:nowrap; text-align:right;">${formatBytes(f.size)}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
                ${truncatedNote}
            </div>
        `;
        incompatibleFolderFilesCache[cacheKey] = html;
        cell.innerHTML = html;
    } catch (error) {
        cell.innerHTML = `<div style="padding:8px; color:#c0392b;">Erreur: ${escapeHtml(error.message)}</div>`;
    }
}

async function deleteIncompatibleFolder(importRoot, relativePath, button) {
    if (!confirm('Supprimer définitivement ce dossier et son contenu du disque ?')) return;
    if (button) button.disabled = true;

    try {
        const data = await _deleteIncompatibleFolderRequest(importRoot, relativePath);
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        incompatibleFolders = incompatibleFolders.filter(
            f => !(f.import_root === importRoot && f.relative_path === relativePath)
        );
        displayImportFiles();
    } catch (error) {
        alert('❌ Erreur: ' + error.message);
        if (button) button.disabled = false;
    }
}

// Ligne d'info pour un .pdf ou un .zip nu (voir file.convertible côté
// scan_import_directory) - "remove convertir en cbz in a tome line as there is already
// one in the multi selection": le bouton par ligne faisait doublon avec le bouton groupé
// du pied de tableau (bulkConvertSelectedImportFiles) une fois celui-ci ajouté - garde
// juste le texte explicatif (pourquoi CE fichier apparaît convertible) pour aider à
// décider quoi cocher, l'action elle-même passe désormais uniquement par la sélection +
// le bouton groupé. Le fichier reste importable tel quel sans conversion (pdf est un
// format supporté par l'import, juste pas par l'écriture ComicInfo.xml, voir
// WRITABLE_FORMATS côté serveur) - c'est une amélioration proposée, pas un blocage.
function _convertActionHtml(file) {
    if (!file.convertible) return '';
    const label = file.convertible === 'pdf'
        ? 'PDF - convertis en CBZ pour permettre l\'écriture des métadonnées Bédéthèque'
        : 'ZIP nu - convertis en CBZ (format préféré de la bibliothèque)';
    return `
        <div class="import-auto-skip-explanation">
            ${svgIcon('refresh-cw')} ${escapeHtml(label)}
        </div>
    `;
}

// Extrait de _importFileRowHtml pour être réutilisable par displayImportFiles (voir
// _isFileImportingNow plus bas et son usage dans fileEntries): "quand tu importes un
// fichier en cours il est rajouté en bout de tableau [...] tout en bas du tableau c'est
// pas tres bien" - un fichier "Import en cours" reste ENCORE calculé ici avec la même
// formule exacte (une seule source de vérité), mais n'est plus forcément affiché comme
// une ligne de CE tableau - voir plus bas.
function _isFileImportingNow(file) {
    const persistedStatus = file.destination && file.destination.download_status;
    // Le cycle de vie de la base fait foi pour les fichiers suivis. En particulier, ne
    // pas étiqueter un fichier terminé comme « en cours d'import » uniquement parce que
    // le planificateur est activé ou qu'une autre opération d'import est en cours.
    if (persistedStatus) return persistedStatus === 'importing';
    const hasDestination = file.destination;
    const hasKnownVolume = _hasKnownVolume(file);
    const isManualOverride = !!(file.manual_override || (file.destination && file.destination.manual_override));
    const willAutoImportItself = hasDestination && hasKnownVolume && !isManualOverride && !file.auto_import_skip_reason;
    // "si l'import ne marche pas garde en import manuel. retire import en cours dans
    // ce cas" - un fichier avec auto_import_skip_reason (ex: échec répété, voir
    // _repeated_failure_skip_reason côté Flask) ne sera JAMAIS repris par le
    // scheduler: même si un AUTRE import tourne au même moment ailleurs
    // (anyImportInProgress), ce fichier-ci doit rester en mode manuel normal
    // (sélectionnable, Modifier/Retirer/Supprimer visibles) plutôt que d'afficher à
    // tort "⏳ Import en cours" pour une opération qui ne le concerne pas.
    return hasDestination && hasKnownVolume && !file.auto_import_skip_reason && (anyImportInProgress || willAutoImportItself);
}

// Ligne d'un fichier prêt à importer (déjà scanné sur disque, voir scan_import_directory) -
// extrait de displayImportFiles pour être réutilisable: une ligne "pack" en attente (voir
// _pendingPackGroupRowHtml plus bas) a besoin d'afficher ces mêmes fichiers, avec toutes
// leurs actions (destination/tome/suppression), imbriqués sous sa propre ligne dépliante
// plutôt qu'en tête de tableau.
function _importFileRowHtml(file, index) {
    const hasDestination = file.destination;
    const hasKnownVolume = _hasKnownVolume(file);
    // "en import en cours je ne devrais plus rien changer. c'est uniquement en mode
    // import manuel" - calculé une seule fois ici (avant albumHtml/_volumeCellHtml/
    // statusBadge, qui en ont tous besoin) plutôt que recalculé séparément à chaque
    // usage: un import déjà en vol (anyImportInProgress) OU ce fichier lui-même sur
    // le point d'être pris en charge (willAutoImportItself) verrouille à la fois les
    // champs Modifier/tome, la corbeille et le badge de statut. Cette ligne n'est
    // atteinte pour un fichier "en cours" QUE depuis l'intérieur d'un pack déplié
    // (_pendingPackGroupRowHtml) - le niveau racine du tableau les retire plutôt de
    // fileEntries (voir displayImportFiles) au profit du bandeau au-dessus du tableau.
    const isImportingNow = _isFileImportingNow(file);
    const seriesTitleHtml = hasDestination
        ? ((file.destination.series_id && !file.destination.is_new_series)
            ? `<a href="/series/${file.destination.series_id}" class="import-series-link" title="Voir la fiche de cette série">${escapeHtml(file.destination.series_title)}</a>`
            : escapeHtml(file.destination.series_title))
        : '';
    const albumHtml = hasDestination
        ? `
            <span>${svgIcon('pin')} ${escapeHtml(file.destination.library_name)} → ${seriesTitleHtml}${file.destination.is_new_series ? ' · nouvelle série' : ''}</span>
            ${isImportingNow ? '' : `
            <button class="btn-icon-only" onclick="openDestinationModal(${index})" data-tooltip="Modifier">${svgIcon('pencil')}</button>
            <button class="btn-icon-only" onclick="removeDestination(${index})" data-tooltip="Retirer l'assignation">${svgIcon('x')}</button>
            `}
        `
        : `<button class="btn-icon-only" onclick="openDestinationModal(${index})" data-tooltip="Assigner une destination (série existante ou nouvelle, avec matching Bédéthèque)">${svgIcon('pin')}</button>`;
    // buildBedethequeLinkHtml ne connaît que les séries DÉJÀ en base - pour un
    // fichier tout juste assigné à une NOUVELLE série matchée sur Bédéthèque pendant
    // cet import (destination.bedetheque_url, voir pickBedethequeImportMatch), cette
    // série n'existe pas encore en base et le lien restait vide malgré le matching
    // déjà fait. Priorité au match déjà choisi pour CE fichier, retombe sur la
    // recherche par titre sinon - résultat mis en cache sur le fichier lui-même
    // ("import est très lent à s'afficher": parcourt toutes les séries de toutes les
    // bibliothèques, pas la peine de le refaire à chaque re-rendu).
    const bedethequeLinkHtml = file.destination?.bedetheque_url
        ? `<a href="${escapeHtml(file.destination.bedetheque_url)}" target="_blank" rel="noopener" class="import-bedetheque-link" title="Voir la fiche Bédéthèque de « ${escapeHtml(file.destination.series_title)} »"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque"></a>`
        : (file._bedethequeLinkHtml ?? (file._bedethequeLinkHtml = buildBedethequeLinkHtml(fileGroupTitle(file))));
    const trackedConflictVolume = file.parsed.tracked_volume_conflict;
    const statusBadge = file.validation_error
        ? (file.forceImport
            ? `<span style="color:#e67e22; font-weight:600;" data-tooltip="${escapeHtml(file.validation_error)} — import forcé malgré la corruption">${svgIcon('triangle-alert')} Corrompu (forcé)</span>`
            : `<span style="color:#dc3545; font-weight:600;" data-tooltip="${escapeHtml(file.validation_error)}">${svgIcon('circle-x')} Corrompu</span><br><button type="button" class="btn-icon-only" style="font-size:0.75em; padding:2px 6px; margin-top:2px;" ${file._rescanning ? 'disabled' : ''} onclick="rescanCorruptedFile(${index})" data-tooltip="Refait le test d'intégrité maintenant, sans attendre le prochain essai automatique">${file._rescanning ? 'Vérification…' : 'Revérifier'}</button> <button type="button" class="btn-icon-only" style="font-size:0.75em; padding:2px 6px; margin-top:2px;" onclick="forceImportCorruptedFile(${index})">Importer quand même</button>`)
        : trackedConflictVolume != null
        ? `<span style="color:#e67e22; font-weight:600;" data-tooltip="Le tome ${escapeHtml(String(trackedConflictVolume))} était attendu (recherché), mais ce fichier est en réalité ${file.parsed.is_integral ? 'une intégrale' : file.parsed.is_hs ? 'un hors-série' : file.parsed.is_episode ? 'un épisode' : 'un one-shot'} - à vérifier avant de valider">${svgIcon('triangle-alert')} Tome inattendu</span>`
        : file.destination?.download_status === 'pending'
            ? `<span style="color:#6c757d; font-weight:600;">${svgIcon('loader-circle', 'icon-spin')} Téléchargement...</span>`
        : file.destination?.download_status === 'importing'
        ? `<span style="color:#e67e22; font-weight:600;">${svgIcon('loader-circle', 'icon-spin')} Import en cours</span>`
        : file.destination?.download_status === 'completed'
            ? (file.auto_import_skip_reason
                ? `<span style="color:#28a745; font-weight:600;" data-tooltip="L'import automatique ne prendra pas ce fichier (voir raison ci-dessus) - cliquez sur « Importer » pour le valider manuellement">${svgIcon('check')} Prêt — import manuel</span>`
                : file.manual_override
                    ? `<span style="color:#28a745; font-weight:600;" data-tooltip="Assignation faite à la main - cliquez sur « Importer » pour valider, l'import automatique ne le reprendra pas tout seul">${svgIcon('check')} Prêt — import manuel</span>`
                    : `<span style="color:#28a745; font-weight:600;" data-tooltip="Aucune action nécessaire - importé automatiquement dans les secondes qui suivent">${svgIcon('check')} Prêt — import automatique</span>`)
        : isImportingNow
            // Même badge "Import en cours" (loader-circle) dans les deux cas (un autre
            // import déjà en vol, ou ce fichier lui-même sur le point d'être pris en
            // charge par le scan de 5s) - seuls couleur/tooltip distinguent lequel,
            // "Prêt" (check) laisserait sinon croire à tort qu'une action de
            // l'utilisateur est encore attendue ("s'il y a un import en cours je ne
            // devrais pas voir Pret... ca fait une grosse confusion").
            ? (anyImportInProgress
                ? `<span style="color:#e67e22; font-weight:600;" data-tooltip="Un import est déjà en cours - ce fichier est peut-être déjà en train d'être traité">${svgIcon('loader-circle', 'icon-spin')} Import en cours</span>`
                : `<span style="color:#6c757d; font-weight:600;" data-tooltip="Aucune action nécessaire - importé automatiquement dans les secondes qui suivent">${svgIcon('loader-circle', 'icon-spin')} Import en cours</span>`)
            // "au lieu de Pret met une information sur ce qu'il faut faire. attente
            // d'import manuelle, etc..." - cette branche n'est atteinte QUE quand
            // isImportingNow est false alors que hasDestination && hasKnownVolume sont
            // vrais: par construction (voir _isFileImportingNow plus haut), ça veut dire
            // soit manual_override (assignation faite à la main, voir "si jai a faire
            // manuelement un matching alors met un flag pas dimport automayique"), soit
            // auto_import_skip_reason (déjà expliqué dans son propre bandeau juste
            // au-dessus, voir "Pas repris par l'import automatique") - dans les DEUX cas,
            // le scheduler planifié ne le reprendra JAMAIS tout seul: "✓ Prêt" nu
            // laissait croire à tort qu'aucune action n'était nécessaire, alors qu'un
            // clic sur "Importer" (ou la sélection groupée) est la SEULE façon de le
            // finaliser.
            : hasDestination && hasKnownVolume
                ? `<span style="color:#28a745; font-weight:600;" data-tooltip="${file.auto_import_skip_reason ? "L'import automatique ne prendra pas ce fichier (voir raison ci-dessus) - cliquez sur « Importer » pour le valider manuellement" : 'Assignation faite à la main - cliquez sur « Importer » pour valider, l\'import automatique ne le reprendra pas tout seul'}">${svgIcon('check')} Prêt — import manuel</span>`
                : hasDestination
                    ? `<span style="color:#e67e22; font-weight:600;">${svgIcon('triangle-alert')} Tome manquant</span>`
                    : `<span style="color:#dc3545; font-weight:600;">${svgIcon('triangle-alert')} Action requise</span>`;

    return `
        <tr style="border-bottom:1px solid #f0f0f0;">
            <td>${hasDestination ? `
                <input type="checkbox" class="import-file-select"
                       ${_isFileSelected(file) ? 'checked' : ''} ${(file.validation_error && !file.forceImport) ? 'disabled' : ''}
                       onchange="toggleFileSelection(${index}, this.checked)"
                       data-tooltip="${(file.validation_error && !file.forceImport) ? 'Fichier corrompu - import impossible' : 'Inclure dans l\'import'}">
            ` : `
                <input type="checkbox" class="import-file-select"
                       ${file._bulkSelected ? 'checked' : ''} ${(file.validation_error && !file.forceImport) ? 'disabled' : ''}
                       onchange="toggleUnassignedSelection(${index}, this.checked)"
                       data-tooltip="${(file.validation_error && !file.forceImport) ? 'Fichier corrompu - import impossible' : 'Sélectionner pour assigner plusieurs fichiers à la même série d\'un coup'}">
            `}</td>
            <td class="import-date-cell">${escapeHtml(_importDateForFile(file) || '—')}</td>
            <td>${clientBadgeHtml(file.client)}</td>
            <td class="import-files-table-filename">
                <div style="font-weight:600;" data-tooltip="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</div>
                <div style="font-size:0.9em;">${formatBytes(file.file_size)}</div>
                ${file.auto_import_skip_reason ? `<div class="import-auto-skip-explanation">${svgIcon('ban')} Pas repris par l'import automatique : ${escapeHtml(file.auto_import_skip_reason)}</div>` : ''}
                ${_convertActionHtml(file)}
            </td>
            <td><div style="display:flex; align-items:center; gap:4px; flex-wrap:wrap;">${albumHtml}${bedethequeLinkHtml || ''}</div></td>
            <td>${_volumeCellHtml(file, index, isImportingNow)}</td>
            <td style="text-align:center; text-transform:uppercase; color:var(--color-text-muted); font-size:0.85em;">${escapeHtml(_importFileExtension(file))}</td>
            <td style="text-align:center;">
                <div>${statusBadge}</div>
                ${isImportingNow ? '' : `<button class="btn-icon-only" onclick="deleteImportFile(${index})" data-tooltip="Supprimer définitivement ce fichier du disque">${svgIcon('trash-2')}</button>`}
            </td>
        </tr>
    `;
}

function _visiblePendingDownloads() {
    const trackingIdsWithRealFile = new Set(
        importFiles.map(f => f.destination && f.destination.tracking_id).filter(id => id != null)
    );
    const liveTrackingIds = new Set(activeDownloads.map(a => a.item.tracking_id).filter(id => id != null));
    const packGroupsById = new Map(_pendingPackGroups().map(g => [g.pending.id, g]));
    return pendingDownloads.filter(p => {
        const group = packGroupsById.get(p.id);
        const hasMatchedItems = group && (group.fileMatches.length > 0 || group.folderMatches.length > 0);
        if (group && hasMatchedItems) return true;
        if (trackingIdsWithRealFile.has(p.id)) return false;
        if (p.is_pack && liveTrackingIds.has(p.id)) return false;
        return true;
    });
}

function _pendingPackGroups() {
    return pendingDownloads
        .map(pending => ({
            pending,
            fileMatches: importFiles
                .map((file, index) => ({ file, index }))
                .filter(({ file }) => file.pack_download_id === pending.id || (file.destination && file.destination.tracking_id === pending.id))
                .sort((a, b) => {
                    if (importTableSort.column) return _compareImportFiles(a.file, b.file, importTableSort.column, importTableSort.direction);
                    return a.file._order - b.file._order;
                }),
            folderMatches: incompatibleFolders
                .map((folder, index) => ({ folder, index }))
                .filter(({ folder }) => folder.pack_download_id === pending.id),
        }))
        // "you don't need to have pack for this behavior. check if there are more than 1
        // file" - le flag is_pack (mot-clé "PACK"/plage de tomes détecté au moment de
        // l'ajout, voir mark_download_pending) n'est plus consulté ici du tout: un
        // téléchargement de PLUSIEURS fichiers mérite un groupe dépliant qu'il ait ou non
        // été étiqueté "pack" par ce heuristique de nommage, faux-négatif inclus (ex.
        // "BDPACK" collé sans séparateur, ou un simple torrent multi-fichiers jamais
        // annoncé comme pack). Compte les fichiers RÉELS, pas le nombre d'ENTRÉES: un
        // dossier incompatible (voir incompatible_folders côté scan_import_directory)
        // compte pour un seul élément dans folderMatches mais peut contenir des centaines
        // de fichiers (constaté: 1327 pages .jpg brutes dans un seul dossier "BDPACK") -
        // sans folder.file_count ici, ce cas ne dépassait jamais le seuil de 1.
        .filter(({ fileMatches, folderMatches }) => {
            const totalFiles = fileMatches.length
                + folderMatches.reduce((sum, { folder }) => sum + (folder.file_count || 1), 0);
            return totalFiles > 1;
        });
}

function togglePendingPackGroup(pendingId) {
    if (expandedPackIds.has(pendingId)) expandedPackIds.delete(pendingId);
    else expandedPackIds.add(pendingId);
    const detailRow = document.getElementById(`pending-pack-${pendingId}-files`);
    if (!detailRow) return;
    const isExpanded = expandedPackIds.has(pendingId);
    detailRow.style.display = isExpanded ? 'table-row' : 'none';
    // "0 fichier prêt à importer + 1 dossier incompatible?? why i cannot see the content
    // of the dossier" - un pack qui ne contient qu'un dossier incompatible cachait son
    // contenu derrière un DEUXIÈME dépli imbriqué (l'icône œil de la propre ligne du
    // dossier, voir _incompatibleFolderRowHtml) en plus de celui-ci - ouvre directement la
    // liste des fichiers de chaque dossier incompatible nichée dès ce premier dépli,
    // seulement si elle n'est pas déjà ouverte (sinon un second clic sur le chevron du
    // pack la refermerait via .click() en plus de la rouvrir un peu plus haut).
    if (isExpanded) {
        detailRow.querySelectorAll('.incompatible-folder-eye-btn').forEach(btn => {
            const filesRow = document.getElementById(btn.dataset.filesRowId);
            if (filesRow && filesRow.style.display === 'none') btn.click();
        });
    }
}

function _packMemberSubfolder(relativePath) {
    const parts = (relativePath || '').split('/');
    return parts.slice(1, -1).join('/');
}

// Regroupe fileMatches/folderMatches d'un pack par sous-dossier d'origine (racine du
// pack d'abord, puis sous-dossiers par ordre alphabétique) - une liste plate mélangeait
// jusqu'ici des tomes normaux et des dossiers "Bonus"/"Hors série" sans distinction.
function _groupPackMembersBySubfolder(fileMatches, folderMatches) {
    const groups = new Map();
    const getGroup = (key) => {
        if (!groups.has(key)) groups.set(key, { files: [], folders: [] });
        return groups.get(key);
    };
    fileMatches.forEach(m => getGroup(_packMemberSubfolder(m.file.relative_path)).files.push(m));
    folderMatches.forEach(m => getGroup(_packMemberSubfolder(m.folder.relative_path)).folders.push(m));

    return [...groups.entries()].sort(([a], [b]) => {
        if (a === '') return -1;
        if (b === '') return 1;
        return a.localeCompare(b, 'fr', { numeric: true, sensitivity: 'base' });
    });
}

function _pendingPackGroupRowHtml({ pending, fileMatches, folderMatches }) {
    const rowId = `pending-pack-${pending.id}`;
    const isExpanded = expandedPackIds.has(pending.id);
    const total = pending.expected_volume_count || fileMatches.length;
    const readyFileMatches = fileMatches.filter(({ file }) => file.destination && _hasKnownVolume(file));
    const readyCount = readyFileMatches.length;
    const autoReadyCount = readyFileMatches.filter(({ file }) => !file.auto_import_skip_reason && !file.manual_override).length;
    const packNeedsManualAction = readyCount > 0 && autoReadyCount < readyCount;
    const seriesLabel = pending.series_title
        ? (pending.series_id ? `<a href="/series/${pending.series_id}" class="import-series-link" title="Voir la fiche de cette série">${escapeHtml(pending.series_title)}</a>` : escapeHtml(pending.series_title))
        : '—';
    return `
        <tr style="border-bottom:1px solid #f0f0f0;">
            <td><input type="checkbox" class="import-file-select" ${selectedPendingIds.has(pending.id) ? 'checked' : ''} onchange="togglePendingRowSelection(${pending.id}, this.checked)" data-tooltip="Sélectionner pour supprimer en masse"></td>
            <td class="import-date-cell">${escapeHtml(_importDateForPending(pending) || '—')}</td>
            <td>${clientBadgeHtml(pending.client)}</td>
            <td class="import-files-table-filename">
                <button class="btn-icon-only" onclick="togglePendingPackGroup(${pending.id})" data-tooltip="Afficher/masquer les fichiers de ce pack" style="vertical-align:middle; margin-right:2px;">${svgIcon('chevron-down')}</button>
                <span style="font-weight:600;" data-tooltip="${escapeHtml(pending.title)}">${svgIcon('package')} ${escapeHtml(pending.series_title || pending.title)}</span>
                <div style="font-size:0.9em;">${readyCount} ${pluralize(readyCount, 'fichier')} ${pluralize(readyCount, 'prêt')} à importer${fileMatches.length !== readyCount ? ` (${fileMatches.length} au total)` : ''}${folderMatches.length ? ` + ${folderMatches.length} ${pluralize(folderMatches.length, 'dossier incompatible', 'dossiers incompatibles')}` : ''}</div>
            </td>
            <td>${seriesLabel}</td>
            <td>—</td>
            <td style="text-align:center; text-transform:uppercase; color:var(--color-text-muted); font-size:0.85em;">${escapeHtml([...new Set(fileMatches.map(({ file }) => _importFileExtension(file)))].filter(Boolean).join(', '))}</td>
            <td style="text-align:center; min-width:110px;">
                <div>${total === 0
                    ? `<span style="font-size:0.85em;">${svgIcon('loader-circle', 'icon-spin')} En attente...</span>`
                    : readyCount === total && folderMatches.length === 0
                        ? (packNeedsManualAction
                            ? `<span style="color:#28a745; font-weight:600;" data-tooltip="Au moins un fichier de ce pack est bloqué en attente manuelle (skip_reason ou assignation à la main) - dépliez le pack pour voir lequel et cliquer sur « Importer »">${svgIcon('check')} Prêt — import manuel</span>`
                            : `<span style="color:#28a745; font-weight:600;">${svgIcon('check')} Prêt</span>`)
                        : `<span style="color:#e67e22; font-weight:600;">${readyCount}/${total} ${pluralize(readyCount, 'prêt')}</span>`}</div>
                <button class="btn-icon-only" onclick="removePendingPack(${pending.id}, [${fileMatches.map(fm => fm.index).join(',')}], [${folderMatches.map(fm => fm.index).join(',')}], this)" data-tooltip="Supprimer le pack ET ses fichiers/dossiers déjà arrivés sur disque">${svgIcon('trash-2')}</button>
            </td>
        </tr>
        <tr id="${rowId}-files" style="display:${isExpanded ? 'table-row' : 'none'};">
            <td colspan="8" style="padding:0 10px 10px 30px; background:var(--color-surface-alt);">
                <table style="width:100%; border-collapse:collapse;">
                    <tbody>
                        ${_groupPackMembersBySubfolder(fileMatches, folderMatches).map(([subfolder, group]) => {
                            if (!subfolder) {
                                return `
                                    ${group.files.map(({ file, index }) => _importFileRowHtml(file, index)).join('')}
                                    ${group.folders.map(({ folder, index }) => _incompatibleFolderRowHtml(folder, index)).join('')}
                                `;
                            }
                            const subfolderKey = `${pending.id}::${subfolder}`;
                            const isCollapsed = !expandedPackSubfolders.has(subfolderKey);
                            const subfolderSelectableFiles = group.files.filter(({ file }) => file.destination);
                            const subfolderAllSelected = subfolderSelectableFiles.length > 0
                                && subfolderSelectableFiles.every(({ file }) => _isFileSelected(file));
                            return `
                                <tr style="cursor:pointer;" onclick="togglePackSubfolder('${escapeForAttribute(subfolderKey)}')">
                                    <td colspan="8" style="padding:6px 4px; font-weight:600; font-size:0.85em; color:var(--color-text-muted);">
                                        ${subfolderSelectableFiles.length ? `<input type="checkbox" class="import-file-select" style="vertical-align:middle; margin-right:6px;" ${subfolderAllSelected ? 'checked' : ''} onclick="event.stopPropagation()" onchange="toggleSubfolderSelection(${pending.id}, '${escapeForAttribute(subfolder)}', this.checked)" data-tooltip="Inclure/exclure tous les fichiers de ce sous-dossier">` : ''}
                                        <span style="display:inline-block; vertical-align:middle; transition:transform 0.15s ease;${isCollapsed ? ' transform:rotate(-90deg);' : ''}">${svgIcon('chevron-down')}</span> ${svgIcon('folder')} ${escapeHtml(subfolder)}
                                    </td>
                                </tr>
                                ${isCollapsed ? '' : `
                                    ${group.files.map(({ file, index }) => _importFileRowHtml(file, index)).join('')}
                                    ${group.folders.map(({ folder, index }) => _incompatibleFolderRowHtml(folder, index)).join('')}
                                `}
                            `;
                        }).join('')}
                    </tbody>
                </table>
            </td>
        </tr>
    `;
}

function displayImportFiles() {
    const container = document.getElementById('import-files-container');
    const resultsSection = document.getElementById('scan-results');
    const hasAnything = importFiles.length > 0 || activeDownloads.length > 0 || pendingDownloads.length > 0 || incompatibleFolders.length > 0;
    // "do not display what files are on disk... never trigger a disk walk
    // automatically" - le bouton "Actualiser" (refreshImportsEnCours) vit DANS ce
    // conteneur (voir templates/import.html): affiché dès hasScannedOnce (le tout premier
    // rendu de la page), pas seulement quand hasAnything est vrai - sinon un premier
    // chargement sans rien de connu (aucun cache, aucun téléchargement actif/en attente)
    // cachait aussi le seul bouton permettant justement de déclencher un scan.
    if (resultsSection && (hasAnything || hasScannedOnce)) {
        resultsSection.style.display = 'block';
    }

    if (!hasAnything) {
        if (!hasScannedOnce) return;
        // Voir hasCheckedActiveDownloadsOnce: la base n'a pas encore été consultée (le
        // sondage /api/activity/status du chargement de page est toujours en vol) - ne
        // PAS affirmer "aucun téléchargement" tant que ce n'est pas confirmé, un pack
        // pourtant bien présent en base n'aurait alors fait que flasher ce message avant
        // de disparaître une fois loadActiveDownloads() résolu.
        if (!hasCheckedActiveDownloadsOnce) {
            container.innerHTML = `<div class="loading"><div class="spinner"></div><p>Chargement...</p></div>`;
            return;
        }
        // "do not display what files are on disk... never trigger a disk walk
        // automatically" - rien de connu (ni cache, ni téléchargement actif/en attente en
        container.innerHTML = `
            <div class="no-data">
                <h3>Aucun téléchargement suivi pour l'instant</h3>
            </div>
        `;
        return;
    }

    // Par défaut (importTableSort.column === null): ordre figé une seule fois à l'arrivée
    // de chaque fichier (voir _assignStableOrder - "keep it at its place", une correction
    // manuelle ne doit pas faire bouger la ligne). Un clic sur un en-tête (voir
    // setImportTableSort) bascule sur un tri de colonne explicite à la place, recalculé
    // dynamiquement à chaque rendu comme avant.
    _assignStableOrder(importFiles);
    const sortedEntries = importFiles
        .map((file, index) => ({ file, index }))
        .sort((a, b) => {
            if (importTableSort.column) return _compareImportFiles(a.file, b.file, importTableSort.column, importTableSort.direction);
            return a.file._order - b.file._order;
        });

    // "for import there should be a database of all the downloads. this is the base
    // reference that should be used for display" - un pack (torrent contenant plusieurs
    // tomes d'un coup, ex: "Videur.BD.HD.PACK.2024...") reste "en attente" (voir
    // get_pending_downloads/_pendingDownloadRowHtml) même une fois ses fichiers réellement
    // arrivés sur disque - regroupé ici sous une ligne dépliante à son nom (voir
    // _pendingPackGroups) dès que le serveur (scan_import_directory) a authoritativement
    // rattaché au moins un fichier/dossier à sa ligne active_downloads.
    const pendingPackGroups = _pendingPackGroups();
    const nestedInPackIndices = pendingPackGroups.reduce((set, { fileMatches }) => {
        fileMatches.forEach(({ index }) => set.add(index));
        return set;
    }, new Set());
    const nestedInPackFolderIndices = pendingPackGroups.reduce((set, { folderMatches }) => {
        folderMatches.forEach(({ index }) => set.add(index));
        return set;
    }, new Set());

    const importingFiles = sortedEntries
        .filter(({ file, index }) => !nestedInPackIndices.has(index) && _isFileImportingNow(file))
        .map(({ file }) => file);

    const fileEntries = sortedEntries
        .filter(({ file, index }) => !nestedInPackIndices.has(index)
            && !_isFileImportingNow(file)
            && _importNameMatches(file.filename)
            && _importTypeMatches(file.client)
            && _importAlbumMatches(_importFileAlbumText(file))
            && _importVolumeMatches(_importFileVolumeText(file))
            && _importExtensionMatches(_importFileExtension(file)))
        .map(({ file, index }) => ({ date: Number(file.mtime || 0) * 1000, html: _importFileRowHtml(file, index) }));
    const rowsHtml = fileEntries.map(e => e.html).join('');

    // Un dossier incompatible n'a ni client/album/volume/extension unique connus (voir
    // _incompatibleFolderRowHtml, ces colonnes y affichent "—"/une liste d'extensions) -
    // dès qu'un filtre Type/Album/Volume/Ext. est actif, ces lignes disparaissent
    // naturellement (aucune valeur ne peut correspondre), cohérent avec ce que montre
    // réellement le tableau.
    const incompatibleRowsHtml = incompatibleFolders
        .map((folder, index) => ({ folder, index }))
        .filter(({ folder, index }) => !nestedInPackFolderIndices.has(index)
            && _importNameMatches(folder.folder_name || folder.name || folder.path)
            && _importTypeMatches('') && _importAlbumMatches('') && _importVolumeMatches('') && _importExtensionMatches(''))
        .map(({ folder, index }) => _incompatibleFolderRowHtml(folder, index))
        .join('');

    const activeRows = importTableSort.column === 'volume'
        ? [...activeDownloads].sort((a, b) => _compareImportDownloadRows(a, b, importTableSort.direction))
        : importTableSort.column === 'date'
            ? [...activeDownloads].sort((a, b) => _compareImportDateRows(a, b, importTableSort.direction))
        : activeDownloads;
    // "item.name || item.title" comparait auparavant le nom au mauvais niveau (activeRows
    // contient des { clientKey, item }, pas directement des objets avec .name/.title - le
    // filtre Nom ne filtrait donc jamais réellement les téléchargements actifs) -
    // déstructuré ici pour de bon en ajoutant Type/Album/Volume/Ext. au même endroit.
    const activeEntries = activeRows
        .filter(({ clientKey, item }) => _importNameMatches(item.name || item.title)
            && _importTypeMatches(clientKey)
            && _importAlbumMatches(item.series_title)
            && _importVolumeMatches(item.volume_number ? `Tome ${item.volume_number}` : '')
            && _importExtensionMatches(_importFileExtension({ filename: item.name })))
        .map(entry => ({ date: Date.parse(entry.item?.created_at || '') || 0, html: _activeDownloadRowHtml(entry) }));
    const activeRowsHtml = activeEntries.map(e => e.html).join('');
    const visiblePending = _visiblePendingDownloads();
    const pendingRows = importTableSort.column === 'volume'
        ? [...visiblePending].sort((a, b) => _compareImportDownloadRows(a, b, importTableSort.direction))
        : importTableSort.column === 'date'
            ? [...visiblePending].sort((a, b) => _compareImportDateRows(a, b, importTableSort.direction))
        : visiblePending;
    const pendingEntries = pendingRows
        .filter(pending => {
            const volumeText = _pendingVolumeLabel(pending);
            return _importNameMatches(pending.title)
                && _importTypeMatches(pending.client)
                && _importAlbumMatches(pending.series_title)
                && _importVolumeMatches(volumeText === '—' ? '' : volumeText)
                && _importExtensionMatches(_importFileExtension({ filename: pending.title }));
        })
        .map(pending => {
            const group = pendingPackGroups.find(g => g.pending === pending);
            return { date: Date.parse(pending.created_at || '') || 0, html: group ? _pendingPackGroupRowHtml(group) : _pendingDownloadRowHtml(pending) };
        });
    const pendingRowsHtml = pendingEntries.map(e => e.html).join('');

    const mainRowsHtml = importTableSort.column === 'date'
        ? [...pendingEntries, ...activeEntries, ...fileEntries]
            .sort((a, b) => importTableSort.direction === 'asc' ? a.date - b.date : b.date - a.date)
            .map(e => e.html).join('')
        : pendingRowsHtml + activeRowsHtml + rowsHtml;

    // Options des selects Type/Volume/Ext. (voir leurs commentaires): seulement ce qui
    // est réellement présent dans les données, PAS encore réduit par les autres filtres
    // actifs (calculé sur les tableaux non filtrés ci-dessus) - un select qui rétrécirait
    // tout seul selon un autre filtre serait déroutant. Album n'a pas cet équivalent
    // (texte libre, trop de valeurs possibles pour un select).
    const availableClientKeys = new Set();
    const availableVolumeLabels = new Set();
    const availableExtensions = new Set();
    importFiles.forEach(file => {
        if (file.client) availableClientKeys.add(file.client);
        const volumeText = _importFileVolumeText(file);
        if (volumeText) availableVolumeLabels.add(volumeText);
        const ext = _importFileExtension(file);
        if (ext) availableExtensions.add(ext);
    });
    activeDownloads.forEach(({ clientKey, item }) => {
        if (clientKey) availableClientKeys.add(clientKey);
        if (item.volume_number) availableVolumeLabels.add(`Tome ${item.volume_number}`);
        const ext = _importFileExtension({ filename: item.name });
        if (ext) availableExtensions.add(ext);
    });
    visiblePending.forEach(pending => {
        if (pending.client) availableClientKeys.add(pending.client);
        const volumeText = _pendingVolumeLabel(pending);
        if (volumeText && volumeText !== '—') availableVolumeLabels.add(volumeText);
        const ext = _importFileExtension({ filename: pending.title });
        if (ext) availableExtensions.add(ext);
    });

    // Bandeau "N fichier(s) en cours d'import" au-dessus du tableau (voir importingFiles
    // plus haut) - noeud séparé de `container`, mis à jour indépendamment plutôt que
    // reconstruit avec le tableau à chaque rendu.
    const importBanner = document.getElementById('import-in-progress-banner');
    if (importBanner) {
        if (importingFiles.length > 0) {
            const names = importingFiles.slice(0, 5).map(f => escapeHtml(fileGroupTitle(f))).join(', ');
            const extra = importingFiles.length > 5 ? ` et ${importingFiles.length - 5} ${pluralize(importingFiles.length - 5, 'autre')}` : '';
            importBanner.innerHTML = `${svgIcon('loader-circle', 'icon-spin')} ${importingFiles.length} ${pluralize(importingFiles.length, 'fichier')} en cours d'import : ${names}${extra}`;
            importBanner.style.display = 'flex';
        } else {
            importBanner.style.display = 'none';
        }
    }

    const existingScroll = container.querySelector('.import-files-scroll');
    const savedScrollLeft = existingScroll ? existingScroll.scrollLeft : 0;
    const savedScrollTop = existingScroll ? existingScroll.scrollTop : 0;

    container.innerHTML = `
        <div class="import-files-scroll" style="overflow-x:auto;">
            <table class="import-files-table">
                <thead>
                    <tr>
                        <th><input type="checkbox" id="import-select-all" class="import-file-select" onchange="toggleSelectAllFiles(this.checked)" data-tooltip="Tout sélectionner / tout désélectionner"></th>
                        ${_importTableHeaderHtml('date', 'Date')}
                        ${_importTypeFilterHeaderHtml(availableClientKeys)}
                        ${_importNameFilterHeaderHtml()}
                        ${_importAlbumFilterHeaderHtml()}
                        ${_importVolumeFilterHeaderHtml(availableVolumeLabels)}
                        ${_importExtensionFilterHeaderHtml(availableExtensions)}
                        ${_importTableHeaderHtml('status', 'Statut')}
                    </tr>
                </thead>
                <tbody>
                    ${incompatibleRowsHtml}
                    ${mainRowsHtml}
                </tbody>
            </table>
        </div>
        <div class="import-files-table-footer">
            <div id="import-bulk-assign-bar" class="import-bulk-assign-bar" style="display:none;"></div>
            <span id="assign-selected-count" class="import-group-meta-text"></span>
            <button class="btn-danger-sm" onclick="bulkDeleteSelectedImportFiles()" id="delete-selected-btn" disabled>${svgIcon('trash-2')} Supprimer la sélection</button>
            <button class="btn-neutral-sm" onclick="bulkConvertSelectedImportFiles()" id="convert-selected-btn" disabled>${svgIcon('refresh-cw')} Convertir en CBZ</button>
            <button class="btn btn-success" onclick="executeImport()" id="import-btn" disabled>${svgIcon('check')} Importer</button>
        </div>
    `;

    // Réapplique la position de défilement capturée avant la reconstruction ci-dessus
    // (voir le commentaire sur savedScrollLeft/savedScrollTop) - sur le NOUVEAU noeud
    // .import-files-scroll, celui d'avant ayant été détruit par innerHTML.
    const newScroll = container.querySelector('.import-files-scroll');
    if (newScroll && (savedScrollLeft || savedScrollTop)) {
        newScroll.scrollLeft = savedScrollLeft;
        newScroll.scrollTop = savedScrollTop;
    }

    // Reconstruit à chaque rendu (voir plus haut): sans ce rappel ici, le bouton Importer
    // fraîchement recréé perdait l'état disabled/enabled calculé par le dernier appel
    // externe à updateImportStats().
    updateImportStats();
    _updateImportBulkAssignBar();

    initClearableSearchInputs(container);
}

function toggleUnassignedSelection(index, checked) {
    importFiles[index]._bulkSelected = checked;
    _updateImportBulkAssignBar();
}

function _selectedUnassignedFileIndices() {
    return importFiles
        .map((f, i) => i)
        .filter(i => importFiles[i]._bulkSelected && !importFiles[i].destination);
}

function _updateImportBulkAssignBar() {
    const bar = document.getElementById('import-bulk-assign-bar');
    if (!bar) return;
    const count = _selectedUnassignedFileIndices().length;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="import-bulk-assign-count">${count} ${pluralize(count, 'fichier')} ${pluralize(count, 'sélectionné')}</span>
        <button class="btn-neutral-sm" onclick="openBulkAssignDestinationModal()">${svgIcon('pin')} Assigner à une série</button>
        <button class="btn-neutral-sm" onclick="clearUnassignedSelection()">Annuler la sélection</button>
    `;
}

function clearUnassignedSelection() {
    importFiles.forEach(f => { f._bulkSelected = false; });
    displayImportFiles();
}

function openBulkAssignDestinationModal() {
    const indices = _selectedUnassignedFileIndices();
    if (indices.length === 0) return;
    openDestinationModal(indices);
}

// Un fichier sans aucun numéro/type de tome reconnu (ni volume, ni intégrale, ni
// hors-série) n'est pas "disponible à l'import" au sens de la sélection globale ("il
// faudrait qu'elle sélectionne que ceux qui sont disponibles à l'import donc ceux qui
// n'ont pas de volumes ne sont pas checkes").
function _hasKnownVolume(file) {
    // "faire un check rapide de vérification d'intégrité du fichier avant de pouvoir
    // l'importer" - un fichier corrompu (voir validation_error, posé au scan par
    // _check_import_file_validity_cached côté routes.py) n'est jamais "prêt", quel que
    // soit son type par ailleurs reconnu - même raisonnement que tracked_volume_conflict
    // juste en dessous: un blocage qui prime sur tout le reste de cette fonction. "bypass
    // this. I want to import it anyway" - sauf si l'utilisateur a explicitement forcé le
    // passage (forceImportCorruptedFile), auquel cas ce fichier redevient un fichier
    // normal pour le reste de cette fonction.
    if (file.validation_error && !file.forceImport) {
        return false;
    }
    // Un conflit entre le tome suivi et le type réel du fichier (voir
    // tracked_volume_conflict, "⚠ Tome inattendu" ci-dessus) a besoin d'une vérification
    // humaine explicite - ne doit jamais compter comme "prêt" ni être coché par défaut,
    // même si le fichier a par ailleurs un type reconnu (intégrale/HS/one-shot).
    if (file.parsed.tracked_volume_conflict != null) {
        return false;
    }
    // is_special: "in rugby there is a file BO4. i cannot change in the dropdown. nor I
    // can select it" - un bonus/promo corrigé en "Spécial" (voir volume-override-type)
    // n'a par nature pas de numéro, mais est bel et bien prêt à être importé.
    if (file.parsed.volume != null || file.parsed.is_integral || file.parsed.is_hs || file.parsed.is_oneshot_tag || file.parsed.is_episode || file.parsed.is_special) {
        return true;
    }
    // "Moon River... ⚠ Tome manquant / c'est un one-shot donc pas de need d'avoir de
    // tome" - le nom de fichier lui-même n'a souvent aucun marqueur de tome pour un
    // one-shot (pas de "OS"/"HS"/numéro), mais la destination assignée le sait déjà via
    // Bédéthèque: si la série ne connaît qu'UNE seule édition, non numérotée (voir
    // _bdVolumeOptionLabel "Édition unique"), il n'y a tout simplement aucun tome à
    // trouver - ne pas exiger un numéro qui n'existera jamais.
    const seriesId = file.destination && !file.destination.is_new_series ? file.destination.series_id : null;
    const volumes = seriesId != null ? _seriesVolumesCache[seriesId] : null;
    if (volumes && volumes.length === 1 && volumes[0].volume_number == null
        && !volumes[0].is_integral && !volumes[0].is_hs && !volumes[0].is_episode) {
        return true;
    }
    return false;
}

function _isFileSelected(file) {
    return !!file.selected;
}

function toggleFileSelection(fileIndex, checked) {
    importFiles[fileIndex].selected = checked;
    updateImportStats();
}

function forceImportCorruptedFile(fileIndex) {
    const file = importFiles[fileIndex];
    if (!confirm(`"${file.filename}" est corrompu (${file.validation_error}). L'importer quand même ?`)) return;
    file.forceImport = true;
    displayImportFiles();
}

// "add a manual rescan. why the import automatic was triggered if the file was not
// good" - un fichier "Fichier corrompu" épuise son budget de tentatives automatiques
// rapprochées et n'est plus retenté qu'au bout d'un long cooldown (voir
// AUTO_IMPORT_CORRUPTION_COOLDOWN_SECONDS, scheduler.py) - un fichier encore copié sur
// un montage réseau/lent devient pourtant souvent lisible bien avant ça. Ce bouton
// force une revérification immédiate (POST /api/import/rescan-file, qui vide tout
// l'état d'échec mémorisé pour ce fichier côté serveur) sans attendre.
async function rescanCorruptedFile(fileIndex) {
    const file = importFiles[fileIndex];
    file._rescanning = true;
    displayImportFiles();
    try {
        const response = await fetch('/api/import/rescan-file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath: file.filepath })
        });
        const result = await response.json();
        if (!result.success) { alert(result.error || 'Erreur lors de la revérification.'); return; }
        file.validation_error = result.valid ? null : result.error;
    } catch (e) {
        alert(`Erreur lors de la revérification : ${e}`);
    } finally {
        file._rescanning = false;
        displayImportFiles();
    }
}

let _seriesVolumesCache = {};

async function _ensureSeriesVolumesLoaded(seriesId) {
    if (seriesId == null || _seriesVolumesCache[seriesId]) return;
    try {
        const response = await fetch(`/api/series/${seriesId}/volumes`);
        _seriesVolumesCache[seriesId] = await response.json();
    } catch (e) {
        _seriesVolumesCache[seriesId] = [];
    }
}

// Précharge en une fois tous les tomes des séries EXISTANTES déjà assignées parmi
// `files` ("nouvelle série" n'a par définition aucun tome existant à proposer) - appelé
// après tout ce qui peut poser de nouvelles destinations (scan, auto-assignation,
// assignation manuelle) et AVANT le rendu qui en dépend.
async function _ensureVolumesLoadedForFiles(files) {
    const seriesIds = [...new Set(
        files.filter(f => f.destination && !f.destination.is_new_series && f.destination.series_id != null)
            .map(f => f.destination.series_id)
    )];
    await Promise.all(seriesIds.map(_ensureSeriesVolumesLoaded));
}

// Un tome de la liste (voir _ensureSeriesVolumesLoaded) désigne-t-il le même tome que
// `parsed` (file.parsed, ou l'équivalent construit depuis une option choisie) ? Même
// comparaison que le repli "Autre" de la modale Modifier (updateVolumeOverrideVisibility).
function _volumeMatchesParsed(v, parsed) {
    return !!v.is_integral === !!parsed.is_integral && !!v.is_hs === !!parsed.is_hs
        && !!v.is_episode === !!parsed.is_episode
        // "Spécial" partage la même identité "vide" qu'un one-shot (voir même correctif
        // côté _loadTrackingEditVolumes/is_special) - sans ce champ, un fichier corrigé
        // en "Spécial" matchait à tort la première "Édition unique" de la série.
        && !!v.is_special === !!parsed.is_special
        && (v.volume_number ?? null) === (parsed.volume ?? null)
        && (v.integral_number ?? null) === (parsed.integral_number ?? null)
        && (v.hs_number ?? null) === (parsed.hs_number ?? null)
        && (v.episode_number ?? null) === (parsed.episode_number ?? null);
}

// Libellé partagé, voir buildVolumeOptionLabel (nav.js).
function _bdVolumeOptionLabel(v) {
    return buildVolumeOptionLabel(v, { showOwned: true });
}

// Cellule "Volume" d'une ligne du tableau /import - dropdown des tomes connus de la série
// assignée quand on en a une (voir _ensureVolumesLoadedForFiles), repli sur un simple champ
// numérique sinon (nouvelle série tout juste nommée, ou cache pas encore chargé).
function _volumeCellHtml(file, index, disabled = false) {
    const seriesId = file.destination && !file.destination.is_new_series ? file.destination.series_id : null;
    const volumes = seriesId != null ? _seriesVolumesCache[seriesId] : null;
    const disabledAttr = disabled ? ' disabled' : '';

    if (!volumes || volumes.length === 0) {
        // "pourquoi dans import je vois pas le numéro du volume mais pourtant dans
        // l'historique c'est bien le bon tome importé" - une intégrale/HS/épisode
        // correctement détecté (file.parsed.is_integral/is_hs/is_episode) n'a pas de
        // volume_number "brut" à mettre dans un champ numérique - cette colonne
        // n'affichait donc rien du tout pour ce cas, alors que l'import lui-même sait
        // très bien s'en servir (voir build_comicinfo_fields, comicinfo_writer.py).
        // Libellé en lecture seule (même formulation que _bdVolumeOptionLabel) tant que
        // le cache des tomes connus n'est pas chargé, plutôt qu'un champ numéro vide qui
        // ne correspond à rien pour ce type de tome.
        if (file.parsed.is_integral || file.parsed.is_hs || file.parsed.is_episode) {
            const label = file.parsed.is_integral
                ? `Intégrale${file.parsed.integral_number != null ? ' ' + file.parsed.integral_number : ''}`
                : file.parsed.is_hs
                    ? `Hors-série${file.parsed.hs_number != null ? ' ' + file.parsed.hs_number : ''}`
                    : `Épisode${file.parsed.episode_number != null ? ' ' + file.parsed.episode_number : ''}`;
            return `<span style="font-size:0.9em;" data-tooltip="Type détecté depuis le nom du fichier">${escapeHtml(label)}</span>`;
        }
        return `<input type="number" class="import-volume-input" min="0" step="1"
                       value="${file.parsed.volume != null ? file.parsed.volume : ''}"
                       placeholder="—"
                       data-tooltip="Numéro de tome - à vérifier avant d'importer"
                       onchange="updateFileVolume(${index}, this.value)"${disabledAttr}>`;
    }

    const matchIndex = volumes.findIndex(v => _volumeMatchesParsed(v, file.parsed));
    const isManual = matchIndex === -1 && (file.parsed.volume != null || file.parsed.is_integral || file.parsed.is_hs || file.parsed.is_episode);
    const optionsHtml = volumes.map((v, i) => `<option value="${i}"${i === matchIndex ? ' selected' : ''}>${escapeHtml(_bdVolumeOptionLabel(v))}</option>`).join('');
    const selectedLabel = matchIndex !== -1
        ? _bdVolumeOptionLabel(volumes[matchIndex])
        : (isManual ? 'Autre (préciser un numéro)' : '—');

    return `
        <select class="import-volume-select" title="${escapeHtml(selectedLabel)}" onchange="updateFileVolumeSlot(${index}, this.value)"${disabledAttr}>
            <option value="none"${matchIndex === -1 && !isManual ? ' selected' : ''}>—</option>
            ${optionsHtml}
            <option value="manual"${isManual ? ' selected' : ''}>Autre (préciser un numéro)</option>
        </select>
        ${isManual ? `
            <input type="number" class="import-volume-input" min="0" step="1" style="margin-top:4px;"
                   value="${file.parsed.volume != null ? file.parsed.volume : ''}" placeholder="Numéro"
                   onchange="updateFileVolume(${index}, this.value)"${disabledAttr}>
        ` : ''}
    `;
}

// Choix d'un tome dans le dropdown de la table - écrit directement dans file.parsed (même
// champ que updateFileVolume/executeImport lisent déjà), pas de mécanisme séparé.
function updateFileVolumeSlot(fileIndex, value) {
    const file = importFiles[fileIndex];
    if (value !== 'manual') {
        file.manual_override = true;
    }
    if (value === 'none') {
        file.parsed = { ...file.parsed, volume: null, is_integral: false, integral_number: null, is_hs: false, hs_number: null, is_episode: false, episode_number: null, is_special: false, special_label: null, tracked_volume_conflict: null };
    } else if (value !== 'manual') {
        const v = _seriesVolumesCache[file.destination.series_id][parseInt(value, 10)];
        file.parsed = {
            ...file.parsed,
            volume: v.volume_number ?? null,
            is_integral: !!v.is_integral,
            integral_number: v.integral_number ?? null,
            is_hs: !!v.is_hs,
            hs_number: v.hs_number ?? null,
            is_episode: !!v.is_episode,
            episode_number: v.episode_number ?? null,
            is_special: !!v.is_special,
            special_label: v.special_label ?? null,
            tracked_volume_conflict: null,
        };
    }
    // "manual": rien à faire ici, juste réafficher le champ numérique manuel (voir
    // _volumeCellHtml) - sa propre valeur sera posée par updateFileVolume au prochain
    // changement.
    displayImportFiles();
}

function toggleSelectAllFiles(checked) {
    // "I cannot select all the album from import when some are ready and some in
    // waiting. I want to be able to select all and deselect the one I don't want
    // manually" - un fichier pas encore assigné à une série (voir hasDestination,
    // toggleUnassignedSelection) utilise sa propre case _bulkSelected, jamais touchée
    // ici jusqu'ici: "tout sélectionner" ne cochait donc que les fichiers déjà prêts,
    // en ignorant silencieusement tous ceux "en attente" d'assignation.
    // "ce checkbox doit selectionner tous les fichiers quel que soit c'est special,
    // normal ou autres" - la garde _hasKnownVolume (qui excluait un fichier sans tome
    // reconnu, ex: un bonus pas encore corrigé en "Spécial") est retirée: "tout
    // sélectionner" coche désormais TOUT fichier déjà assigné à une série, prêt ou non -
    // à l'utilisateur de décocher ensuite ceux qu'il ne veut pas importer tel quel.
    // Exception: un fichier corrompu (validation_error) ne peut de toute façon jamais
    // être importé (rejeté côté serveur, voir _execute_import_batch) - pas la même
    // ambiguïté qu'un type de tome pas encore classifié, un blocage technique, jamais
    // coché même par "tout sélectionner".
    importFiles.forEach(f => {
        if (f.validation_error) return;
        if (f.destination) f.selected = checked;
        else f._bulkSelected = checked;
    });
    // _visiblePendingDownloads: une ligne "pending" masquée (fichier déjà retrouvé comme
    // vrai fichier importFiles, voir son commentaire) n'a plus de case à cocher visible,
    // "tout sélectionner" ne doit donc pas non plus la marquer sélectionnée.
    _visiblePendingDownloads().forEach(p => {
        if (checked) selectedPendingIds.add(p.id); else selectedPendingIds.delete(p.id);
    });
    activeDownloads.forEach(({ clientKey, item }) => {
        if (item.id == null) return;
        const key = `${clientKey}:${item.id}`;
        if (checked) selectedActiveKeys.add(key); else selectedActiveKeys.delete(key);
    });
    incompatibleFolders.forEach(folder => {
        const key = _incompatibleFolderKey(folder);
        if (checked) selectedIncompatibleFolderKeys.add(key); else selectedIncompatibleFolderKeys.delete(key);
    });
    displayImportFiles();
}

// ===== MATCHING BÉDÉTHÈQUE À L'IMPORT (nouvelles séries uniquement) =====
// Le match choisi dans la modale est mémorisé par titre saisi (normalisé) puis transmis
// à l'exécution de l'import (destination.bedetheque_url): la série est créée déjà
// matchée avec exactement la fiche choisie. Renommer le champ invalide naturellement
// le choix (la clé ne correspond plus).
const bedethequePicks = {};  // titre normalisé -> {url, title}

// Match Bédéthèque choisi pour un titre saisi (ou null): utilisé au moment de
// l'assignation pour le transmettre à l'import
function getBedethequeMatchForTitle(seriesName) {
    const pick = bedethequePicks[normalizeTitle(seriesName)];
    return pick ? pick.url : null;
}

// Met à jour l'icône/texte de statut à côté d'un champ "Nom de la nouvelle série" selon
// qu'un match Bédéthèque a déjà été choisi pour le nom actuellement saisi. Paramétré par
// id (défaut: #select-destination-modal) plutôt que codé en dur - "creer une nouveau
// album devrait faire un match bedetheque" réutilise ce même widget pour
// #tracking-edit-new-series-name (tracking-edit-modal) plutôt que d'en dupliquer un
// second, bedethequePicks lui-même étant déjà partagé (clé = titre normalisé, pas id de
// champ).
function updateNewSeriesBedethequeIndicator(inputId = 'new-series-name', btnId = 'new-series-bedetheque-btn', statusId = 'new-series-bedetheque-status') {
    const input = document.getElementById(inputId);
    const btn = document.getElementById(btnId);
    const status = document.getElementById(statusId);
    if (!input || !btn || !status) return;

    const seriesName = input.value.trim();
    const pick = seriesName ? bedethequePicks[normalizeTitle(seriesName)] : null;
    const img = btn.querySelector('img');
    if (img) {
        img.style.filter = pick ? 'none' : 'grayscale(1)';
        img.style.opacity = pick ? '1' : '.55';
    }
    status.textContent = pick ? `Matché Bédéthèque : ${pick.title || pick.url}` : '';
}

// Même fenêtre de matching manuel que sur les fiches séries (recherche par titre +
// collage d'URL directe), mais la sélection est simplement mémorisée pour ce champ de
// saisie au lieu d'être enregistrée en base (la série n'existe pas encore).
// targetInputId: id du champ texte à préremplir/mettre à jour avec le titre choisi.
// btnId/statusId: voir updateNewSeriesBedethequeIndicator - retenus sur la modale pour que
// pickBedethequeImportMatch sache quel indicateur rafraîchir après un choix, quel que soit
// le champ d'origine (#new-series-name ou #tracking-edit-new-series-name).
function openBedethequeImportModal(targetInputId, btnId = 'new-series-bedetheque-btn', statusId = 'new-series-bedetheque-status') {
    const input = document.getElementById(targetInputId);
    const seriesTitle = input ? input.value.trim() : '';

    let modal = document.getElementById('bedetheque-match-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'bedetheque-match-modal';
        modal.className = 'modal';
        modal.innerHTML = `
            <div class="modal-content" style="max-width: 700px;">
                <span class="close-modal" onclick="closeBedethequeImportModal()">×</span>
                <div id="bedetheque-match-modal-body"></div>
            </div>
        `;
        document.body.appendChild(modal);
    }
    modal._targetInputId = targetInputId;
    modal._btnId = btnId;
    modal._statusId = statusId;
    modal.classList.add('active');

    const body = document.getElementById('bedetheque-match-modal-body');
    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/bedetheque-logo.png',
        title: 'Matcher sur Bedetheque',
        helpText: "Cherchez par titre ou collez directement l'URL de la fiche série Bedetheque.",
        queryId: 'bedetheque-match-query',
        queryPlaceholder: 'Entrez le nom de série ou adresse Bedetheque...',
        prefillValue: seriesTitle,
        resultsId: 'bedetheque-match-results',
        searchOnclick: 'searchBedethequeImportCandidates()',
        autoSearch: true,
    });

    wireMatchModalEnterKeys('bedetheque-match-query', searchBedethequeImportCandidates);

    // Recherche automatique à l'ouverture, voir openBedethequeMatchModal dans library.js
    // pour la même modale côté fiche série (même changement, même raison)
    searchBedethequeImportCandidates();
}

function closeBedethequeImportModal() {
    const modal = document.getElementById('bedetheque-match-modal');
    if (modal) modal.classList.remove('active');
}

async function searchBedethequeImportCandidates() {
    const resultsEl = document.getElementById('bedetheque-match-results');
    const query = document.getElementById('bedetheque-match-query').value.trim();

    if (!query) {
        resultsEl.innerHTML = '';
        return;
    }

    resultsEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const response = await fetch(`/api/bedetheque/search?q=${encodeURIComponent(query)}`);
        const data = await response.json();

        if (!data.success) {
            resultsEl.innerHTML = matchModalErrorHtml(data.error || 'Erreur inconnue');
            return;
        }
        if (!data.results || data.results.length === 0) {
            resultsEl.innerHTML = matchModalNoResultsHtml('Aucune série trouvée sur Bedetheque');
            return;
        }

        resultsEl.innerHTML = data.results.map((r, index) => bedethequeMatchCandidateCardHtml(
            `pickBedethequeImportMatch('${escapeForAttribute(r.url)}', '${escapeForAttribute(r.title)}')`,
            r.title, escapeHtml(r.genre || ''), index, 'import-bedetheque-match'
        )).join('');
        loadBedethequeMatchCandidateCovers(data.results, 'import-bedetheque-match');
    } catch (error) {
        resultsEl.innerHTML = matchModalErrorHtml(error.message);
    }
}

// Titre Bédéthèque -> nom de dossier de série valide: uniquement les caractères
// interdits dans un nom de fichier sont remplacés, le reste (ordre des mots/articles)
// est conservé tel quel - la fiche Bédéthèque fait référence, sans reformatage
function bedethequeTitleToSeriesName(title) {
    return title
        .replace(/[\\/:*?"<>|]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function pickBedethequeImportMatch(url, title) {
    const modal = document.getElementById('bedetheque-match-modal');
    const targetInputId = modal ? modal._targetInputId : null;
    const btnId = modal ? modal._btnId : undefined;
    const statusId = modal ? modal._statusId : undefined;
    closeBedethequeImportModal();
    if (!targetInputId) return;

    const input = document.getElementById(targetInputId);
    if (!input) return;

    // Remplit le champ avec le titre de la fiche choisie: la série sera créée sous le
    // nom Bédéthèque, aligné avec son match (URL collée sans titre: le nom saisi reste)
    if (title) {
        input.value = bedethequeTitleToSeriesName(title);
    }
    const seriesName = input.value.trim();
    if (!seriesName) return;

    bedethequePicks[normalizeTitle(seriesName)] = { url: url, title: title };
    updateNewSeriesBedethequeIndicator(targetInputId, btnId, statusId);
}

function openDestinationModal(fileIndexOrIndices) {
    currentFileIndices = Array.isArray(fileIndexOrIndices) ? fileIndexOrIndices : [fileIndexOrIndices];
    console.debug('openDestinationModal called with indices=', currentFileIndices);
    _populateDestinationModal();
    document.getElementById('select-destination-modal').classList.add('active');
    _refreshLibrariesInBackground(
        'select-destination-modal', 'destination-library', 'destination-series',
        _populateDestinationModal
    );
}

function _populateDestinationModal() {
    const isBulk = currentFileIndices.length > 1;
    const file = importFiles[currentFileIndices[0]];

    document.getElementById('file-to-assign').textContent = isBulk
        ? `${currentFileIndices.length} fichiers sélectionnés`
        : `Fichier: ${file.filename}`;

    // Remplir la liste des bibliothèques
    const librarySelect = document.getElementById('destination-library');
    librarySelect.innerHTML = '<option value="">-- Sélectionner une bibliothèque --</option>' +
        allLibraries.map(lib => `<option value="${lib.id}">${escapeHtml(lib.name)}</option>`).join('');

    // Si déjà assigné, pré-remplir (seulement pour un fichier unique: une assignation
    // groupée part toujours d'un formulaire vierge, les fichiers sélectionnés n'ayant
    // justement pas encore de destination commune à préremplir)
    if (!isBulk && file.destination) {
        librarySelect.value = file.destination.library_id;
        loadLibrarySeries();
        setTimeout(() => {
            const seriesValue = file.destination.series_id ? String(file.destination.series_id) : '__new__';
            document.getElementById('destination-series').value = seriesValue;
            syncSeriesComboboxDisplay();
            if (file.destination.is_new_series) {
                document.getElementById('new-series-name-group').style.display = 'block';
                document.getElementById('new-series-name').value = file.destination.series_title;
            }
            updateVolumeOverrideVisibility(seriesValue, file.destination.volume_override);
        }, 100);
    } else if (allLibraries.length === 1) {
        // Une seule bibliothèque : pas besoin de faire choisir l'utilisateur
        librarySelect.value = allLibraries[0].id;
        loadLibrarySeries();
    }
}

// Rafraîchit allLibraries/librariesSeriesMap APRÈS l'ouverture d'une modale (jamais avant,
// pour ne pas retarder son affichage par cet aller-retour réseau - "ouvre la et charge en
// background"), et ne réappelle onRefreshed que si l'utilisateur n'a pas déjà commencé à
// changer la bibliothèque/série entre-temps (sans quoi on écraserait une sélection en
// cours), ni fermé la modale. Partagé entre openDestinationModal et openTrackingEditModal.
async function _refreshLibrariesInBackground(modalId, librarySelectId, seriesSelectId, onRefreshed) {
    const modal = document.getElementById(modalId);
    const librarySelect = document.getElementById(librarySelectId);
    const seriesSelect = document.getElementById(seriesSelectId);
    const libraryValueBefore = librarySelect ? librarySelect.value : null;
    const seriesValueBefore = seriesSelect ? seriesSelect.value : null;
    await loadAllLibraries();
    if (!modal || !modal.classList.contains('active')) return;
    if (librarySelect && librarySelect.value !== libraryValueBefore) return;
    if (seriesSelect && seriesSelect.value !== seriesValueBefore) return;
    onRefreshed();
}

function closeDestinationModal() {
    document.getElementById('select-destination-modal').classList.remove('active');
    document.getElementById('destination-library').value = '';
    document.getElementById('destination-series').value = '';
    document.getElementById('destination-series-search').value = '';
    _closeSeriesCombobox();
    document.getElementById('new-series-name-group').style.display = 'none';
    document.getElementById('new-series-name').value = '';
    document.getElementById('volume-override-group').style.display = 'none';
    document.getElementById('volume-override-manual-group').style.display = 'none';
    document.getElementById('volume-override-number').value = '';
    volumeOverrideSlots = [];
    currentFileIndices = [];
}

// Préfixe des valeurs <option> représentant une "nouvelle série" DÉJÀ posée par un autre
// fichier de ce même lot d'import (pas encore réellement créée en base - seul
// executeImport() le fait). "quand je crees une nouvelle série dans imports elle n'est
// toujours pas referencée apres": tant que l'import n'a pas tourné, retaper exactement le
// même nom pour un 2e fichier est le seul moyen de le rattacher à la même série, fragile
// (une faute de frappe crée deux séries distinctes) - lister ces séries en attente permet
// de les sélectionner directement, sans re-saisie.
const PENDING_SERIES_PREFIX = '__pending__';

function loadLibrarySeries() {
    const libraryId = document.getElementById('destination-library').value;
    const seriesSelect = document.getElementById('destination-series');

    if (!libraryId) {
        seriesSelect.innerHTML = '<option value="">-- Sélectionner une série --</option>';
        return;
    }

    const series = librariesSeriesMap[libraryId] || [];

    // Titres "nouvelle série" déjà choisis par d'AUTRES fichiers de ce lot (pas les
    // fichiers en cours d'édition eux-mêmes, voir currentFileIndices), pour la même
    // bibliothèque - dédupliqués par titre.
    const editingIndices = new Set(currentFileIndices);
    const pendingTitles = [...new Set(
        importFiles
            .filter((f, idx) => !editingIndices.has(idx) && f.destination && f.destination.is_new_series
                && String(f.destination.library_id) === String(libraryId))
            .map(f => f.destination.series_title)
    )];
    const pendingOptionsHtml = pendingTitles.map(title =>
        `<option value="${PENDING_SERIES_PREFIX}${escapeHtml(title)}">${escapeHtml(title)} (nouvelle série de ce lot, pas encore créée)</option>`
    ).join('');

    seriesSelect.innerHTML = '<option value="">-- Sélectionner une série --</option>' +
        '<option value="__new__">+ Créer une nouvelle série</option>' +
        pendingOptionsHtml +
        series.map(s => `<option value="${s.id}">${escapeHtml(s.title)}</option>`).join('');

    seriesSelect.onchange = function() {
        const newSeriesGroup = document.getElementById('new-series-name-group');
        if (this.value === '__new__') {
            newSeriesGroup.style.display = 'block';
            // Pré-remplir avec le titre parsé du premier fichier sélectionné
            const file = importFiles[currentFileIndices[0]];
            document.getElementById('new-series-name').value = fileGroupTitle(file);
            updateNewSeriesBedethequeIndicator();
        } else {
            newSeriesGroup.style.display = 'none';
        }
        updateVolumeOverrideVisibility(this.value);
    };

    // seriesSelect.innerHTML vient d'être reconstruit (donc remis à '' - "Sélectionner
    // une série") - garde le champ texte visible du combobox (voir openSeriesCombobox
    // ci-dessous) aligné dessus.
    syncSeriesComboboxDisplay();
}

// ===== SÉLECTEUR DE SÉRIE FILTRABLE ("le scrolldown mais avec filter... je tape le nom
// pour trouver au lieu de chercher") =====
// Le <select> réel visé (masqué) reste l'unique source de vérité lue/écrite par le reste
// d'import.js (assignDestination, openDestinationModal, saveTrackingEdit...) - ce champ
// texte + cette liste ne sont qu'une autre façon de le remplir, plus pratique qu'un
// <select> à faire défiler dès qu'une bibliothèque a beaucoup de séries.
//
// Partagé entre #select-destination-modal (pin/pencil, fichier sur disque) et
// #tracking-edit-modal (pencil, téléchargement suivi) - "le bouton epingle et le stylo le
// choix de la série sont différents, garde epingle style pour mettre sur le stylo": ce
// dernier utilisait encore un <select> brut, seul le pin/pencil de la première modale
// avait ce combobox filtrable, une divergence purement historique plutôt que voulue.
// _activeSeriesCombobox retient QUELLE instance (quels ids) est actuellement ouverte, les
// deux modales ne pouvant de toute façon jamais être ouvertes en même temps.
//
// La liste est positionnée en fixed (comme #js-tooltip, voir nav.js) plutôt qu'en absolute
// dans le flux normal: chaque modale a son propre défilement interne (.modal-content,
// overflow-y:auto), une liste en position:absolute classique serait coupée dès qu'elle
// dépasse la zone visible du modal.
let _activeSeriesCombobox = null;

function syncSeriesComboboxDisplay(searchId = 'destination-series-search', selectId = 'destination-series') {
    const input = document.getElementById(searchId);
    const select = document.getElementById(selectId);
    if (!input || !select) return;
    const selected = select.options[select.selectedIndex];
    input.value = (selected && selected.value) ? selected.textContent : '';
}

function _onDocumentMousedownCloseSeriesCombobox(e) {
    if (!_activeSeriesCombobox) return;
    if (e.target.closest(`#${_activeSeriesCombobox.searchId}`) || e.target.closest(`#${_activeSeriesCombobox.listId}`)) return;
    _closeSeriesCombobox();
}

function _closeSeriesCombobox() {
    if (!_activeSeriesCombobox) return;
    const { searchId, selectId, listId, modalSelector } = _activeSeriesCombobox;
    const list = document.getElementById(listId);
    if (list) {
        list.style.display = 'none';
        list.innerHTML = '';
    }
    document.removeEventListener('mousedown', _onDocumentMousedownCloseSeriesCombobox, true);
    const modalContent = document.querySelector(modalSelector);
    if (modalContent) modalContent.removeEventListener('scroll', _closeSeriesCombobox);
    // Fermeture sans sélection (clic ailleurs après avoir tapé sans choisir dans la
    // liste): revenir à ce que le <select> réel contient déjà plutôt que de laisser un
    // texte tapé orphelin qui ne correspond à aucune sélection réelle. Sans effet quand
    // la fermeture suit déjà un choix (selectSeriesComboboxOption a déjà synchronisé
    // juste avant, ceci ne fait alors que répéter la même valeur).
    syncSeriesComboboxDisplay(searchId, selectId);
    _activeSeriesCombobox = null;
}

function openSeriesCombobox(searchId = 'destination-series-search', listId = 'destination-series-list', modalSelector = '#select-destination-modal .modal-content', selectId = 'destination-series') {
    const input = document.getElementById(searchId);
    const list = document.getElementById(listId);
    if (!input || !list) return;
    _activeSeriesCombobox = { searchId, selectId, listId, modalSelector };

    // Texte déjà présent (rouvrir "Modifier" sur un fichier/téléchargement déjà assigné)
    // sélectionné en entier: taper immédiatement remplace la sélection au lieu de
    // filtrer dessus, sans quoi la liste ne montrerait plus que la série déjà choisie
    // tant qu'on ne l'a pas effacée à la main.
    input.select();
    renderSeriesComboboxOptions('');

    const rect = input.getBoundingClientRect();
    list.style.left = `${rect.left}px`;
    list.style.top = `${rect.bottom + 4}px`;
    list.style.width = `${rect.width}px`;
    list.style.display = 'block';

    document.addEventListener('mousedown', _onDocumentMousedownCloseSeriesCombobox, true);
    // Un défilement du modal invaliderait la position figée ci-dessus (fixed, calculée
    // une seule fois à l'ouverture) - fermer plutôt que de la corriger en continu, une
    // liste qui se ferme quand on scrolle est un comportement standard de combobox.
    const modalContent = document.querySelector(modalSelector);
    if (modalContent) modalContent.addEventListener('scroll', _closeSeriesCombobox, { once: true });
}

function renderSeriesComboboxOptions(query) {
    if (!_activeSeriesCombobox) return;
    const { listId, selectId } = _activeSeriesCombobox;
    const list = document.getElementById(listId);
    const select = document.getElementById(selectId);
    if (!list || !select) return;

    const q = normalizeTitle(query || '');
    const options = [...select.options].filter(o => o.value !== '');
    const filtered = q ? options.filter(o => normalizeTitle(o.textContent).includes(q)) : options;

    if (filtered.length === 0) {
        list.innerHTML = '<div class="series-combobox-empty">Aucune série ne correspond</div>';
        return;
    }

    list.innerHTML = filtered.map(o => `
        <button type="button" class="series-combobox-item${o.value === select.value ? ' active' : ''}"
                onmousedown="event.preventDefault(); selectSeriesComboboxOption('${escapeForAttribute(o.value)}')">${escapeHtml(o.textContent)}</button>
    `).join('');
}

function selectSeriesComboboxOption(value) {
    if (!_activeSeriesCombobox) return;
    const { selectId, searchId } = _activeSeriesCombobox;
    const select = document.getElementById(selectId);
    select.value = value;
    // .value assigné directement ne déclenche pas 'change' - on invoque nous-même le
    // handler déclaré sur le <select> pour garder tout le comportement existant (case
    // "nouvelle série", sélecteur de tome...), exactement comme pour la pré-sélection
    // côté openDestinationModal/openTrackingEditModal.
    if (typeof select.onchange === 'function') select.onchange();
    syncSeriesComboboxDisplay(searchId, selectId);
    _closeSeriesCombobox();
}

let volumeOverrideSlots = [];

// existingOverride: destination.volume_override déjà enregistré pour ce fichier (voir
// openDestinationModal) - sans le restaurer, rouvrir "Modifier" sur un fichier déjà
// corrigé manuellement retombait toujours sur "Laisser tel quel", donnant l'impression
// que le choix n'avait jamais été pris en compte ("toujours pas sélectionner le volume").
async function updateVolumeOverrideVisibility(seriesValue, existingOverride) {
    const group = document.getElementById('volume-override-group');
    const isBulk = currentFileIndices.length > 1;
    const show = !isBulk && seriesValue && seriesValue !== '__new__';
    group.style.display = show ? 'block' : 'none';
    if (!show) return;

    const slotSelect = document.getElementById('volume-override-slot');
    const isPending = seriesValue.startsWith(PENDING_SERIES_PREFIX);

    if (isPending) {
        // Série pas encore créée en base (voir loadLibrarySeries/assignDestination) - pas
        // d'ID à interroger via /api/series/<id>/volumes, les seuls tomes "connus" sont
        // ceux déjà posés par les AUTRES fichiers de ce lot visant la même série en
        // attente, synthétisés dans la même forme que la réponse serveur habituelle.
        const pendingTitle = seriesValue.slice(PENDING_SERIES_PREFIX.length);
        const editingIndices = new Set(currentFileIndices);
        volumeOverrideSlots = importFiles
            .filter((f, idx) => !editingIndices.has(idx) && f.destination && f.destination.is_new_series
                && f.destination.series_title === pendingTitle)
            .map(f => ({
                volume_number: f.parsed.volume ?? null,
                is_integral: !!f.parsed.is_integral,
                integral_number: f.parsed.integral_number ?? null,
                is_hs: !!f.parsed.is_hs,
                hs_number: f.parsed.hs_number ?? null,
                is_episode: !!f.parsed.is_episode,
                episode_number: f.parsed.episode_number ?? null,
                filepath: null,
                comicinfo: null
            }));

        const slotOptionsHtml = volumeOverrideSlots.map((v, index) => {
            let label;
            if (v.is_integral) label = `Intégrale${v.integral_number != null ? ' ' + v.integral_number : ''}`;
            else if (v.is_hs) label = `Hors-série${v.hs_number != null ? ' ' + v.hs_number : ''}`;
            else if (v.is_episode) label = `Épisode${v.episode_number != null ? ' ' + v.episode_number : ''}`;
            else if (v.volume_number != null) label = `Tome ${v.volume_number}`;
            else label = 'Édition unique';
            return `<option value="${index}">${escapeHtml(label)} (autre fichier de ce lot)</option>`;
        }).join('');

        slotSelect.innerHTML = '<option value="auto">Laisser tel quel</option>' +
            slotOptionsHtml +
            '<option value="manual">Autre (préciser un numéro)</option>';
        slotSelect.value = 'auto';
        document.getElementById('volume-override-manual-group').style.display = 'none';
        updateVolumeOverrideFields();
        return;
    }

    slotSelect.innerHTML = '<option value="auto">Laisser tel quel</option>' +
        '<option value="manual">Autre (préciser un numéro)</option><option disabled>Chargement des tomes...</option>';
    slotSelect.value = 'auto';
    document.getElementById('volume-override-manual-group').style.display = 'none';

    try {
        const response = await fetch(`/api/series/${seriesValue}/volumes`);
        const volumes = await response.json();
        volumeOverrideSlots = volumes;

        const slotOptionsHtml = volumes.map((v, index) => `<option value="${index}">${escapeHtml(_bdVolumeOptionLabel(v))}</option>`).join('');

        slotSelect.innerHTML = '<option value="auto">Laisser tel quel</option>' +
            slotOptionsHtml +
            '<option value="manual">Autre (préciser un numéro)</option>';
        slotSelect.value = 'auto';

        if (existingOverride) {
            // Cherche un tome de la liste identique à la correction déjà enregistrée -
            // sinon (ex: hors-série pas encore connu de cette série) repli sur "Autre"
            // avec le type/numéro déjà enregistrés pré-remplis.
            const matchIndex = volumes.findIndex(v =>
                !!v.is_integral === !!existingOverride.is_integral &&
                !!v.is_hs === !!existingOverride.is_hs &&
                !!v.is_episode === !!existingOverride.is_episode &&
                // "Spécial" partage la même identité "vide" qu'un one-shot (ni tome, ni
                // intégrale, ni HS, ni épisode) - sans ce champ, un fichier corrigé en
                // "Spécial" matchait à tort la première "Édition unique" trouvée dans la
                // liste (voir is_special côté buildVolumeOverride/routes.py).
                !!v.is_special === !!existingOverride.is_special &&
                (v.volume_number ?? null) === (existingOverride.volume ?? null) &&
                (v.integral_number ?? null) === (existingOverride.integral_number ?? null) &&
                (v.hs_number ?? null) === (existingOverride.hs_number ?? null) &&
                (v.episode_number ?? null) === (existingOverride.episode_number ?? null)
            );
            if (matchIndex !== -1) {
                slotSelect.value = String(matchIndex);
            } else {
                slotSelect.value = 'manual';
                document.getElementById('volume-override-manual-group').style.display = 'block';
                const type = existingOverride.is_special ? 'special'
                    : existingOverride.is_integral ? 'integral' : existingOverride.is_hs ? 'hs' : existingOverride.is_episode ? 'episode' : 'volume';
                document.getElementById('volume-override-type').value = type;
                const number = existingOverride.is_integral ? existingOverride.integral_number
                    : existingOverride.is_hs ? existingOverride.hs_number
                    : existingOverride.is_episode ? existingOverride.episode_number
                    : existingOverride.volume;
                document.getElementById('volume-override-number').value = number != null ? number : '';
                updateVolumeOverrideFields();
            }
        }
    } catch (error) {
        // Repli silencieux sur "auto"/"manual" seuls (déjà en place ci-dessus avant le
        // fetch) si la liste des tomes n'a pas pu être chargée - la correction manuelle
        // reste possible via "Autre"
        slotSelect.innerHTML = '<option value="auto">Laisser tel quel</option>' +
            '<option value="manual">Autre (préciser un numéro)</option>';
    }
    updateVolumeOverrideFields();
}

function updateVolumeOverrideFields() {
    const slot = document.getElementById('volume-override-slot').value;
    document.getElementById('volume-override-manual-group').style.display = slot === 'manual' ? 'block' : 'none';
    // "Spécial" n'a par nature pas de numéro (voir buildVolumeOverride) - le champ numéro
    // n'a donc aucun sens pour ce type, contrairement aux 4 autres.
    const type = document.getElementById('volume-override-type').value;
    document.getElementById('volume-override-number').style.display = (slot === 'manual' && type === 'special') ? 'none' : '';
}

// Construit destination.volume_override, ou undefined si laissé sur "auto" (aucune
// correction à envoyer - le fichier garde ce que parse_filename a détecté, comportement
// inchangé pour le cas courant)
function buildVolumeOverride() {
    const slot = document.getElementById('volume-override-slot').value;
    if (slot === 'auto') return undefined;

    if (slot === 'manual') {
        const type = document.getElementById('volume-override-type').value;
        const rawNumber = document.getElementById('volume-override-number').value.trim();
        const number = rawNumber === '' ? null : parseInt(rawNumber, 10);

        if (type === 'volume') {
            return { volume: number, is_integral: false, integral_number: null, is_hs: false, hs_number: null, is_episode: false, episode_number: null };
        }
        if (type === 'integral') {
            return { volume: null, is_integral: true, integral_number: number, is_hs: false, hs_number: null, is_episode: false, episode_number: null };
        }
        if (type === 'hs') {
            return { volume: null, is_integral: false, integral_number: null, is_hs: true, hs_number: number, is_episode: false, episode_number: null };
        }
        if (type === 'special') {
            // "in rugby there is a file BO4. i cannot change in the dropdown. nor I can
            // select it" - un bonus/promo n'a par nature pas de numéro (voir is_special,
            // CLAUDE.md), contrairement aux 4 autres types proposés ici.
            return { volume: null, is_integral: false, integral_number: null, is_hs: false, hs_number: null, is_episode: false, episode_number: null, is_special: true, special_label: null };
        }
        // type === 'episode'
        return { volume: null, is_integral: false, integral_number: null, is_hs: false, hs_number: null, is_episode: true, episode_number: number };
    }

    // Un tome précis de la liste: on reprend tel quel son propre numéro/type (aucune
    // ambiguïté possible, contrairement à la saisie manuelle)
    const v = volumeOverrideSlots[parseInt(slot, 10)];
    return {
        volume: v.volume_number != null ? v.volume_number : null,
        is_integral: !!v.is_integral,
        integral_number: v.integral_number != null ? v.integral_number : null,
        is_hs: !!v.is_hs,
        hs_number: v.hs_number != null ? v.hs_number : null,
        is_episode: !!v.is_episode,
        episode_number: v.episode_number != null ? v.episode_number : null,
        // Voir même correctif côté type "special" ci-dessus - un tome de la liste peut
        // lui-même être déjà classé Spécial (un autre fichier "Spécial" déjà importé
        // pour cette série).
        is_special: !!v.is_special,
        special_label: v.special_label ?? null
    };
}

async function assignDestination() {
    console.debug('assignDestination called, currentFileIndices=', currentFileIndices);
    const libraryId = parseInt(document.getElementById('destination-library').value);
    const seriesValue = document.getElementById('destination-series').value;
    
    if (!libraryId || !seriesValue) {
        alert('⚠️ Veuillez sélectionner une bibliothèque et une série');
        return;
    }
    
    let library = allLibraries.find(l => l.id === libraryId);
    if (!library) {
        // tolerate string ids
        library = allLibraries.find(l => parseInt(l.id) === libraryId);
    }
    console.debug('assignDestination: libraryId=', libraryId, 'seriesValue=', seriesValue, 'library=', library);
    let destination;
    
    if (seriesValue === '__new__') {
        const newSeriesName = document.getElementById('new-series-name').value.trim();
        if (!newSeriesName) {
            alert('⚠️ Veuillez entrer un nom pour la nouvelle série');
            return;
        }
        
        destination = {
            library_id: libraryId,
            library_name: library.name,
            library_path: library.path,
            series_id: null,
            series_title: newSeriesName,
            is_new_series: true,
            bedetheque_url: getBedethequeMatchForTitle(newSeriesName)
        };
    } else if (seriesValue.startsWith(PENDING_SERIES_PREFIX)) {
        // Rattache CE fichier à une "nouvelle série" déjà posée par un autre fichier de ce
        // lot (voir loadLibrarySeries) - même forme de destination que "__new__" (rien
        // n'existe encore réellement en base), execute_import() dédoublonne déjà par
        // titre exact au moment d'exécuter l'import, donc les deux fichiers finissent
        // sur la MÊME série créée une seule fois.
        const pendingTitle = seriesValue.slice(PENDING_SERIES_PREFIX.length);
        destination = {
            library_id: libraryId,
            library_name: library.name,
            library_path: library.path,
            series_id: null,
            series_title: pendingTitle,
            is_new_series: true,
            bedetheque_url: getBedethequeMatchForTitle(pendingTitle)
        };

        const volumeOverride = buildVolumeOverride();
        if (volumeOverride) {
            destination.volume_override = volumeOverride;
        }
    } else {
        const seriesId = parseInt(seriesValue);
        const seriesList = librariesSeriesMap[libraryId] || librariesSeriesMap[String(libraryId)] || [];
        const series = seriesList.find(s => parseInt(s.id) === seriesId);

        if (!series) {
            alert('⚠️ Série introuvable dans la bibliothèque sélectionnée. Vérifiez la bibliothèque choisie.');
            console.warn('assignDestination: series not found', { libraryId, seriesId, seriesList });
            return;
        }

        destination = {
            library_id: libraryId,
            library_name: library.name,
            library_path: library.path,
            series_id: seriesId,
            series_title: series.title,
            is_new_series: false
        };

        const volumeOverride = buildVolumeOverride();
        if (volumeOverride) {
            destination.volume_override = volumeOverride;
        }
    }

    destination.manual_override = true;

    currentFileIndices.forEach(idx => {
        importFiles[idx].destination = { ...destination };
        // "quand je valide un nouveau tome from editer il n'est pas mis a jour dans la
        // fenetre import" - le backend applique déjà volume_override au moment de
        // l'import (voir execute_import/execute_auto_import côté routes.py), mais la
        // colonne "Volume" du tableau lisait encore file.parsed.volume, jamais mis à jour
        // ici - donnant l'impression que la correction n'avait pas été prise en compte.
        // Répercuté sur file.parsed pour que la colonne (et _hasKnownVolume, donc
        // l'éligibilité à la sélection automatique) reflètent la correction elle-même.
        if (destination.volume_override) {
            const ov = destination.volume_override;
            importFiles[idx].parsed = {
                ...importFiles[idx].parsed,
                volume: ov.volume,
                is_integral: ov.is_integral,
                integral_number: ov.integral_number,
                is_hs: ov.is_hs,
                hs_number: ov.hs_number,
                is_episode: ov.is_episode,
                episode_number: ov.episode_number,
                is_special: ov.is_special,
                special_label: ov.special_label,
            };
        }

        // Persisté côté serveur (best-effort, fire-and-forget: une erreur réseau
        // ponctuelle ne doit pas bloquer l'assignation elle-même côté page, le prochain
        // passage du scheduler reste de toute façon best-effort lui aussi) pour que le
        // scheduler automatique (process séparé, ne partage pas cet état mémoire
        // navigateur) sache ne plus jamais reprendre ce fichier.
        fetch('/api/import/mark-manual', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath: importFiles[idx].filepath })
        }).catch(() => {});
        // Sélection groupée "à assigner" désormais sans objet (le fichier a sa
        // destination), voir toggleUnassignedSelection/_updateImportBulkAssignBar.
        importFiles[idx]._bulkSelected = false;
    });

    // Dropdown des tomes de CETTE série disponible directement dans le tableau (voir
    // _volumeCellHtml) dès l'assignation - pas seulement au prochain scan/auto-match.
    await _ensureVolumesLoadedForFiles(currentFileIndices.map(idx => importFiles[idx]));

    updateImportStats();
    displayImportFiles();
    closeDestinationModal();
}

function updateFileVolume(fileIndex, value) {
    const trimmed = value.trim();
    const parsed = trimmed === '' ? null : parseInt(trimmed, 10);
    const fileParsed = importFiles[fileIndex].parsed;
    fileParsed.volume = Number.isFinite(parsed) ? parsed : null;
    // Voir le commentaire équivalent dans updateFileVolumeSlot: une correction manuelle
    // du numéro de tome EST la vérification humaine attendue, le conflit détecté au scan
    // ne doit plus s'appliquer une fois ce champ modifié à la main.
    fileParsed.tracked_volume_conflict = null;
    // Même correctif que updateFileVolumeSlot ("ca met import en cours" mais rien ne
    // s'importe jamais): purement côté client, le scheduler automatique ne voit jamais
    // cette correction et rejettera ce fichier indéfiniment en silence - manual_override
    // évite d'afficher à tort "Import en cours" comme si aucune action n'était nécessaire.
    importFiles[fileIndex].manual_override = true;
    displayImportFiles();
}

function removeDestination(fileIndex) {
    delete importFiles[fileIndex].destination;
    updateImportStats();
    displayImportFiles();
}

// "si dans la page import il y a des fichiers non importés clique supprimer doit
// supprimer dans le client de telechargement et les fichiers du dossier de
// telechargement" (order.md) - filename/client transmis pour que le serveur tente
// D'ABORD d'annuler le téléchargement chez son client (voir delete_import_file côté
// routes.py) avant de supprimer le fichier local, qui disparaît dans tous les cas.
async function deleteImportFile(fileIndex) {
    const file = importFiles[fileIndex];
    if (!confirm(`Supprimer définitivement "${file.filename}" du disque (et annuler le téléchargement chez le client s'il est retrouvé) ? Cette action est irréversible.`)) {
        return;
    }

    try {
        const data = await _deleteImportFileRequest(file);

        if (!data.success) {
            alert(`❌ Erreur: ${data.error || 'Suppression impossible'}`);
            return;
        }

        // indexOf(file), pas le fileIndex figé au moment de l'appel: un scan de fond
        // (refreshImportFilesQuietly) peut avoir réordonné/fusionné importFiles pendant
        // l'await ci-dessus - l'identité d'objet, elle, survit à ce merge (voir
        // removePendingPack un peu plus haut pour la même correction).
        const idx = importFiles.indexOf(file);
        if (idx !== -1) importFiles.splice(idx, 1);
        updateImportStats();
        displayImportFiles();
    } catch (error) {
        alert(`❌ Erreur de connexion: ${error.message}`);
    }
}


function calculateSimilarity(str1, str2) {
    // Calculer la similarité entre deux chaînes
    if (str1 === str2) return 100;

    // Si une chaîne contient l'autre: signal fort de correspondance côté import (un
    // dossier/fichier est souvent nommé "Titre - T01 - Sous-titre" ou "Titre - Une
    // histoire de Titre" pour une série qui existe en bibliothèque sous le nom "Titre"
    // tout court). Pénaliser ce score au ratio de longueur (ancien calcul) le faisait
    // chuter bien en dessous des seuils d'auto-match (70/90) dès que les deux titres
    // différaient beaucoup en longueur - alors que le contenu, lui, matche parfaitement.
    // Comme ce score ne sert qu'à PRÉ-SÉLECTIONNER un choix que l'utilisateur valide ou
    // corrige manuellement (jamais d'assignation silencieuse sans confirmation), on peut
    // se permettre d'être agressif ici plutôt que de pénaliser la longueur en trop.
    const shorter = str1.length < str2.length ? str1 : str2;
    const longer = str1.length >= str2.length ? str1 : str2;

    if (shorter.length >= 3 && longer.includes(shorter)) {
        // Bonus si le plus court est en tête du plus long (motif le plus courant:
        // "Titre" vs "Titre - T01 - Suffixe") plutôt qu'ailleurs au milieu/en fin
        return longer.startsWith(shorter) ? 96 : 88;
    }

    // Calcul de distance basique (nombre de mots en commun)
    const words1 = str1.split(' ').filter(w => w.length > 2);
    const words2 = str2.split(' ').filter(w => w.length > 2);
    
    let commonWords = 0;
    for (const word of words1) {
        if (words2.includes(word)) {
            commonWords++;
        }
    }
    
    if (words1.length === 0 || words2.length === 0) return 0;
    
    // Score basé sur le ratio de mots communs
    const ratio = commonWords / Math.max(words1.length, words2.length);
    return ratio * 100;
}

// Cherche la meilleure série existante toutes bibliothèques confondues pour un titre
// deviné (parsé depuis un nom de fichier/dossier) donné. Partagé entre autoMatchAll
// (assignation automatique en masse) et buildBedethequeLinkHtml ci-dessous - un seul nom
// importé n'indique pas dans quelle bibliothèque chercher, donc on les compare toutes.
function findBestExistingSeriesAcrossLibraries(rawTitle) {
    const normalizedInput = normalizeTitle(rawTitle);
    let best = null; // { library, series, score }

    for (const lib of allLibraries) {
        const series = librariesSeriesMap[lib.id] || [];

        for (const s of series) {
            // Normalisé une seule fois par série pour toute la durée de vie de la page
            // ("import est très lent à s'afficher") - cette fonction est appelée pour
            // CHAQUE fichier importé (auto-assignation + lien Bédéthèque), sans ce cache
            // chaque appel refaisait le même normalizeTitle (plusieurs passes regex) pour
            // toutes les séries de toutes les bibliothèques, un coût qui grimpe vite avec
            // une grosse bibliothèque.
            if (s._normalizedTitle === undefined) s._normalizedTitle = normalizeTitle(s.title);
            const score = calculateSimilarity(normalizedInput, s._normalizedTitle);
            if (!best || score > best.score) {
                best = { library: lib, series: s, score };
            }
            if (score === 100) return best;
        }
    }

    return best;
}

function buildBedethequeLinkHtml(title) {
    // Pas de lien du tout si aucune série existante ne correspond suffisamment (ou si
    // elle n'a pas encore de fiche Bédéthèque en base) - demandé explicitement: "si ca
    // matche pas ne met pas de lien" plutôt que replier sur une recherche /discover.
    const best = findBestExistingSeriesAcrossLibraries(title);
    if (best && best.score >= 70 && best.series.bedetheque_url) {
        return `<a href="${escapeHtml(best.series.bedetheque_url)}" target="_blank" rel="noopener" class="import-bedetheque-link" onclick="event.stopPropagation()" title="Voir la fiche Bédéthèque de « ${escapeHtml(best.series.title)} »"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque"></a>`;
    }
    return '';
}

async function autoMatchAll() {
    if (allLibraries.length === 0) return;

    let matchCount = 0;

    for (let file of importFiles) {
        if (file.destination) continue; // Déjà assigné

        let best = findBestExistingSeriesAcrossLibraries(fileGroupTitle(file));

        // Repli sur le titre parsé depuis le nom de FICHIER (pas le dossier) quand le
        // score par dossier reste sous le seuil - "why now it is doing... I did not
        // change anything and the matching got lost": un pack téléchargé (torrent
        // "[COLLECTION]") extrait ses tomes dans un sous-dossier nommé d'après le
        // torrent complet (tags de release/groupe compris, voir folder_name côté
        // scan_import_directory), et ce bruit fait chuter le score de mots communs
        // sous 70% alors que file.parsed.title (extrait du nom du FICHIER lui-même par
        // parse_filename) reste propre et matcherait à ~100%. Ne remplace `best` que
        // s'il fait mieux, pour ne jamais dégrader les cas où le dossier était déjà le
        // bon signal (ex: pack d'images en vrac, voir fileGroupTitle).
        const parsedTitle = file.parsed && file.parsed.title;
        if ((!best || best.score < 70) && parsedTitle && parsedTitle !== fileGroupTitle(file)) {
            const titleBest = findBestExistingSeriesAcrossLibraries(parsedTitle);
            if (titleBest && (!best || titleBest.score > best.score)) best = titleBest;
        }

        // Assigner si correspondance >= 70%
        if (best && best.score >= 70) {
            file.destination = {
                library_id: best.library.id,
                library_name: best.library.name,
                library_path: best.library.path,
                series_id: best.series.id,
                series_title: best.series.title,
                is_new_series: false
            };
            matchCount++;
        }
    }

    if (matchCount > 0) showToast('auto-match', `✅ ${matchCount} ${pluralize(matchCount, 'fichier')} ${pluralize(matchCount, 'assigné')} automatiquement`, { icon: 'target', autoHideMs: 4000 });
    await _ensureVolumesLoadedForFiles(importFiles);
    updateImportStats();
    displayImportFiles();
}

async function executeImport() {
    const filesToImport = importFiles.filter(f => f.destination && _isFileSelected(f));

    if (filesToImport.length === 0) {
        alert('⚠️ Aucun fichier à importer');
        return;
    }

    if (!confirm(`Importer ${filesToImport.length} ${pluralize(filesToImport.length, 'fichier')} ?`)) {
        return;
    }

    const importBtn = document.getElementById('import-btn');
    importBtn.disabled = true;
    importBtn.innerHTML = `${svgIcon('loader-circle', 'icon-spin')} Import en cours...`;
    showToast('import', 'Import en cours...');

    anyImportInProgress = true;
    displayImportFiles();

    try {
        const response = await fetch('/api/import/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                files: filesToImport
            })
        });

        const data = await response.json();

        if (data.success) {
            let message = `✅ Import terminé !\n\n`;
            message += `📥 Importés : ${data.imported_count}\n`;
            if (data.replaced_count > 0) {
                message += `🔄 Remplacés : ${data.replaced_count} (ancien fichier supprimé)\n`;
            }
            if (data.skipped_count > 0) {
                message += `⏭️ Ignorés : ${data.skipped_count} (doublon moins bon, supprimé)\n`;
            }
            if (data.failed_count > 0) {
                message += `❌ Échecs : ${data.failed_count}\n`;
                // Détail affiché directement dans l'alerte plutôt que seulement en
                // console: sans ça, l'utilisateur n'avait aucun moyen de savoir QUEL
                // fichier a échoué et pourquoi sans ouvrir les devtools
                if (data.failures && data.failures.length > 0) {
                    message += '\n' + data.failures.slice(0, 10).map(f => `  • ${f.file}: ${f.error}`).join('\n');
                    if (data.failures.length > 10) {
                        const remaining = data.failures.length - 10;
                        message += `\n  … et ${remaining} ${pluralize(remaining, 'autre')}`;
                    }
                }
            }
            if (data.cleaned_directories > 0) {
                message += `\n🧹 Répertoires vides nettoyés : ${data.cleaned_directories}\n`;
            }

            if (data.imported_count > 0 || data.replaced_count > 0) {
                showToast('komga-scan', 'Scan Komga demandé', { icon: 'radio', autoHideMs: 4000 });
            }
            alert(message);

            await loadAllLibraries();

            // Recharger le scan (cet import vient de le vider): l'historique complet est
            // désormais consultable sur sa propre page (voir /history, static/js/history.js)
            await loadActiveDownloads();
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    } finally {
        importBtn.disabled = false;
        importBtn.innerHTML = `${svgIcon('check')} Importer`;
        dismissToast('import');
        // Resynchronise anyImportInProgress sur l'état RÉEL côté serveur (voir plus haut)
        // que l'import ait réussi, échoué, ou même jamais atteint le serveur (erreur
        // réseau) - sinon un échec laisserait le flag bloqué à `true` indéfiniment, et
        // TOUTES les lignes afficheraient "⏳ Import en cours" à tort après ça. Fait aussi
        // apparaître cette opération dans le résumé en bas de page tout de suite, sans
        // attendre un rechargement de la page.
        loadImportHistorySection();
    }
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

function formatEta(seconds) {
    if (seconds == null || seconds < 0) return null;
    if (seconds < 60) return `${Math.round(seconds)}s restantes`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min restantes`;
    return `${(seconds / 3600).toFixed(1)} h restantes`;
}

function escapeHtml(text) {
    if (!text) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

function escapeForAttribute(text) {
    return String(text ?? '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ===== HISTORIQUE RÉCENT DES IMPORTS (résumé en bas de /import) =====
// "widget history and /history should have the same UI [...] no need to do double code
// for the same things" - le rendu (résumé, statut, détail par fichier) vit dans
// history-shared.js, chargé avant ce fichier (voir templates/import.html), et est
// PARTAGÉ à l'identique avec la page /history (static/js/history.js). Ce qui reste ici
// est spécifique au widget: scopé aux seuls imports (pas téléchargements/renommages/
// suppressions), limité aux 10 plus récents.
//
// "widget auto-refreshes every 10s. remove that. no autorefresh unless there is an
// import done [...] manual or automatic" - PAS de minuteur aveugle: rechargé uniquement
// juste après un import manuel (voir plus bas) et depuis refreshImportFilesQuietly quand
// un fichier suivi disparaît du scan (signe qu'il vient d'être importé - par ce même
// onglet, un autre onglet, ou le scheduler automatique - seul signal fiable dont dispose
// le client pour un import déclenché côté serveur sans clic ici).

let _importHistorySignature = null;

let _openImportHistoryOperationIds = new Set();

async function loadImportHistorySection(silent = false) {
    const loading = document.getElementById('import-history-loading');
    const empty = document.getElementById('import-history-empty');
    const table = document.getElementById('import-history-table');
    if (!silent) {
        loading.style.display = 'block';
        empty.style.display = 'none';
        table.style.display = 'none';
    }

    try {
        // "widget history in import is limited to 10 entry"
        const response = await fetch('/api/import/history?limit=10');
        const data = await response.json();
        const events = data.history || [];
        if (!silent) loading.style.display = 'none';

        const wasInProgress = anyImportInProgress;
        anyImportInProgress = events.some(e => e.status === 'started');
        if (anyImportInProgress !== wasInProgress) displayImportFiles();

        if (events.length === 0) {
            if (silent && _importHistorySignature === '[]') return;
            _importHistorySignature = '[]';
            loading.style.display = 'none';
            empty.style.display = 'block';
            table.style.display = 'none';
            return;
        }

        const signature = JSON.stringify(events.map(h => [
            h.operation_id, h.status, h.files_imported, h.files_replaced,
            h.files_skipped, h.files_failed, h.file_names
        ]));
        if (silent && signature === _importHistorySignature) return;
        _importHistorySignature = signature;

        loading.style.display = 'none';
        table.style.display = '';
        renderImportHistorySection(events);
    } catch (error) {
        if (!silent) loading.style.display = 'none';
        console.error('Erreur lors du chargement de l\'historique des imports:', error);
    }
}

let _importHistoryRawEvents = [];
let _importHistorySearchText = '';
let _importHistorySort = { column: null, direction: 'asc' };

function filterImportHistoryBySearchText(value) {
    _importHistorySearchText = value || '';
    renderImportHistorySection(_importHistoryRawEvents);
}

function _importHistorySortValue(event, column) {
    if (column === 'date') return event.date ? (parseDbUtcDate(event.date)?.getTime() ?? 0) : 0;
    if (column === 'type') return (HISTORY_TYPE_LABELS[event.type] || event.type || '').toLowerCase();
    if (column === 'label') return (event.label || '').toLowerCase();
    if (column === 'status') return event.success ? 1 : 0;
    return 0;
}

function setImportHistorySort(column) {
    if (_importHistorySort.column === column) {
        _importHistorySort.direction = _importHistorySort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        _importHistorySort = { column, direction: 'asc' };
    }
    _updateImportHistorySortIndicators();
    renderImportHistorySection(_importHistoryRawEvents);
}

function _updateImportHistorySortIndicators() {
    document.querySelectorAll('#import-history-events-wrapper [data-sort-column]').forEach(th => {
        const active = th.dataset.sortColumn === _importHistorySort.column;
        const arrow = th.querySelector('.import-history-sort-arrow');
        if (arrow) arrow.textContent = active ? (_importHistorySort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
        th.style.color = active ? 'var(--color-accent)' : '';
        th.style.fontWeight = active ? '600' : '';
    });
}

function renderImportHistorySection(rawEvents) {
    // mapImportHistoryToEvents/historyEventRowHtml: voir history-shared.js - même
    // gabarit que /history, idPrefix distinct ('import-history-row') pour ne pas
    // collisionner avec history.js si jamais les deux tournaient un jour sur la même page.
    _importHistoryRawEvents = rawEvents || [];
    let events = mapImportHistoryToEvents(_importHistoryRawEvents);
    const needle = navNormalizeSearch(_importHistorySearchText.trim());
    if (needle) {
        events = events.filter(e => navNormalizeSearch(`${e.label || ''} ${e.detail || ''}`).includes(needle));
    }
    if (_importHistorySort.column) {
        events = [...events].sort((a, b) => {
            const va = _importHistorySortValue(a, _importHistorySort.column);
            const vb = _importHistorySortValue(b, _importHistorySort.column);
            const cmp = (typeof va === 'number' && typeof vb === 'number')
                ? va - vb
                : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
            return _importHistorySort.direction === 'asc' ? cmp : -cmp;
        });
    }
    const body = document.getElementById('import-history-body');
    body.innerHTML = events.map((e, index) => historyEventRowHtml(e, index, 'import-history-row')).join('');
    _updateImportHistorySortIndicators();

    // Rouvre les lignes qui l'étaient avant cette reconstruction (voir
    // _openImportHistoryOperationIds) - toggleImportFiles ouvre normalement (état
    // fraîchement "none" après le rebuild ci-dessus) et réutilise son propre cache si déjà
    // chargé, donc sans re-fetch ni "⏳ Chargement..." visible pour l'utilisateur.
    events.forEach((e, index) => {
        if (_openImportHistoryOperationIds.has(e.operationId)) {
            toggleImportFiles(`import-history-row-${index}`, e.operationId);
        }
    });
}

window.onclick = function(event) {
    const modal = document.getElementById('select-destination-modal');
    if (event.target == modal) {
        closeDestinationModal();
    }
}

window.addEventListener('load', function() {
    // "why different windows different result.. is there any cache that shoud not be
    // there.. remove the cache it does not help" - un cache localStorage par fenêtre
    // affichait un état périmé différent selon quelle fenêtre l'avait écrit en dernier
    // (une fenêtre ouverte depuis longtemps montrait son propre instantané figé, une
    // fenêtre neuve n'en avait aucun) - deux fenêtres du même /import ne montraient
    // jamais la même chose. Supprimé : la base (via loadActiveDownloads ci-dessous) est
    // la seule source, à charger à chaque fois plutôt que pré-remplie d'un instantané
    // local potentiellement faux. hasScannedOnce est mis à true inconditionnellement
    // (plus jamais un scan qui le fait passer à true lui-même) pour que
    // displayImportFiles() affiche tout de suite l'état réel ("Aucun fichier trouvé"
    // invitant à Actualiser, ou le chargement en cours - voir hasCheckedActiveDownloadsOnce)
    // plutôt que de laisser indéfiniment le placeholder statique "Scan en cours..." du
    // template, qui ne correspond plus à rien.
    hasScannedOnce = true;
    updateImportStats();
    displayImportFiles();

    (async () => {
        await Promise.all([loadAllLibraries(), loadActiveDownloads()]);
        // "when you load the import page and the file is downloading completly you
        // should scan the folder to display the file itself. no need of manual action
        // here" - le scan de rattrapage lui-même vit maintenant DANS loadActiveDownloads
        // (voir hasUnscannedCompletedDownload) plutôt qu'ici uniquement: "did not trigger
        // the scan" - un téléchargement qui passe 'completed' PENDANT que la page est
        // déjà ouverte (détecté par refreshDownloadingProgress, plus bas) a besoin du
        // même rattrapage que le tout premier chargement, pas seulement celui-ci.
        loadImportHistorySection();
    })();
});

// "why having a schedule for re render? [...] i don't want to have a re render instead
// it is manually done" - plus de reconstruction complète périodique du tableau
// (l'ancien setInterval(loadActiveDownloads, 10000) perdait la position de défilement à
// chaque cycle, voir .import-files-scroll côté displayImportFiles): loadActiveDownloads()
// (structure - apparition/disparition/import) ne tourne plus qu'au chargement de la page
// et sur clic explicite "Actualiser" (refreshImportsEnCours ci-dessous) - idem pour
// refreshImportFilesQuietly (scan de répertoire), déjà uniquement manuel.
//
// "make the update for the download a 5s but only for the downloading files" / "still
// waiting for download status update every 5s. now it is not updating" - le premier jet
// ne touchait QUE activeDownloads (clients probés en direct: qBittorrent/rTorrent/Deluge/
// aMule), jamais un téléchargement Telegram encore actif (voir pendingDownloads,
// bytes_downloaded/bytes_total alimentés par update_download_progress côté downloader.py -
// SEULE source qui progresse réellement d'un sondage à l'autre, les autres clients
// n'apparaissent dans pendingDownloads qu'à l'état 'pending'/'completed' figé ou dans
// activeDownloads pour leur progression réelle). Les deux groupes sont mis à jour EN
// PLACE ici (jamais innerHTML du tableau entier, donc jamais de perte de défilement) - un
// vrai changement structurel (téléchargement apparu/disparu/fini) retombe sur
// loadActiveDownloads(), seul capable de refléter ça correctement (recalcule aussi les
// lignes "pending" simples/regroupées en pack) - rare comparé au tic de progression
// normal.
function refreshDownloadingProgress() {
    const oldDownloadingPending = pendingDownloads.filter(p => p.status !== 'completed' && p.bytes_total != null);
    if (activeDownloads.length === 0 && oldDownloadingPending.length === 0) return; // rien à mettre à jour, pas d'appel réseau inutile
    fetch('/api/activity/status')
        .then(r => r.json())
        .then(data => {
            if (!data.success) return;
            const newActive = [];
            for (const client of data.clients) {
                for (const item of client.items) {
                    newActive.push({ clientKey: client.client, item });
                }
            }
            const newDownloadingPending = (data.pending || []).filter(p => p.status !== 'completed' && p.bytes_total != null);

            const oldKeys = new Set([
                ...activeDownloads.map(a => _activeDownloadKey(a.clientKey, a.item)),
                ...oldDownloadingPending.map(p => `pending:${p.id}`),
            ]);
            const newKeys = new Set([
                ...newActive.map(a => _activeDownloadKey(a.clientKey, a.item)),
                ...newDownloadingPending.map(p => `pending:${p.id}`),
            ]);
            const structurallyChanged = oldKeys.size !== newKeys.size || [...oldKeys].some(k => !newKeys.has(k));
            if (structurallyChanged) {
                // Un téléchargement est apparu/a disparu/vient de finir - seul un rendu
                // complet (loadActiveDownloads, qui recalcule aussi pendingDownloads) peut
                // refléter ça correctement.
                loadActiveDownloads();
                return;
            }

            activeDownloads = newActive;
            for (const { clientKey, item } of newActive) {
                const row = document.querySelector(`[data-active-key="${CSS.escape(_activeDownloadKey(clientKey, item))}"]`);
                if (!row) continue;
                const fill = row.querySelector('.active-download-progress-fill');
                const label = row.querySelector('.active-download-progress-label');
                const sizeLine = row.querySelector('.active-download-size-line');
                if (fill) fill.style.width = `${Math.min(100, Math.max(0, item.progress || 0))}%`;
                if (label) label.innerHTML = _activeDownloadProgressLabelHtml(item);
                if (sizeLine) sizeLine.innerHTML = _activeDownloadSizeLineHtml(item);
            }

            // Remplace UNIQUEMENT les entrées encore en progression (Telegram) dans
            // pendingDownloads - les autres (packs, 'completed', sans bytes_total) restent
            // les objets déjà affichés, structurallyChanged garantit qu'aucune d'elles n'a
            // par ailleurs changé d'identité ce cycle-ci.
            const freshById = new Map(newDownloadingPending.map(p => [p.id, p]));
            pendingDownloads = pendingDownloads.map(p => freshById.get(p.id) || p);
            for (const pending of newDownloadingPending) {
                const row = document.querySelector(`[data-pending-key="${pending.id}"]`);
                if (!row) continue;
                const fill = row.querySelector('.pending-download-progress-fill');
                const label = row.querySelector('.pending-download-progress-label');
                if (fill) fill.style.width = `${Math.min(100, Math.max(0, (pending.bytes_downloaded || 0) * 100 / pending.bytes_total))}%`;
                if (label) label.innerHTML = _pendingProgressLabelHtml(pending);
            }
        })
        .catch(() => {}); // erreur réseau ponctuelle - ne pas perturber l'affichage existant
}
// 5s -> 15s: "repeated five-second polling—especially from multiple browser tabs—caused
// [aMule EC] assertions and eventual file-descriptor exhaustion" - le cache serveur
// (_amule_status, activity/routes.py) protège déjà contre ça à lui seul (au plus une
// connexion EC toutes les 20s quel que soit le nombre d'onglets), mais un aller-retour
// HTTP sur 5 tous les 5s pour rien (le cache renvoie alors la même réponse déjà vue)
// n'apportait de toute façon aucune fraîcheur réelle à l'affichage.
setInterval(refreshDownloadingProgress, 15000);

// A manual series-match review links here with the title in the query string.
// Reuse the normal Import Bédéthèque modal (the same one used by the destination
// assignment form), rather than sending the user through Discover's series-creation
// flow. The hidden field gives pickBedethequeImportMatch its normal target.
document.addEventListener('DOMContentLoaded', () => {
    const title = new URLSearchParams(window.location.search).get('bedetheque_match');
    if (!title) return;
    const input = document.createElement('input');
    input.type = 'hidden';
    input.id = 'validation-bedetheque-series-name';
    input.value = title;
    document.body.appendChild(input);
    openBedethequeImportModal('validation-bedetheque-series-name');
});

async function refreshImportsEnCours(buttonEl) {
    if (buttonEl) buttonEl.disabled = true;
    try {
        // /api/import/state contient déjà l'état de la base/du client et l'instantané du
        // système de fichiers suivi. Un second scan provoquait auparavant une course
        // avec la première réponse et pouvait réintroduire des lignes obsolètes ou
        // écraser une association manuelle.
        await loadActiveDownloads();
    } finally {
        if (buttonEl) buttonEl.disabled = false;
    }
}
