/**
 * Gestion de la surveillance des volumes manquants
 */

// Variables globales
let currentSeriesData = [];
let selectedLibraries = [];
let seriesSortDirection = "asc";

document.addEventListener('DOMContentLoaded', function() {
    try {
        loadLibraries();
    } catch (e) {
        console.error('❌ Erreur loadLibraries:', e);
    }

    // Événements de recherche avec vérifications
    const seriesSearch = document.getElementById('series-search');
    if (seriesSearch) {
        seriesSearch.addEventListener('input', filterSeries);
    } else {
        console.warn('⚠️ series-search element not found');
    }

    document.getElementById("series-sort-button")?.addEventListener("click", () => {
        seriesSortDirection = seriesSortDirection === "asc" ? "desc" : "asc";
        const indicator = document.querySelector("#series-sort-button .sort-indicator");
        if (indicator) indicator.textContent = seriesSortDirection === "asc" ? "↑" : "↓";
        filterSeries();
    });

    const missingVolumesFilter = document.getElementById("missing-volumes-filter");
    if (missingVolumesFilter) missingVolumesFilter.addEventListener("change", filterSeries);

    const completeFilter = document.getElementById("series-complete-filter");
    if (completeFilter) completeFilter.addEventListener("change", filterSeries);

    const monitoredStatusFilter = document.getElementById("monitored-status-filter");
    if (monitoredStatusFilter) monitoredStatusFilter.addEventListener("change", filterSeries);

    const seriesStatusFilter = document.getElementById("series-status-filter");
    if (seriesStatusFilter) {
        seriesStatusFilter.addEventListener('change', filterSeries);
    } else {
        console.warn('⚠️ series-status-filter element not found');
    }

    const syncSelection = checked => {
        document.querySelectorAll('.series-selection').forEach(input => { input.checked = checked; });
        ['select-all-series', 'select-all-series-header'].forEach(id => {
            const control = document.getElementById(id);
            if (control) control.checked = checked;
        });
        updateMonitorBulkActionsBar();
    };
    document.getElementById('select-all-series-header')?.addEventListener('change', event => syncSelection(event.target.checked));
});

function updateMonitorBulkActionsBar() {
    const bar = document.getElementById('monitor-bulk-actions');
    const countEl = document.getElementById('monitor-bulk-actions-count');
    if (!bar) return;
    const count = document.querySelectorAll('.series-selection:checked').length;
    bar.style.display = count > 0 ? 'flex' : 'none';
    if (countEl) countEl.textContent = count > 0 ? `${count} série${count > 1 ? 's' : ''} sélectionnée${count > 1 ? 's' : ''}` : '';
}

// ========== LIBRARIES ==========

async function loadLibraries() {
    try {
        const response = await fetch('/api/missing-monitor/libraries');
        const data = await response.json();
        
        if (data.success) {
            // Récupérer les bibliothèques sélectionnées
            selectedLibraries = data.libraries
                .filter(lib => lib.monitored === 1)
                .map(lib => lib.id);
            
            // Charger les séries des bibliothèques sélectionnées
            await loadSeriesForSelectedLibraries();
        }
    } catch (error) {
        console.error('Erreur chargement bibliothèques:', error);
    }
}

async function loadSeriesForSelectedLibraries() {
    if (selectedLibraries.length === 0) {
        currentSeriesData = [];
        filterSeries();
        return;
    }
    
    try {
        let allSeries = [];
        
        for (const libraryId of selectedLibraries) {
            const response = await fetch(`/api/missing-monitor/libraries/${libraryId}/series`);
            const data = await response.json();
            
            if (data.success) {
                allSeries = allSeries.concat(data.series);
            }
        }
        
        currentSeriesData = allSeries;
        filterSeries();
    } catch (error) {
        console.error('Erreur chargement séries:', error);
    }
}

// ========== MONITORED SERIES ==========

async function loadMonitoredSeries() {
    // Fonction héritée - maintenant remplacée par loadLibraries() et selectiveloading
    // Garder pour la compatibilité
    await loadLibraries();
}

function parseMissingVolumes(value) {
    if (Array.isArray(value)) return value;
    if (typeof value === "string") {
        try { return JSON.parse(value); } catch (_) { return []; }
    }
    return [];
}

function isBedethequeFinished(status) {
    const normalized = (status || "").toLowerCase();
    return normalized.includes("termin") || normalized.includes("fini");
}

function filterSeries() {
    const searchTerm = navNormalizeSearch(document.getElementById('series-search').value);
    const statusFilter = document.getElementById('series-status-filter').value;
    const monitoredFilter = document.getElementById("monitored-status-filter").value;
    const missingFilter = document.getElementById("missing-volumes-filter").value;
    const completeFilter = document.getElementById("series-complete-filter").value;
    
    let filtered = currentSeriesData;
    
    // Filtre par recherche
    if (searchTerm) {
        filtered = filtered.filter(s => 
            navNormalizeSearch(s.title).includes(searchTerm)
        );
    }
    
    // Filtre par statut (manquant/complète)
    if (statusFilter === "finished") {
        filtered = filtered.filter(s => isBedethequeFinished(s.bedetheque_status));
    } else if (statusFilter === "ongoing") {
        filtered = filtered.filter(s => !isBedethequeFinished(s.bedetheque_status));
    }
    
    if (missingFilter === "with-missing") {
        filtered = filtered.filter(s => Array.isArray(parseMissingVolumes(s.missing_volumes)) && parseMissingVolumes(s.missing_volumes).length > 0);
    } else if (missingFilter === "without-missing") {
        filtered = filtered.filter(s => !Array.isArray(parseMissingVolumes(s.missing_volumes)) || parseMissingVolumes(s.missing_volumes).length === 0);
    }

    if (completeFilter === "yes") {
        filtered = filtered.filter(s => s.bedetheque_complete === 1 || s.bedetheque_complete === true);
    } else if (completeFilter === "no") {
        filtered = filtered.filter(s => s.bedetheque_complete === 0 || s.bedetheque_complete === false || s.bedetheque_complete == null);
    }

    // Filtre par statut monitored
    if (monitoredFilter === 'monitored') {
        filtered = filtered.filter(s => s.enabled !== 0);
    } else if (monitoredFilter === 'not-monitored') {
        filtered = filtered.filter(s => s.enabled === 0);
    }
    
    filtered = [...filtered].sort((a, b) => {
        const comparison = (a.title || "").localeCompare(b.title || "", "fr", { sensitivity: "base" });
        return seriesSortDirection === "asc" ? comparison : -comparison;
    });

    displaySeriesGrid(filtered);
}

function displaySeriesGrid(series) {
    const tableBody = document.getElementById("series-table-body");

    if (series.length === 0) {
        tableBody.innerHTML = "<tr><td colspan=\"6\" class=\"no-data\">Aucune série trouvée</td></tr>";
        return;
    }

    const rows = series.map(s => {
        // Parser missing_volumes si c'est une chaîne JSON
        let missingVols = s.missing_volumes;
        if (typeof missingVols === 'string') {
            try {
                missingVols = JSON.parse(missingVols);
            } catch (e) {
                missingVols = [];
            }
        }

        // Une série complète peut être couverte par une intégrale : ses trous de
        // numérotation ne doivent alors pas être présentés comme des tomes à récupérer.
        const effectiveMissingVols = s.bedetheque_complete ? [] : missingVols;
        const missingVolsStr = Array.isArray(effectiveMissingVols) ? effectiveMissingVols.join(', ') : '';
        const missingCount = Array.isArray(effectiveMissingVols) ? effectiveMissingVols.length : 0;
        const isMonitored = s.enabled !== 0; // 0 = non suivi, 1 = suivi
        const statusIcon = isMonitored ? svgIcon("check") : svgIcon("x");
        const statusClass = isMonitored ? "monitor-status-monitored" : "monitor-status-not-monitored";

        return `
            <tr class="monitor-series-row">
                <td class="monitor-select-cell"><input type="checkbox" class="series-selection" value="${s.id}" aria-label="Sélectionner ${escapeHtml(s.title)}" onchange="updateMonitorBulkActionsBar()"></td>
                <td class="monitor-series-title"><a href="/series/${s.id}">${escapeHtml(s.title)}</a></td>
                <td class="${missingCount > 0 ? 'series-missing' : ''}">${missingCount > 0 ? `${missingCount} ${missingCount === 1 ? 'tome' : 'tomes'} → ${escapeHtml(missingVolsStr)}` : '—'}</td>
                <td><span class="bedetheque-status ${isBedethequeFinished(s.bedetheque_status) ? 'bedetheque-status-finished' : 'bedetheque-status-ongoing'}">${isBedethequeFinished(s.bedetheque_status) ? 'Terminé' : 'Série en cours'}</span></td>
                <td><span class="series-complete-status">${s.bedetheque_complete ? 'Oui' : 'Non'}</span></td>
                <td><span class="monitor-status-icon ${statusClass}" title="${isMonitored ? 'Suivie' : 'Non suivie'}">${statusIcon}</span></td>
            </tr>
        `;
    }).join('');
    tableBody.innerHTML = rows;
}

async function bulkSetMonitoring(enabled) {
    const ids = Array.from(document.querySelectorAll(".series-selection:checked")).map(input => Number(input.value));
    if (!ids.length) {
        showToast('monitor-bulk-set', "Sélectionnez au moins une série", { icon: 'info', autoHideMs: 3000 });
        return;
    }
    showToast('monitor-bulk-set', `${enabled ? "Activation" : "Désactivation"} de la surveillance pour ${ids.length} série(s)…`, { icon: 'loader-circle' });
    try {
        const results = await Promise.all(ids.map(id => fetch(`/api/missing-monitor/series/${id}/monitor`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled })
        }).then(response => response.json())));
        const failed = results.filter(result => !result.success);
        if (failed.length) throw new Error(failed[0].error || "Erreur de mise à jour");
        showToast('monitor-bulk-set', `Surveillance ${enabled ? "activée" : "désactivée"} pour ${ids.length} série(s)`, { icon: 'check', autoHideMs: 4000 });
        await loadMonitoredSeries();
        // loadMonitoredSeries reconstruit tout le corps du tableau (cases décochées par
        // défaut) mais ne touche pas le bandeau lui-même (élément séparé) - sans cet
        // appel, il restait affiché avec le compteur de la sélection précédente malgré
        // des cases redevenues vides.
        updateMonitorBulkActionsBar();
    } catch (error) {
        showToast('monitor-bulk-set', "Erreur : " + error.message, { icon: 'triangle-alert', autoHideMs: 5000 });
    }
}

// ========== UTILITIES ==========

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}
