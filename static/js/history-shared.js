// Rendu partagé de l'historique des imports - utilisé à la fois par le widget
// "Historique récent des imports" (/import, voir import.js) et le tableau général
// (/history, voir history.js). "widget history and /history should have the same UI...
// no need to do double code for the same things" - avant ce fichier, chacune des deux
// pages avait sa propre version légèrement divergente (ordre des colonnes, calcul du
// résumé/statut, affichage - ou non - du message d'erreur par fichier) qui divergeait un
// peu plus à chaque correctif appliqué à une seule des deux copies. Toute la logique
// commune à une LIGNE "import" (résumé, statut, détail par fichier) vit ici ; chaque page
// ne garde que ce qui lui est propre : history.js pour download/rename/delete + le
// mélange des sources + le tri/filtre, import.js pour le polling et le déclenchement du
// widget après un import.
//
// Chargé sur les deux pages AVANT history.js/import.js (voir templates/history.html et
// templates/import.html).

const HISTORY_TYPE_ICONS = {
    import: 'package', download: 'download', rename: 'pencil', delete: 'trash-2', delete_volume: 'trash-2',
    renumber_volume: 'tag', convert: 'refresh-cw', merge: 'git-merge', move_volume: 'git-merge',
    add_series: 'plus', auto_acquire: 'radar', search: 'search', quality_upgrade: 'sparkles',
    missing_volume_match: 'eye'
};
const HISTORY_TYPE_LABELS = {
    import: 'Import', download: 'Téléchargement', rename: 'Renommage', delete: 'Suppression',
    delete_volume: 'Suppression de tome', renumber_volume: 'Changement de numéro',
    convert: 'Conversion', merge: 'Fusion', move_volume: 'Déplacement de tome',
    add_series: 'Ajout de série', auto_acquire: 'Recherche automatique', search: 'Recherche manuelle',
    quality_upgrade: 'Upgrade qualité', missing_volume_match: 'Tome manquant détecté'
};

const HISTORY_SOURCE_LABELS = { ebdz: 'EBDZ', prowlarr: 'Prowlarr', telegram: 'Telegram', fourtoutici: 'fourtoutici' };
const HISTORY_SOURCE_ICONS = {
    ebdz: '/static/img/ebdz-logo.png', prowlarr: '/static/img/prowlarr-logo.svg',
    telegram: '/static/img/telegram-logo.svg', fourtoutici: '/static/img/fourtoutici-logo.svg'
};

function safeHistorySourceLink(sourceLink) {
    if (!sourceLink) return '';
    try {
        const parsed = new URL(String(sourceLink), window.location.origin);
        return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : '';
    } catch (_) {
        return '';
    }
}

function historySourceBadgeHtml(source, sourceLink) {
    const label = HISTORY_SOURCE_LABELS[source];
    if (!label) return '';
    const icon = `<img src="${HISTORY_SOURCE_ICONS[source]}" alt="${label}" style="width:14px; height:14px; vertical-align:-2px; object-fit:contain;">`;
    const safeLink = safeHistorySourceLink(sourceLink);
    return safeLink
        ? `<a href="${escapeHtmlHistory(safeLink)}" target="_blank" rel="noopener" data-tooltip="${label} - voir la source">${icon}</a>`
        : `<span data-tooltip="${label}">${icon}</span>`;
}
// Distinction manuel/auto - plus fine que HISTORY_TYPE_ICONS/LABELS ci-dessus (qui ne
// connaissent que le type générique 'import'), portée par le libellé de la ligne plutôt
// que par l'icône de colonne Type (partagée avec tous les autres types d'événements).
const IMPORT_OPERATION_TYPE_LABELS = { manual_import: 'Import manuel', auto_import: 'Import automatique' };
const IMPORT_HISTORY_ACTION_LABELS = {
    imported: '📥 Importé', replaced: '🔄 Remplacé', skipped: '⏭️ Ignoré',
    failed: '❌ Échec', undone: '↩️ Annulé', processing: '⏳ En cours'
};

// Nom de fichier réel (EBDZ) toujours %-encodé en base (voir _title_from_ed2k_link
// côté emule/routes.py, decodeFilename déjà dupliquée côté search.js/library.js) - le
// libellé "Téléchargement" ci-dessous (mapHistoryResponsesToEvents) affichait h.title
// brut ("...%20...", parfois même "&amp;" pour un fichier scrapé avant la correction du
// scraper EBDZ) faute d'appeler cette fonction, contrairement à toutes les autres pages
// listant des résultats EBDZ. Copie locale plutôt que dépendance à search.js/library.js:
// history-shared.js est aussi chargé seul sur /history et /import (templates
// history.html/import.html), qui ne chargent ni l'un ni l'autre.
function decodeFilename(filename) {
    if (!filename) return filename;
    let decoded = filename;
    try {
        decoded = decodeURIComponent(decoded);
    } catch (e) {
        // %-encoding invalide - on garde la chaîne telle quelle
    }
    // Entités HTML restantes sur un nom scrapé avant la correction du scraper EBDZ
    // (extract_ed2k_links n'appliquait pas encore html.unescape sur le HTML brut).
    return decoded.replace(/&amp;|&lt;|&gt;|&quot;|&#0?39;|&apos;/g, m => ({
        '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#039;': "'", '&apos;': "'"
    }[m]));
}

function escapeHtmlHistory(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}
function escapeAttrHistory(text) {
    return (text == null ? '' : String(text)).replace(/'/g, "\\'");
}

function importHistoryVolumeLabel(f) {
    if (f.is_integral) return `Intégrale${f.integral_number != null ? ' ' + f.integral_number : ''}`;
    if (f.is_hs) return `Hors-série${f.hs_number != null ? ' ' + f.hs_number : ''}`;
    if (f.is_episode) return `Épisode${f.episode_number != null ? ' ' + f.episode_number : ''}`;
    if (f.volume_number != null) return `Tome ${f.volume_number}`;
    return '';
}

function importHistorySummary(h) {
    if (h.status === 'started') {
        return h.file_names ? `⏳ Import en cours : ${h.file_names}` : '⏳ Import en cours...';
    }
    const totalFiles = (h.files_imported || 0) + (h.files_replaced || 0) + (h.files_skipped || 0) + (h.files_failed || 0);
    // Un seul fichier: l'action qui a un compteur non nul EST le résultat, pas la peine
    // d'énumérer les 3 autres catégories à 0.
    if (totalFiles <= 1) {
        const action = h.files_imported ? 'imported' : h.files_replaced ? 'replaced' : h.files_skipped ? 'skipped' : h.files_failed ? 'failed' : null;
        const label = action ? IMPORT_HISTORY_ACTION_LABELS[action] : 'Aucun fichier';
        return h.file_names ? `${label} : ${h.file_names}` : label;
    }
    const parts = [];
    if (h.files_imported) parts.push(`${h.files_imported} ${pluralize(h.files_imported, 'importé')}`);
    if (h.files_replaced) parts.push(`${h.files_replaced} ${pluralize(h.files_replaced, 'remplacé')}`);
    if (h.files_skipped) parts.push(`${h.files_skipped} ${pluralize(h.files_skipped, 'ignoré')}`);
    if (h.files_failed) parts.push(`${h.files_failed} ${pluralize(h.files_failed, 'échec')}`);
    return parts.join(' · ') || 'Aucun fichier';
}

// Convertit la réponse brute de GET /api/import/history en la forme générique attendue
// par historyEventRowHtml (voir plus bas) - une seule fois, réutilisée par le widget
// (import.js, imports seuls) et la page générale (history.js, mélangée avec
// download/rename/delete).
function mapImportHistoryToEvents(historyList) {
    return (historyList || []).map(h => {
        const totalFiles = (h.files_imported || 0) + (h.files_replaced || 0) + (h.files_skipped || 0) + (h.files_failed || 0);
        return {
            type: 'import',
            operationId: h.operation_id,
            date: h.created_at,
            status: h.status,
            label: IMPORT_OPERATION_TYPE_LABELS[h.operation_type] || h.operation_type || 'Import',
            detail: importHistorySummary(h),
            // Déjà inclus dans detail via importHistorySummary pour un lot mono-fichier
            // ("Importé : nom.cbz") - une ligne séparée serait redondante. Un lot
            // multi-fichiers, lui, n'a que les compteurs dans detail: la liste des noms
            // reste utile en dessous ("voir le nom du volume importé sans avoir à ouvrir
            // le detail").
            fileNames: totalFiles > 1 ? (h.file_names || '') : '',
            success: h.status === 'completed'
        };
    });
}

// Fusionne les 3 sources déjà journalisées séparément (imports, téléchargements,
// renommages/suppressions) en une seule timeline triée - extrait de loadHistoryEvents
// (history.js) pour être réutilisé tel quel par la fiche historique d'une série
// (openSeriesHistoryModal, library.js) plutôt que dupliqué avec une variante légèrement
// différente ("widget history and /history should have the same UI [...] no need to do
// double code for the same things", déjà le principe qui a amené mapImportHistoryToEvents
// ci-dessus dans ce même fichier).
function mapHistoryResponsesToEvents(importResp, downloadResp, actionResp) {
    const imports = mapImportHistoryToEvents(importResp.history);

    const downloads = (downloadResp.history || []).map(h => ({
        type: 'download',
        date: h.created_at,
        seriesId: h.series_id,
        seriesTitle: h.series_title,
        label: decodeFilename(h.title) + (h.volume_number != null ? ` - Tome ${h.volume_number}` : ''),
        detail: `${h.client || ''}${h.message ? ' · ' + h.message : ''}`,
        sourceHtml: historySourceBadgeHtml(h.source, h.source_link),
        success: h.success
    }));

    // Renommages/suppressions (voir action_history.py côté Flask) - series_id peut être
    // null si la série a depuis été supprimée (une suppression ne pointe plus vers
    // rien): pas de lien cliquable dans ce cas, contrairement aux autres lignes.
    const actions = (actionResp.history || []).map(h => ({
        type: h.action_type,
        actionId: h.id,
        date: h.created_at,
        seriesId: h.action_type === 'delete' ? null : h.series_id,
        seriesTitle: h.series_title,
        label: h.series_title || `Série #${h.series_id}`,
        detail: h.error || h.detail || '',
        success: !!h.success
    }));

    return imports.concat(downloads, actions).sort((a, b) => new Date(b.date) - new Date(a.date));
}

// Classification à 3 états d'un événement (pas juste succès/échec) - factorisée hors de
// historyEventRowHtml pour être réutilisée par le filtre Statut de /history (voir
// filterHistoryByStatus, history.js), qui a besoin de la MÊME classification que le
// badge affiché, pas d'une seconde logique divergente. Voir le commentaire original
// (encore visible dans le diff) pour le "started"/success==null.
function historyEventStatus(e) {
    if (e.status === 'started' || e.success == null) return 'progress';
    return e.success ? 'ok' : 'failed';
}

// Ligne de tableau générique pour UN événement d'historique (import/download/rename/
// delete/...) - extrait de history.js, qui l'utilisait déjà de façon totalement générique
// (rien de spécifique à /history dans cette fonction). idPrefix distingue les ids DOM du
// widget (import.js) de ceux de la page générale (history.js) - jamais présents sur la
// même page en même temps, mais gardés distincts pour la lisibilité au débogage.
function historyEventRowHtml(e, index, idPrefix = 'history-row', hideSeriesLink = false) {
    const dateObj = e.date ? parseDbUtcDate(e.date) : null;
    const date = dateObj ? dateObj.toLocaleString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ }) : '-';
    const isToday = dateObj && isSameDisplayDate(dateObj, new Date());
    const dateShort = !dateObj ? '-' : (isToday
        ? dateObj.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', timeZone: BULLARR_DISPLAY_TZ })
        : dateObj.toLocaleDateString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ }));
    const typeLabel = HISTORY_TYPE_LABELS[e.type] || e.type;
    // 3 états (pas juste succès/échec) - un import encore status='started' affichait à
    // tort "✗ Échec" côté /history avant cette unification (e.success n'était jamais vrai
    // pour une opération en cours). "once the download is initiated it should be in the
    // database" - un download logué dès son lancement (mark_download_pending, voir
    // downloader.py) a le même besoin: e.success vaut alors NULL (résultat pas encore
    // connu, pas explicitement faux) tant que log_manual_download n'a pas mis à jour
    // cette même ligne avec le résultat final - sans ce cas, NULL était traité comme
    // faux et affichait à tort "✗ Échec" pour un téléchargement qui vient tout juste de
    // démarrer.
    const status = historyEventStatus(e);
    const statusBadge = status === 'progress'
        ? '<span style="color:#e67e22; font-weight:600;">⏳</span>'
        : status === 'ok'
            ? '<span style="color:#28a745; font-weight:600;">✓ OK</span>'
            : '<span style="color:#dc3545; font-weight:600;">✗ Échec</span>';
    // Seuls les imports et les renommages ont un détail par fichier consultable - les
    // téléchargements n'ont qu'un seul fichier/tome et les suppressions un seul résumé,
    // déjà entièrement décrits par le libellé de la ligne.
    const expandable = e.type === 'import' || e.type === 'rename';
    // Une série supprimée n'a plus de fiche à ouvrir (seriesId volontairement null pour
    // 'delete', voir loadHistoryEvents côté history.js) - un lien reste affiché pour
    // rename/delete_volume, mais pour rename il ouvre la fiche sans déclencher le
    // dépliage de la ligne (stopPropagation), le clic sur la ligne elle-même étant
    // réservé au dépliage du détail.
    // "dans l'historique pas besoin des liens pour la série puisqu'on est deja dans la
    // série" - openSeriesHistoryModal (library.js) passe hideSeriesLink=true: toutes les
    // lignes de cette modale concernent déjà la même série que celle affichée derrière,
    // ni le lien ni le nom de série répété sur chaque ligne n'apportent rien (contrairement
    // à /history et au widget "Historique récent" de /import, qui mélangent plusieurs
    // séries et où ce lien est le seul moyen d'ouvrir la bonne fiche).
    const clickableSeries = e.seriesId != null && !hideSeriesLink;
    const rowId = `${idPrefix}-${index}`;
    const rowOnclick = e.type === 'import' ? `onclick="toggleImportFiles('${rowId}', '${escapeAttrHistory(e.operationId)}', '${escapeAttrHistory(date)}')"`
        : e.type === 'rename' ? `onclick="toggleActionDetail('${rowId}', ${e.actionId}, '${escapeAttrHistory(date)}')"`
        : clickableSeries ? `onclick="window.location.href='/series/${e.seriesId}'"`
        : '';
    const seriesLinkHtml = clickableSeries
        ? `<a href="/series/${encodeURIComponent(e.seriesId)}" data-tooltip="Voir la fiche série" onclick="event.stopPropagation();" style="color:var(--color-accent); text-decoration:underline;">${escapeHtmlHistory(e.seriesTitle || e.label)}</a>`
        : '';
    const labelHtml = hideSeriesLink
        ? escapeHtmlHistory(e.label)
        : (clickableSeries && e.seriesTitle && e.seriesTitle !== e.label
            ? `${seriesLinkHtml}<span style="color:var(--color-text-muted, #777);"> · </span>${escapeHtmlHistory(e.label)}`
            : (clickableSeries ? seriesLinkHtml : escapeHtmlHistory(e.label)));
    const fileNamesHtml = e.fileNames ? `<div style="color:#777; font-size:0.9em;">${escapeHtmlHistory(e.fileNames)}</div>` : '';
    return `
        <tr class="history-shared-row" style="border-bottom:1px solid var(--color-border); font-size:0.85em;${(expandable || clickableSeries) ? ' cursor:pointer;' : ''}" ${rowOnclick}>
            <td style="padding:6px 8px; white-space:nowrap;" data-tooltip="${escapeHtmlHistory(date)}">${escapeHtmlHistory(dateShort)}</td>
            <td style="padding:6px 8px; white-space:nowrap; text-align:center;" data-tooltip="${escapeHtmlHistory(typeLabel)}">${svgIcon(HISTORY_TYPE_ICONS[e.type] || 'tag')}</td>
            <td style="padding:6px 8px; overflow-wrap:break-word; word-break:break-word;">
                <div style="font-weight:600; display:flex; align-items:center; gap:4px;">${expandable ? '▸ ' : ''}${labelHtml}</div>
                <div style="color:#777; font-size:0.9em; white-space:pre-line;">${e.sourceHtml ? e.sourceHtml + ' ' : ''}${escapeHtmlHistory(e.detail)}</div>
                ${fileNamesHtml}
            </td>
            <td style="padding:6px 8px; text-align:center;">${statusBadge}</td>
        </tr>
        ${expandable ? `<tr id="${rowId}-files" style="display:none;"><td colspan="4" style="padding:0 8px 8px 26px; background:var(--color-surface-alt);"></td></tr>` : ''}
    `;
}

// Détail fichier par fichier d'un import (nom, action - importé/remplacé/ignoré/échec -
// et série de destination), chargé à la demande au premier clic sur la ligne puis mis en
// cache (pas besoin de refaire la requête si on replie/déplie plusieurs fois). Partagé
// entre le widget et la page générale (GET /api/import/history/<id> identique dans les
// deux cas).
const _importFilesDetailCache = {};

async function toggleImportFiles(rowId, operationId, fullDate) {
    const detailRow = document.getElementById(`${rowId}-files`);
    if (!detailRow) return;

    if (detailRow.style.display !== 'none') {
        detailRow.style.display = 'none';
        if (typeof _openImportHistoryOperationIds !== 'undefined') _openImportHistoryOperationIds.delete(operationId);
        return;
    }
    if (typeof _openImportHistoryOperationIds !== 'undefined') _openImportHistoryOperationIds.add(operationId);

    const cell = detailRow.querySelector('td');
    detailRow.style.display = 'table-row';
    const dateHtml = fullDate ? `<div style="padding:6px 8px 2px; color:#777; font-size:0.85em;">🕐 ${escapeHtmlHistory(fullDate)}</div>` : '';

    if (_importFilesDetailCache[operationId]) {
        cell.innerHTML = dateHtml + _importFilesDetailCache[operationId];
        return;
    }

    cell.innerHTML = dateHtml + '<div style="padding:8px; color:#666;">⏳ Chargement des fichiers...</div>';
    try {
        const response = await fetch(`/api/import/history/${encodeURIComponent(operationId)}`);
        const data = await response.json();
        const files = (data.details && data.details.files) || [];
        const operation = (data.details && data.details.operation) || {};

        const shortFailureDetail = (operation.details || '').split(' : ').slice(0, 2).join(' : ');
        const failureMessage = operation.status === 'failed' && operation.details
            ? `<div style="color:#c0392b; font-size:0.9em;">${escapeHtmlHistory(shortFailureDetail)}</div>`
            : '';
        const filesHtml = files.length === 0
            ? (failureMessage || '<div style="padding:8px; color:#666;">Aucun fichier détaillé pour cet import</div>')
            : `<table class="history-files-detail-table">
                <tbody>
                    ${files.map(f => `
                        <tr style="border-bottom:1px solid #eee;">
                            <td class="history-file-name">
                                ${escapeHtmlHistory(f.filename || '')}
                                ${f.message ? `<div style="${f.action === 'failed' ? 'color:#c0392b;' : 'color:#888;'} font-size:0.9em;">${f.action === 'failed' ? '⚠️' : '🔄'} ${escapeHtmlHistory(f.message)}</div>` : ''}
                            </td>
                            <td class="history-file-series">${f.series_id ? `<a href="/series/${f.series_id}">${escapeHtmlHistory(f.series_title || '')}</a>` : escapeHtmlHistory(f.series_title || '')}</td>
                            <td class="history-file-volume">${escapeHtmlHistory(importHistoryVolumeLabel(f))}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`;

        _importFilesDetailCache[operationId] = filesHtml;
        cell.innerHTML = dateHtml + filesHtml;
    } catch (error) {
        cell.innerHTML = dateHtml + '<div style="padding:8px; color:#c0392b;">Erreur lors du chargement des fichiers</div>';
    }
}

// Détail fichier par fichier d'un renommage (ancien nom -> nouveau nom, plus le dossier
// de la série s'il a lui aussi été renommé) - même principe de cache que
// toggleImportFiles ci-dessus, voir GET /api/actions/history/<id> côté Flask
// (action_history.py, colonne files_json posée par _log_rename_action). Déplacée ici
// depuis history.js (déjà partagée par le widget /import) pour être réutilisable par
// openSeriesHistoryModal (library.js) sans dupliquer cette logique une 3e fois.
const actionDetailCache = {};

async function toggleActionDetail(rowId, actionId, fullDate) {
    const detailRow = document.getElementById(`${rowId}-files`);
    if (!detailRow) return;

    if (detailRow.style.display !== 'none') {
        detailRow.style.display = 'none';
        return;
    }

    const cell = detailRow.querySelector('td');
    detailRow.style.display = 'table-row';
    // Voir toggleImportFiles - même besoin d'heure complète visible sans hover sur mobile.
    const dateHtml = fullDate ? `<div style="padding:6px 8px 2px; color:#777; font-size:0.85em;">🕐 ${escapeHtmlHistory(fullDate)}</div>` : '';

    if (actionDetailCache[actionId]) {
        cell.innerHTML = dateHtml + actionDetailCache[actionId];
        return;
    }

    cell.innerHTML = dateHtml + '<div style="padding:8px; color:#666;">⏳ Chargement du détail...</div>';
    try {
        const response = await fetch(`/api/actions/history/${actionId}`);
        const data = await response.json();
        const files = (data.action && data.action.files) || [];
        const folder = data.action && data.action.folder;

        const rows = [];
        if (folder && folder.changed) {
            rows.push(`
                <tr style="border-top:1px solid #eee;">
                    <td style="padding:6px 8px; white-space:nowrap;">${folder.success ? '✅ Dossier' : '❌ Dossier'}</td>
                    <td style="padding:6px 8px; color:#777;" colspan="2">${escapeHtmlHistory((folder.old_path || '').split('/').pop())} → ${escapeHtmlHistory((folder.new_path || '').split('/').pop())}${!folder.success && folder.error ? ` (${escapeHtmlHistory(folder.error)})` : ''}</td>
                </tr>
            `);
        }
        files.filter(f => f.changed !== false).forEach(f => {
            const label = f.success ? '✅ Fichier' : '❌ Fichier';
            rows.push(`
                <tr style="border-top:1px solid #eee;">
                    <td style="padding:6px 8px; white-space:nowrap;">${label}</td>
                    <td style="padding:6px 8px; color:#777;" colspan="2">${escapeHtmlHistory(f.old_name || '')} → ${escapeHtmlHistory(f.new_name || '')}${!f.success && f.error ? ` (${escapeHtmlHistory(f.error)})` : ''}</td>
                </tr>
            `);
        });

        const detailHtml = rows.length === 0
            ? '<div style="padding:8px; color:#666;">Aucun détail disponible pour ce renommage</div>'
            : `<table style="width:100%; border-collapse:collapse; font-size:13px;"><tbody>${rows.join('')}</tbody></table>`;

        actionDetailCache[actionId] = detailHtml;
        cell.innerHTML = dateHtml + detailHtml;
    } catch (error) {
        cell.innerHTML = dateHtml + '<div style="padding:8px; color:#dc3545;">Erreur lors du chargement du détail</div>';
    }
}
