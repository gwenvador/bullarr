// Page "Historique": combine deux sources déjà journalisées séparément côté serveur
// (imports de fichiers depuis /amule et /torrents, et téléchargements auto de volumes
// manquants/nouveaux) en une seule timeline, plutôt que deux pages distinctes à consulter
let allHistoryEvents = [];
let currentHistoryFilter = 'all';
// "add a filter so I can search for special entry... met le au niveau de details dans la
// barre de recherche. un filtre comme dans les autres tableaux du site" - filtre texte
// libre (titre/détail) posé dans l'en-tête "Détails" (voir templates/history.html), même
// pattern .th-filterable-* que les tableaux série/tomes/résultats de recherche plutôt
// qu'une barre de recherche à part.
let currentHistorySearchText = '';
let currentHistoryStatusFilter = '';

function _historyNormalizeForSearch(text) {
    return String(text || '')
        .replace(/œ/g, 'oe').replace(/Œ/g, 'OE')
        .replace(/æ/g, 'ae').replace(/Æ/g, 'AE')
        .normalize('NFKD')
        .replace(/[̀-ͯ]/g, '')
        .toLowerCase();
}

function filterHistoryBySearchText(value) {
    currentHistorySearchText = value || '';
    renderHistoryEvents();
}

async function loadHistoryEvents() {
    document.getElementById('history-events-loading').style.display = 'block';
    document.getElementById('history-events-empty').style.display = 'none';
    document.getElementById('history-events-body').innerHTML = '';

    try {
        const [importResp, downloadResp, actionResp] = await Promise.all([
            fetch('/api/import/history?limit=500').then(r => r.json()).catch(() => ({history: []})),
            fetch('/api/missing-monitor/history?limit=500').then(r => r.json()).catch(() => ({history: []})),
            fetch('/api/actions/history?limit=500').then(r => r.json()).catch(() => ({history: []}))
        ]);

        // "widget history and /history should have the same UI [...] no need to do
        // double code for the same things" - le mapping réponse brute -> ligne d'affichage
        // (résumé, statut à 3 états, aperçu des fichiers, fusion des 3 sources) vit
        // maintenant dans history-shared.js (mapHistoryResponsesToEvents), partagé avec le
        // widget "Historique récent" de /import (import.js) ET la fiche historique d'une
        // série (openSeriesHistoryModal, library.js) plutôt que dupliqué ici.
        allHistoryEvents = mapHistoryResponsesToEvents(importResp, downloadResp, actionResp);

        document.getElementById('history-events-loading').style.display = 'none';
        renderHistoryEvents();
    } catch (error) {
        document.getElementById('history-events-loading').style.display = 'none';
        console.error('Erreur lors du chargement de l\'historique:', error);
    }
}

function filterHistoryEvents(type) {
    currentHistoryFilter = type;
    document.querySelectorAll('#history-filter-all, #history-filter-import, #history-filter-download, #history-filter-search, #history-filter-rename, #history-filter-convert, #history-filter-delete')
        .forEach(btn => btn.classList.remove('history-filter-active'));
    document.getElementById('history-filter-' + type).classList.add('history-filter-active');
    // Reste synchronisé avec le select de la colonne Type (voir templates/history.html) -
    // que le déclencheur soit un clic d'onglet ou un choix dans le select, les deux
    // doivent refléter le même état.
    const typeSelect = document.getElementById('history-type-select');
    if (typeSelect && typeSelect.value !== type) {
        typeSelect.value = type;
        // "all" est le sentinel "pas de filtre" pour ce select (voir templates/history.html) -
        // pas _syncFilterControlActive générique, qui traiterait "all" comme une valeur active.
        typeSelect.classList.toggle('has-value', type !== 'all');
    }
    renderHistoryEvents();
}

function filterHistoryByStatus(value) {
    currentHistoryStatusFilter = value || '';
    renderHistoryEvents();
}

// HISTORY_TYPE_ICONS/HISTORY_TYPE_LABELS: voir history-shared.js (chargé avant ce
// fichier) - "type c'est l'icône pas besoin de mettre de texte" pour le rappel du choix
// d'icône seule dans la colonne Type.

// Tri par en-tête cliquable ("tous les tableaux doivent pouvoir etre ordonné en cliquant
// sur leur header") - column: null revient à l'ordre par défaut (date décroissante, déjà
// appliqué à allHistoryEvents dans loadHistoryEvents).
let historySort = { column: null, direction: 'asc' };

function _historySortValue(e, column) {
    switch (column) {
        case 'date': return e.date ? (parseDbUtcDate(e.date)?.getTime() ?? 0) : 0;
        case 'type': return (HISTORY_TYPE_LABELS[e.type] || e.type || '').toLowerCase();
        case 'label': return (e.label || '').toLowerCase();
        case 'status': return e.success ? 1 : 0;
        default: return 0;
    }
}

function _compareHistoryEvents(a, b, column, direction) {
    const va = _historySortValue(a, column);
    const vb = _historySortValue(b, column);
    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    return direction === 'asc' ? cmp : -cmp;
}

function _updateHistorySortIndicators() {
    document.querySelectorAll('#history-events-wrapper [data-sort-column]').forEach(th => {
        const active = th.dataset.sortColumn === historySort.column;
        const arrow = th.querySelector('.history-sort-arrow');
        if (arrow) arrow.textContent = active ? (historySort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
        th.style.color = active ? 'var(--color-accent)' : '';
        th.style.fontWeight = active ? '600' : '';
    });
}

function setHistorySort(column) {
    if (historySort.column === column) {
        historySort.direction = historySort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        historySort.column = column;
        historySort.direction = 'asc';
    }
    _updateHistorySortIndicators();
    renderHistoryEvents();
}

function renderHistoryEvents() {
    let events = currentHistoryFilter === 'all'
        ? allHistoryEvents
        : currentHistoryFilter === 'search'
            ? allHistoryEvents.filter(e => e.type === 'search' || e.type === 'auto_acquire')
            : currentHistoryFilter === 'delete'
                ? allHistoryEvents.filter(e => e.type === 'delete' || e.type === 'delete_volume')
                : allHistoryEvents.filter(e => e.type === currentHistoryFilter);
    if (currentHistorySearchText.trim()) {
        const needle = _historyNormalizeForSearch(currentHistorySearchText.trim());
        events = events.filter(e =>
            _historyNormalizeForSearch(e.label).includes(needle) || _historyNormalizeForSearch(e.detail).includes(needle)
        );
    }
    if (currentHistoryStatusFilter) {
        events = events.filter(e => historyEventStatus(e) === currentHistoryStatusFilter);
    }
    if (historySort.column) {
        events = [...events].sort((a, b) => _compareHistoryEvents(a, b, historySort.column, historySort.direction));
    }

    const body = document.getElementById('history-events-body');
    const empty = document.getElementById('history-events-empty');

    if (events.length === 0) {
        body.innerHTML = '';
        empty.style.display = 'block';
        return;
    }
    empty.style.display = 'none';

    // historyEventRowHtml: voir history-shared.js - gabarit de ligne partagé avec le
    // widget de /import (même code, "widget history and /history should have the same
    // UI"). idPrefix par défaut ('history-row') laissé tel quel ici.
    body.innerHTML = events.map((e, index) => historyEventRowHtml(e, index)).join('');
}

// toggleImportFiles/toggleActionDetail: voir history-shared.js - partagées avec le widget
// de /import et la fiche historique d'une série (openSeriesHistoryModal, library.js).
// importHistoryVolumeLabel/escapeHtmlHistory/escapeAttrHistory: voir history-shared.js.

loadHistoryEvents();
// "/history is never refreshed" (délibéré, pas un oubli) - contrairement au widget
// "Historique récent" de /import (voir setInterval + rechargement après import dans
// import.js), CETTE page ne se recharge JAMAIS toute seule: c'est la vue "consultation
// complète" avec tri/filtre, où un rechargement en tâche de fond referme silencieusement
// tout détail déplié et réinitialise le tri en cours - gênant sur une table qu'on est en
// train d'explorer, contrairement au widget (10 lignes, jamais trié/filtré). Seul le
// bouton "Actualiser" manuel recharge cette page.
