// ===== UTILITAIRES PARTAGÉS (pas d'autre script chargé sur cette page qui les fournisse,
// à part match-modal.js) =====
function escapeHtml(text) {
    if (!text) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

function escapeForAttribute(text) {
    return text.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ===== VÉRIFICATION DE LA BIBLIOTHÈQUE (métadonnées manquantes + nommage + fichiers
// invalides + volumes possédés non rattachés + doublons), une catégorie à la fois =====
// "au lieu d'avoir toutes les verifications lancés en meme temps. groupe par different
// types de verification et on peut cliquer dans chacune d'une" - remplace l'ancien
// runVerification() unique (calculait/affichait les 4 catégories d'un coup à chaque appel,
// y compris la plus lente - fichiers invalides, test d'intégrité + décompression - avant
// de pouvoir montrer quoi que ce soit) par runVerificationCategory(type) ci-dessous,
// déclenché indépendamment par le bouton "Lancer" de chaque carte. Chaque appel ne
// (re)calcule QUE cette catégorie (?type=..., voir _verify_* côté blueprints/settings/
// routes.py) - un clic sur "Métadonnées manquantes" n'attend plus le test d'intégrité.
const VERIFICATION_CATEGORIES = {
    missing_metadata: { runBtnId: 'verifRunMissingMetadataBtn', render: renderMissingMetadata },
    misnamed: { runBtnId: 'verifRunMisnamedBtn', render: renderMisnamed },
    misplaced_folders: { runBtnId: 'verifRunMisplacedFoldersBtn', render: renderMisplacedFolders },
    invalid_files: { runBtnId: 'verifRunInvalidFilesBtn', render: renderInvalidFiles },
    unmatched_owned_volumes: { runBtnId: 'verifRunUnmatchedOwnedBtn', render: renderUnmatchedOwnedVolumes },
    unmatched_owned_komga: { runBtnId: 'verifRunUnmatchedOwnedKomgaBtn', render: renderUnmatchedOwnedKomga },
    duplicate_series: { runBtnId: 'verifRunDuplicateSeriesBtn', render: renderDuplicateSeries },
};

function _updateVerificationSummary(data) {
    const summaryEl = document.getElementById('verificationSummary');
    // series_count/volume_count sont renvoyés par CHAQUE appel ?type=... (voir
    // _load_verification_series_and_volumes côté Flask, partagé par les 4 catégories) -
    // n'importe laquelle lancée en premier suffit à remplir ce résumé. "met le à jour en
    // fonction des actions" - _updateVerificationSummary est rappelée après CHAQUE action
    // corrective (chaque handler relance runVerificationCategory du même type), ce texte
    // reflète donc toujours l'état actuel, pas un instantané figé au premier "Lancer".
    summaryEl.textContent = `${data.series_count} ${pluralize(data.series_count, 'série')}, ${data.volume_count} ${pluralize(data.volume_count, 'tome')} dans la bibliothèque.`;
}

async function runVerificationCategory(type) {
    const config = VERIFICATION_CATEGORIES[type];
    const btn = document.getElementById(config.runBtnId);
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="btn-icon">${svgIcon('loader-circle', 'icon-spin')}</span>`;

    try {
        const response = await fetch(`/api/settings/verification?type=${type}`);
        const data = await response.json();

        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            return;
        }

        _updateVerificationSummary(data);
        config.render(data);
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
    }
}

let verifMetaItems = [];
function renderMissingMetadata(data) {
    verifMetaItems = data.missing_metadata;
    const metaList = document.getElementById('missingMetadataList');
    document.getElementById('missingMetadataCount').textContent = verifMetaItems.length;
    if (verifMetaItems.length === 0) {
        metaList.innerHTML = `<p class="help-text">${svgIcon('check')} Rien à signaler.</p>`;
    } else {
        const query = window.verifMetaTitleFilter || '';
        const groups = [];
        const groupsBySeriesId = {};
        verifMetaItems.forEach((item, i) => {
            let group = groupsBySeriesId[item.series_id];
            if (!group) {
                group = { seriesId: item.series_id, seriesTitle: item.series_title, indices: [] };
                groupsBySeriesId[item.series_id] = group;
                groups.push(group);
            }
            group.indices.push(i);
        });
        metaList.innerHTML = `
            <table class="series-table series-table-compact">
                <thead>
                    <tr>
                        <th class="volume-table-select-cell"></th>
                        <th><div class="th-filterable-row"><span class="th-filterable-label">Série</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(query)}" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterVerifMetaTable(this.value)"></span></div></th>
                        <th>Détail</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    ${groups.map(g => {
                        const groupKey = `g${g.seriesId}`;
                        if (g.indices.length === 1) {
                            return _verifMetaItemRowHtml(verifMetaItems[g.indices[0]], g.indices[0]);
                        }
                        const volumeCount = g.indices.filter(i => verifMetaItems[i].type !== 'series').length;
                        const hasSeriesEntry = volumeCount !== g.indices.length;
                        const countLabel = hasSeriesEntry
                            ? `(série non matchée${volumeCount ? ` + ${volumeCount} ${pluralize(volumeCount, 'tome')}` : ''})`
                            : `(${g.indices.length} ${pluralize(g.indices.length, 'tome')})`;
                        return `
                            <tr class="series-table-row verif-group-row" data-group="${groupKey}">
                                <td class="volume-table-select-cell"><input type="checkbox" class="verif-meta-group-select" aria-label="Sélectionner tous les tomes de ${escapeHtml(g.seriesTitle)}" onchange="verifToggleMetaGroup(this, '${groupKey}')"></td>
                                <td colspan="3">
                                    <button type="button" class="verif-section-toggle verif-group-toggle" onclick="verifToggleGroupRows('${groupKey}', this)" data-tooltip="Déplier/replier" style="border:none; background:transparent; cursor:pointer; padding:2px; display:inline-flex; vertical-align:-5px; color:inherit; transition:transform 0.15s ease; transform:rotate(-90deg);"><svg class="icon" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6" /></svg></button>
                                    <a href="/series/${g.seriesId}" class="missing-series-link">${escapeHtml(g.seriesTitle)}</a>
                                    <span class="help-text">${countLabel}</span>
                                </td>
                            </tr>
                            ${g.indices.map(i => _verifMetaItemRowHtml(verifMetaItems[i], i, groupKey, true)).join('')}
                        `;
                    }).join('')}
                </tbody>
            </table>
        `;
        filterVerifMetaTable(query);
        initClearableSearchInputs(metaList);
    }
    const selectAllEl = document.getElementById('verifMetaSelectAll');
    selectAllEl.checked = false;
    selectAllEl.disabled = false;
    document.getElementById('verifMetaBulkControls').style.display = verifMetaItems.length > 0 ? 'flex' : 'none';
    verifUpdateMetaSelectionCount();
}

// Ligne d'un item individuel - utilisée à la fois pour une série à un seul item (montrée
// directement, pas de groupe) et pour chaque ligne de détail d'un groupe replié
// (groupKey/isDetail posés uniquement dans ce second cas: data-group pour le toggle en
// masse, display:none par défaut - repliée tant qu'on n'a pas cliqué le chevron).
function _verifMetaItemRowHtml(item, i, groupKey = null, isDetail = false) {
    return `
        <tr class="series-table-row"${groupKey ? ` data-group="${groupKey}" style="display:none;"` : ''} id="verif-meta-${item.type}-${item.volume_id || item.series_id}">
            <td class="volume-table-select-cell"><input type="checkbox" class="verif-meta-select" data-index="${i}" data-series-id="${item.series_id}" data-volume-id="${item.volume_id ?? ''}" data-type="${item.type}" data-title="${escapeHtml(item.series_title)}" aria-label="Sélectionner ${escapeHtml(item.series_title)}" onchange="verifUpdateMetaSelectionCount()"></td>
            <td>${isDetail ? `<span class="help-text" style="padding-left:20px;">${item.filename ? escapeHtml(item.filename) : escapeHtml(item.series_title)}</span>` : `<a href="/series/${item.series_id}" class="missing-series-link">${escapeHtml(item.series_title)}</a>`}</td>
            <td>${!isDetail && item.filename ? `<div class="help-text">${escapeHtml(item.filename)}</div>` : ''}<div class="help-text">${escapeHtml(item.reason)}</div></td>
            <!-- "gros bouton matcher, maj hyper moche. met une icône plus sympa a la
                 place" - icône seule (.btn-icon-only), même gabarit que les boutons
                 Bédéthèque/EBDZ de la table "Matching incorrect" plus bas, au lieu
                 du bouton texte+logo pleine largeur. -->
            <td>${item.type === 'series'
                ? `<button class="btn-icon-only" onclick="verifMatchSeries(${item.series_id}, '${escapeForAttribute(item.series_title)}', this)" data-tooltip="Matcher sur Bédéthèque" aria-label="Matcher sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="" style="width:18px;height:18px;object-fit:contain;"></button>`
                : `<button class="btn-icon-only" onclick="verifUpdateMetadata(${item.series_id}, ${item.volume_id}, '${escapeForAttribute(item.series_title)}', this)" data-tooltip="MAJ métadonnées de ce tome" aria-label="MAJ métadonnées de ce tome">${svgIcon('tag')}</button>`}</td>
        </tr>
    `;
}

// Case à cocher de groupe: sélectionne/désélectionne tous les .verif-meta-select de ce
// groupe, sans forcer à déplier d'abord - une correction groupée doit rester possible
// même repliée (on sait déjà ce que ça va corriger, pas besoin de le voir en détail).
function verifToggleMetaGroup(checkboxEl, groupKey) {
    document.querySelectorAll(`#missingMetadataList tr[data-group="${groupKey}"] .verif-meta-select`).forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateMetaSelectionCount();
}

// Une ligne de détail (data-group posé, voir _verifMetaItemRowHtml) n'a pas son propre
// .missing-series-link - elle suit l'état filtré de sa ligne d'en-tête de groupe plutôt
// que d'être testée individuellement.
function filterVerifMetaTable(query) {
    if (query !== undefined) window.verifMetaTitleFilter = String(query || '');
    const needle = (window.verifMetaTitleFilter || '').trim().toLowerCase();
    document.querySelectorAll('#missingMetadataList tbody tr').forEach(row => {
        if (row.hasAttribute('data-group') && !row.classList.contains('verif-group-row')) return;
        const title = row.querySelector('.missing-series-link')?.textContent?.toLowerCase() || '';
        const match = !needle || title.includes(needle);
        row.hidden = !match;
        if (row.classList.contains('verif-group-row')) {
            document.querySelectorAll(`#missingMetadataList tr[data-group="${row.dataset.group}"]`).forEach(r => { r.hidden = !match; });
        }
    });
}

// Ligne d'un item individuel (voir _verifMetaItemRowHtml pour le même principe côté
// Métadonnées manquantes) - groupKey/isDetail posés uniquement pour une ligne de détail
// repliée dans un groupe multi-tomes.
function _verifRenameItemRowHtml(item, groupKey = null, isDetail = false) {
    return `
        <tr class="series-table-row"${groupKey ? ` data-group="${groupKey}" style="display:none;"` : ''} id="verif-rename-${item.is_folder ? 'folder' : 'volume'}-${item.volume_id || item.series_id}">
            <td class="volume-table-select-cell"><input type="checkbox" class="verif-rename-select" data-series-id="${item.series_id}" data-volume-id="${item.volume_id ?? ''}" data-is-folder="${item.is_folder ? '1' : ''}" data-title="${escapeHtml(item.series_title)}" aria-label="Sélectionner ${escapeHtml(item.series_title)}" onchange="verifUpdateRenameSelectionCount()"></td>
            <td>${isDetail
                ? `<span class="help-text" style="padding-left:20px;">${item.is_folder ? 'Dossier de la série' : escapeHtml(item.current_name)}</span>`
                : `<a href="/series/${item.series_id}" class="missing-series-link">${escapeHtml(item.series_title)}</a>${item.is_folder ? '<span class="help-text"> (dossier)</span>' : ''}`}</td>
            <td><code>${escapeHtml(item.current_name)}</code></td>
            <td><code>${escapeHtml(item.expected_name)}</code></td>
            <td><button class="btn-icon-only" onclick="verifRenameItem(${item.series_id}, ${item.volume_id ?? 'null'}, ${item.is_folder ? 'true' : 'false'}, this)" data-tooltip="Renommer" aria-label="Renommer">${svgIcon('pencil')}</button></td>
        </tr>
    `;
}


function _folderPlacementRowHtml(item) {
    const action = item.can_reconcile
        ? `<button class="btn-icon-only" onclick="verifReconcileFolder(${item.series_id}, this)" data-tooltip="Déplacer vers le dossier attendu" aria-label="Déplacer vers le dossier attendu">${svgIcon('folder')}</button>`
        : '<span class="help-text">Conflit à résoudre</span>';
    return `<tr class="series-table-row">
        <td class="volume-table-select-cell">${item.can_reconcile ? `<input type="checkbox" class="verif-folder-move-select" data-series-id="${item.series_id}" aria-label="Sélectionner ${escapeHtml(item.series_title)}" onchange="verifUpdateFolderMoveSelectionCount()">` : ''}</td>
        <td><a href="/series/${item.series_id}" class="missing-series-link">${escapeHtml(item.series_title)}</a></td>
        <td><code>${escapeHtml(item.current_path || '')}</code></td>
        <td>${item.expected_path ? `<code>${escapeHtml(item.expected_path)}</code>` : '—'}</td>
        <td>${escapeHtml(item.reason)}</td>
        <td>${action}</td>
    </tr>`;
}

function renderMisplacedFolders(data) {
    const list = document.getElementById('misplacedFoldersList');
    const items = data.misplaced_folders || [];
    document.getElementById('misplacedFoldersCount').textContent = items.length;
    if (!items.length) {
        list.innerHTML = '<p class="help-text">Tous les dossiers correspondent au template et à leur univers.</p>';
    } else {
        const universeGroups = new Map();
        items.forEach(item => {
            const universeName = item.universe_name || 'Sans univers';
            if (!universeGroups.has(universeName)) universeGroups.set(universeName, []);
            universeGroups.get(universeName).push(item);
        });
        list.innerHTML = [...universeGroups.entries()]
            .sort(([a], [b]) => a.localeCompare(b, 'fr'))
            .map(([universeName, groupItems], index) => {
                const groupKey = `folder-universe-${index}`;
                const selectableCount = groupItems.filter(item => item.can_reconcile).length;
                return `
                    <details class="verification-series-collapse" style="margin-bottom:8px;">
                        <summary class="verification-series-collapse-title verification-folder-universe-summary" style="justify-content:flex-start; text-align:left;" onclick="event.preventDefault(); const details = this.parentElement; details.open = !details.open;">
                            <svg class="icon verification-folder-universe-chevron" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
                            <span>${escapeHtml(universeName)}</span>
                            <span class="verification-series-collapse-count">${groupItems.length} dossier(s)</span>
                            ${selectableCount ? `<span class="verification-folder-universe-controls"><input type="checkbox" class="verif-folder-move-group-select" data-group="${groupKey}" aria-label="Sélectionner les dossiers de ${escapeHtml(universeName)}" onclick="event.stopPropagation()" onchange="verifToggleFolderMoveGroup(this, '${groupKey}')"></span>` : ''}
                        </summary>
                        <table class="series-table"><thead><tr><th></th><th>Série</th><th>Actuel</th><th>Attendu</th><th>État</th><th>Action</th></tr></thead><tbody>${groupItems.map(item => _folderPlacementRowHtml(item).replace('class="verif-folder-move-select"', `class="verif-folder-move-select" data-group="${groupKey}"`)).join('')}</tbody></table>
                    </details>`;
            }).join('');
    }
    const controls = document.getElementById('verifFolderMoveBulkControls');
    controls.style.display = items.some(item => item.can_reconcile) ? 'flex' : 'none';
    document.getElementById('verifFolderMoveSelectAll').checked = false;
    verifUpdateFolderMoveSelectionCount();
}

async function _verifRequestFolderReconciliation(seriesId) {
    const response = await fetch(`/api/series/${seriesId}/rename/execute`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
    });
    const payload = await response.json();
    if (!response.ok || !payload.success || !payload.folder?.success) {
        throw new Error(payload.error || payload.folder?.error || 'Déplacement impossible');
    }
    return payload;
}

async function verifReconcileFolder(seriesId, buttonEl) {
    buttonEl.disabled = true;
    try {
        await _verifRequestFolderReconciliation(seriesId);
        showToast('Dossier déplacé vers son emplacement attendu.', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        await runVerificationCategory('misplaced_folders');
    }
}

function verifToggleFolderMoveGroup(checkboxEl, groupKey) {
    document.querySelectorAll(`.verif-folder-move-select[data-group="${groupKey}"]`).forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateFolderMoveSelectionCount();
}

function verifToggleSelectAllFolderMoves(checkboxEl) {
    document.querySelectorAll('.verif-folder-move-select').forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateFolderMoveSelectionCount();
}

function verifUpdateFolderMoveSelectionCount() {
    const count = document.querySelectorAll('.verif-folder-move-select:checked').length;
    document.getElementById('verifFolderMoveSelectedCount').textContent = count;
    document.getElementById('verifBulkFolderMoveBtn').disabled = count === 0;
}

async function verifBulkReconcileFolders() {
    const ids = [...document.querySelectorAll('.verif-folder-move-select:checked')].map(cb => Number(cb.dataset.seriesId));
    const button = document.getElementById('verifBulkFolderMoveBtn');
    button.disabled = true;
    let failed = 0;
    for (const seriesId of ids) {
        try { await _verifRequestFolderReconciliation(seriesId); } catch (error) { failed += 1; }
    }
    showToast(failed ? `${ids.length - failed} déplacé(s), ${failed} échec(s).` : `${ids.length} dossier(s) déplacé(s).`, failed ? 'error' : 'success');
    await runVerificationCategory('misplaced_folders');
}

function renderMisnamed(data) {
    const namingList = document.getElementById('misnamedList');
    document.getElementById('misnamedCount').textContent = data.misnamed.length;
    if (data.misnamed.length === 0) {
        namingList.innerHTML = `<p class="help-text">${svgIcon('check')} Rien à signaler.</p>`;
    } else {
        const query = window.verifRenameTitleFilter || '';
        const groups = [];
        const groupsBySeriesId = {};
        data.misnamed.forEach(item => {
            let group = groupsBySeriesId[item.series_id];
            if (!group) {
                group = { seriesId: item.series_id, seriesTitle: item.series_title, items: [] };
                groupsBySeriesId[item.series_id] = group;
                groups.push(group);
            }
            group.items.push(item);
        });
        namingList.innerHTML = `
            <table class="series-table series-table-compact">
                <thead>
                    <tr>
                        <th class="volume-table-select-cell"></th>
                        <th><div class="th-filterable-row"><span class="th-filterable-label">Série</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(query)}" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterVerifRenameTable(this.value)"></span></div></th>
                        <th>Actuel</th>
                        <th>Attendu</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    ${groups.map(g => {
                        if (g.items.length === 1) {
                            return _verifRenameItemRowHtml(g.items[0]);
                        }
                        const groupKey = `g${g.seriesId}`;
                        return `
                            <tr class="series-table-row verif-group-row" data-group="${groupKey}">
                                <td class="volume-table-select-cell"><input type="checkbox" class="verif-rename-group-select" aria-label="Sélectionner tous les éléments de ${escapeHtml(g.seriesTitle)}" onchange="verifToggleRenameGroup(this, '${groupKey}')"></td>
                                <td colspan="4">
                                    <button type="button" class="verif-section-toggle verif-group-toggle" onclick="verifToggleGroupRows('${groupKey}', this)" data-tooltip="Déplier/replier" style="border:none; background:transparent; cursor:pointer; padding:2px; display:inline-flex; vertical-align:-5px; color:inherit; transition:transform 0.15s ease; transform:rotate(-90deg);"><svg class="icon" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6" /></svg></button>
                                    <a href="/series/${g.seriesId}" class="missing-series-link">${escapeHtml(g.seriesTitle)}</a>
                                    <span class="help-text">(${g.items.length} éléments)</span>
                                </td>
                            </tr>
                            ${g.items.map(item => _verifRenameItemRowHtml(item, groupKey, true)).join('')}
                        `;
                    }).join('')}
                </tbody>
            </table>
        `;
        filterVerifRenameTable(query);
        initClearableSearchInputs(namingList);
    }
    const renameSelectAllEl = document.getElementById('verifRenameSelectAll');
    renameSelectAllEl.checked = false;
    renameSelectAllEl.disabled = false;
    document.getElementById('verifRenameBulkControls').style.display = data.misnamed.length > 0 ? 'flex' : 'none';
    verifUpdateRenameSelectionCount();
}

// Toggle générique multi-lignes (voir aussi verifToggleMetaGroupRows) - factorisé ici
// puisque Métadonnées manquantes ET Nommage non standard en ont maintenant tous deux
// besoin, plutôt que deux fonctions identiques à un nom près.
function verifToggleGroupRows(groupKey, btnEl) {
    // La ligne d'en-tête (.verif-group-row) porte aussi data-group (pour la case à cocher
    // "tout sélectionner" et le filtre) - l'exclure du toggle, sinon elle se cache elle-même
    // (avec le bouton du toggle dedans) et le groupe entier disparaît sans moyen de le rouvrir.
    const rows = document.querySelectorAll(`tr[data-group="${groupKey}"]:not(.verif-group-row)`);
    const collapsed = rows.length > 0 && rows[0].style.display === 'none';
    rows.forEach(row => { row.style.display = collapsed ? '' : 'none'; });
    btnEl.style.transform = collapsed ? '' : 'rotate(-90deg)';
}

function verifToggleRenameGroup(checkboxEl, groupKey) {
    document.querySelectorAll(`tr[data-group="${groupKey}"] .verif-rename-select`).forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateRenameSelectionCount();
}

// Une ligne de détail d'un groupe replié (data-group) n'a pas son propre
// .missing-series-link - elle suit l'état filtré de sa ligne d'en-tête (voir aussi
// filterVerifMetaTable, même principe).
function filterVerifRenameTable(query) {
    if (query !== undefined) window.verifRenameTitleFilter = String(query || '');
    const needle = (window.verifRenameTitleFilter || '').trim().toLowerCase();
    document.querySelectorAll('#misnamedList tbody tr').forEach(row => {
        if (row.hasAttribute('data-group') && !row.classList.contains('verif-group-row')) return;
        const title = row.querySelector('.missing-series-link')?.textContent?.toLowerCase() || '';
        const match = !needle || title.includes(needle);
        row.hidden = !match;
        if (row.classList.contains('verif-group-row')) {
            document.querySelectorAll(`#misnamedList tr[data-group="${row.dataset.group}"]`).forEach(r => { r.hidden = !match; });
        }
    });
}

function renderInvalidFiles(data) {
    const invalidList = document.getElementById('invalidFilesList');
    const invalidFiles = data.invalid_files || [];
    document.getElementById('invalidFilesCount').textContent = data.komga_configured ? invalidFiles.length : '—';
    if (!data.komga_configured) {
        invalidList.innerHTML = '<p class="help-text">Komga n\'est pas configuré (Configuration &gt; Komga) - rien à afficher ici.</p>';
        document.getElementById('verifInvalidBulkControls').style.display = 'none';
        return;
    }
    if (invalidFiles.length === 0) {
        invalidList.innerHTML = `<p class="help-text">${svgIcon('check')} Rien à signaler côté Komga.</p>`;
    } else {
        const query = window.verifInvalidTitleFilter || '';
        invalidList.innerHTML = `
            <table class="series-table series-table-compact">
                <thead>
                    <tr>
                        <th class="volume-table-select-cell"></th>
                        <th><div class="th-filterable-row"><span class="th-filterable-label">Série</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(query)}" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterVerifInvalidTable(this.value)"></span></div></th>
                        <th>Fichier</th>
                        <th>Raison</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
                    ${invalidFiles.map(item => `
                        <tr class="series-table-row" id="verif-invalid-${item.volume_id}">
                            <td class="volume-table-select-cell"><input type="checkbox" class="verif-invalid-select" data-volume-id="${item.volume_id}" data-title="${escapeHtml(item.series_title)}" aria-label="Sélectionner ${escapeHtml(item.series_title)}" onchange="verifUpdateInvalidSelectionCount()"></td>
                            <td><a href="/series/${item.series_id}" class="missing-series-link">${escapeHtml(item.series_title)}</a></td>
                            <td>${item.filename ? escapeHtml(item.filename) : '-'}</td>
                            <td class="help-text">${escapeHtml(item.reason)}</td>
                            <td><button class="btn-icon-only btn-icon-danger" onclick="verifDeleteInvalidFile(${item.volume_id}, '${escapeForAttribute(item.series_title)}', this)" data-tooltip="Supprimer le fichier" aria-label="Supprimer le fichier">${svgIcon('trash-2')}</button></td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        `;
        filterVerifInvalidTable(query);
        initClearableSearchInputs(invalidList);
    }
    const selectAllEl = document.getElementById('verifInvalidSelectAll');
    selectAllEl.checked = false;
    selectAllEl.disabled = false;
    document.getElementById('verifInvalidBulkControls').style.display = invalidFiles.length > 0 ? 'flex' : 'none';
    verifUpdateInvalidSelectionCount();
}

function filterVerifInvalidTable(query) {
    if (query !== undefined) window.verifInvalidTitleFilter = String(query || '');
    const needle = (window.verifInvalidTitleFilter || '').trim().toLowerCase();
    document.querySelectorAll('#invalidFilesList tbody tr').forEach(row => {
        const title = row.querySelector('.missing-series-link')?.textContent?.toLowerCase() || '';
        row.hidden = !!needle && !title.includes(needle);
    });
}

function verifToggleSelectAllInvalid(checkboxEl) {
    document.querySelectorAll('.verif-invalid-select').forEach(cb => { if (!cb.closest('tr').hidden) cb.checked = checkboxEl.checked; });
    verifUpdateInvalidSelectionCount();
}

function verifUpdateInvalidSelectionCount() {
    const count = document.querySelectorAll('.verif-invalid-select:checked').length;
    document.getElementById('verifInvalidSelectedCount').textContent = count;
    document.getElementById('verifBulkDeleteInvalidBtn').disabled = count === 0;
}

async function verifBulkDeleteInvalidFiles() {
    const selected = [...document.querySelectorAll('.verif-invalid-select:checked')];
    if (!selected.length) return;
    if (!confirm(`Supprimer définitivement ${selected.length} fichier(s) invalide(s) ? Cette action est irréversible.`)) return;

    const btn = document.getElementById('verifBulkDeleteInvalidBtn');
    btn.disabled = true;
    const toastId = 'verif-bulk-delete-invalid';
    const failed = [];
    for (let i = 0; i < selected.length; i++) {
        const cb = selected[i];
        const volumeId = cb.dataset.volumeId;
        const title = cb.dataset.title;
        showToast(toastId, `Suppression ${i + 1}/${selected.length}: ${title}...`);
        try {
            const response = await fetch(`/api/volumes/${volumeId}`, { method: 'DELETE' });
            const data = await response.json();
            if (!data.success) failed.push(title);
        } catch (error) {
            failed.push(title);
        }
    }
    dismissToast(toastId);
    if (failed.length > 0) {
        alert(`⚠️ Suppression terminée avec ${failed.length} ${pluralize(failed.length, 'échec')} sur ${selected.length}: ${failed.join(', ')}.`);
    }
    runVerificationCategory('invalid_files');
}

// "c'est pas clair... ca doit etre fichier non detecté de bedetheque. ca regarde si le
// volume est bien matché avec une entrée bedetheque" - remplace l'ancienne carte
// "doublons de tomes placeholder" (déjà nettoyée manuellement, plus rien à surveiller
// dans la durée). Une ligne par TOME possédé sans lien Bédéthèque.
// "solution is to MAJ metadonnées. mais il faut juste ajouter le lien bedetheque. pas
// besoin de tout mettre à jour" - _verifRequestLinkVolume (POST .../link-volume/<id>,
// pas update-metadata/volume) n'écrit QUE le champ Web, jamais le reste du ComicInfo
// (déjà correct sur un tome possédé - inutile de tout recalculer/écraser).
async function _verifRequestLinkVolume(volumeId) {
    try {
        const response = await fetch(`/api/bedetheque/link-volume/${volumeId}`, { method: 'POST' });
        const data = await response.json();
        return data.success ? { success: true } : { success: false, error: data.error || 'Erreur inconnue' };
    } catch (error) {
        return { success: false, error: error.message };
    }
}

async function verifLinkVolume(volumeId, seriesId, seriesTitle, buttonEl) {
    const button = buttonEl;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';
    const toastId = `verif-link-${volumeId}`;
    showToast(toastId, `Rattachement Bédéthèque: ${seriesTitle}...`, { href: `/series/${seriesId}` });

    const result = await _verifRequestLinkVolume(volumeId);
    dismissToast(toastId);

    if (result.success) {
        button.innerHTML = svgIcon('check');
        runVerificationCategory('unmatched_owned_volumes');
    } else {
        button.disabled = false;
        button.innerHTML = originalHtml;
        alert('❌ Erreur: ' + result.error);
    }
}

function renderUnmatchedOwnedVolumes(data) {
    const list = document.getElementById('unmatchedOwnedList');
    const items = data.unmatched_owned_volumes || [];
    document.getElementById('unmatchedOwnedCount').textContent = items.length;
    if (items.length === 0) {
        list.innerHTML = `<p class="help-text">${svgIcon('check')} Tous les tomes possédés sont rattachés à un album Bédéthèque.</p>`;
        document.getElementById('verifUnmatchedOwnedBulkControls').style.display = 'none';
        return;
    }
    const query = window.verifUnmatchedOwnedTitleFilter || '';
    list.innerHTML = `
        <table class="series-table series-table-compact unmatched-owned-table">
            <thead>
                <tr>
                    <th class="volume-table-select-cell"><input type="checkbox" id="verifUnmatchedOwnedSelectAll" aria-label="Sélectionner tous les tomes visibles" onchange="verifToggleSelectAllUnmatchedOwned(this)"></th>
                    <th><div class="th-filterable-row"><span class="th-filterable-label">Série</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(query)}" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterVerifUnmatchedOwnedTable(this.value)"></span></div></th>
                    <th>Fichier</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                ${items.map(item => `
                    <tr class="series-table-row" id="verif-unmatched-owned-${item.volume_id}">
                        <td class="volume-table-select-cell"><input type="checkbox" class="verif-unmatched-owned-select" data-series-id="${item.series_id}" data-volume-id="${item.volume_id}" data-title="${escapeHtml(item.series_title)}" aria-label="Sélectionner ${escapeHtml(item.series_title)}" onchange="verifUpdateUnmatchedOwnedSelectionCount()"></td>
                        <td><a href="/series/${item.series_id}" class="missing-series-link">${escapeHtml(item.series_title)}</a></td>
                        <td><span class="help-text">${escapeHtml(item.filename)}</span></td>
                        <td><button class="btn-icon-only" onclick="verifLinkVolume(${item.volume_id}, ${item.series_id}, '${escapeForAttribute(item.series_title)}', this)" data-tooltip="Rattacher à son album Bédéthèque" aria-label="Rattacher à son album Bédéthèque">${svgIcon('link')}</button></td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
    filterVerifUnmatchedOwnedTable(query);
    initClearableSearchInputs(list);
    const selectAllEl = document.getElementById('verifUnmatchedOwnedSelectAll');
    selectAllEl.checked = false;
    selectAllEl.disabled = false;
    document.getElementById('verifUnmatchedOwnedBulkControls').style.display = 'flex';
    verifUpdateUnmatchedOwnedSelectionCount();
}

const verificationKomgaManualSeriesIds = new Set();

function renderUnmatchedOwnedKomga(data) {
    const card = document.getElementById('verificationUnmatchedOwnedKomgaCard');
    const list = document.getElementById('unmatchedOwnedKomgaList');
    if (!data.komga_configured) {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';
    const items = data.unmatched_owned_komga || [];
    document.getElementById('unmatchedOwnedKomgaCount').textContent = items.length;
    document.getElementById('verifKomgaSelectAll').checked = false;
    document.getElementById('verifKomgaSelectAll').disabled = items.length === 0;
    verifUpdateKomgaSelectionCount();
    if (items.length === 0) {
        list.innerHTML = `<p class="help-text">${svgIcon('check')} Tous les tomes possédés sont liés à Komga.</p>`;
        return;
    }
    const bySeries = new Map();
    items.forEach(item => {
        if (!bySeries.has(item.series_id)) {
            bySeries.set(item.series_id, { title: item.series_title, items: [] });
        }
        bySeries.get(item.series_id).items.push(item);
    });
    list.innerHTML = Array.from(bySeries.entries()).map(([seriesId, series]) => `
        <div class="verification-series-group">
        <div class="verification-series-header">
        <details class="verification-series-collapse">
            <summary>
                <span class="verification-series-collapse-title">
                    <input type="checkbox" class="verif-komga-series-checkbox" data-series-id="${seriesId}" onchange="verifToggleKomgaSeries(this)" onclick="event.stopPropagation()" aria-label="Sélectionner la série ${escapeHtml(series.title)}">
                    <a href="/series/${seriesId}" class="missing-series-link" onclick="event.stopPropagation()">${escapeHtml(series.title)}</a>
                    <span class="verification-series-collapse-count">${series.items.length} ${pluralize(series.items.length, 'fichier')}</span>
                </span>
            </summary>
            <table class="series-table series-table-compact unmatched-owned-komga-table">
                <thead><tr><th>Fichier</th></tr></thead>
                <tbody>${series.items.map(item => `
                    <tr class="series-table-row">
                        <td><input type="checkbox" class="verif-komga-select" data-series-id="${seriesId}" data-series-title="${escapeForAttribute(series.title)}" onchange="verifUpdateKomgaSelectionCount()" aria-label="Sélectionner ${escapeHtml(item.filename || '')}"> <span class="verification-komga-filename">${escapeHtml(item.filename || '')}</span></td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </details>
        <div class="verification-series-actions">
            <button class="btn btn-sm verif-komga-manual-match" type="button" style="display:${verificationKomgaManualSeriesIds.has(Number(seriesId)) ? 'inline-flex' : 'none'};" onclick="openVerificationKomgaMatcher(${seriesId}, '${escapeForAttribute(series.title)}')">${svgIcon('search')} Matcher manuellement</button>
            <button class="btn btn-sm" type="button" onclick="repairUnmatchedKomgaSeries(${seriesId}, this)">${svgIcon('refresh-cw')} Réparer</button>
        </div>
        </div>
        </div>`).join('');
}

function verifToggleKomgaSeries(checkboxEl) {
    const details = checkboxEl.closest('details');
    details.querySelectorAll('.verif-komga-select').forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateKomgaSelectionCount();
}

function verifToggleSelectAllKomga(checkboxEl) {
    document.querySelectorAll('.verif-komga-select').forEach(cb => { cb.checked = checkboxEl.checked; });
    document.querySelectorAll('.verif-komga-series-select-all').forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateKomgaSelectionCount();
}

function verifUpdateKomgaSelectionCount() {
    const selected = document.querySelectorAll('.verif-komga-select:checked');
    const count = selected.length;
    const countEl = document.getElementById('verifKomgaSelectedCount');
    const button = document.getElementById('verifBulkRepairKomgaBtn');
    if (countEl) countEl.textContent = count;
    if (button) button.disabled = count === 0;
    const all = document.querySelectorAll('.verif-komga-select');
    const master = document.getElementById('verifKomgaSelectAll');
    if (master) {
        master.checked = all.length > 0 && count === all.length;
        master.indeterminate = count > 0 && count < all.length;
    }
    document.querySelectorAll('.verif-komga-series-checkbox').forEach(seriesCheckbox => {
        const details = seriesCheckbox.closest('details');
        const seriesFiles = details ? details.querySelectorAll('.verif-komga-select') : [];
        const seriesSelected = details ? details.querySelectorAll('.verif-komga-select:checked') : [];
        seriesCheckbox.checked = seriesFiles.length > 0 && seriesSelected.length === seriesFiles.length;
        seriesCheckbox.indeterminate = seriesSelected.length > 0 && seriesSelected.length < seriesFiles.length;
    });
}

async function verifBulkRepairKomga() {
    const selected = [...document.querySelectorAll('.verif-komga-select:checked')];
    const seriesIds = [...new Set(selected.map(cb => Number(cb.dataset.seriesId)))];
    if (!seriesIds.length) return;
    if (!confirm(`Réparer les liens Komga de ${seriesIds.length} série(s) sélectionnée(s) ?`)) return;
    const button = document.getElementById('verifBulkRepairKomgaBtn');
    button.disabled = true;
    const failed = [];
    for (const seriesId of seriesIds) {
        try {
            const response = await fetch(`/api/series/${seriesId}/komga-enrich`, { method: 'POST' });
            const data = await response.json();
            if (!data.success || data.match_status === 'unmatched') failed.push(seriesId);
        } catch (error) {
            failed.push(seriesId);
        }
    }
    await runVerificationCategory('unmatched_owned_komga');
    if (failed.length) alert(`⚠️ ${failed.length} série(s) n'ont pas pu être réparée(s). Utilisez le matching manuel.`);
}

async function openVerificationKomgaMatcher(seriesId, seriesTitle) {
    const modal = document.getElementById('komga-match-modal');
    const body = document.getElementById('komga-match-modal-body');
    modal.dataset.seriesId = seriesId;
    modal.classList.add('active');
    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/komga-logo.svg',
        title: 'Matcher manuellement sur Komga',
        queryId: 'verification-komga-match-query',
        queryPlaceholder: 'Titre de la série Komga',
        prefillValue: seriesTitle,
        resultsId: 'verification-komga-match-results',
        searchOnclick: `searchVerificationKomgaCandidates(${seriesId})`,
        autoSearch: true,
    });
    wireMatchModalEnterKeys('verification-komga-match-query', () => searchVerificationKomgaCandidates(seriesId));
    await searchVerificationKomgaCandidates(seriesId);
}

async function searchVerificationKomgaCandidates(seriesId) {
    const query = document.getElementById('verification-komga-match-query').value.trim();
    const results = document.getElementById('verification-komga-match-results');
    results.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const response = await fetch(`/api/series/${seriesId}/komga-candidates?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        if (!data.success) { results.innerHTML = matchModalErrorHtml(data.error || 'Erreur inconnue'); return; }
        if (!data.candidates.length) { results.innerHTML = matchModalNoResultsHtml('Aucune série Komga trouvée'); return; }
        results.innerHTML = data.candidates.map(candidate => `
            <button type="button" class="series-card match-candidate-card" style="width:100%; text-align:left; cursor:pointer; margin-bottom:8px;" onclick="selectVerificationKomgaCandidate(${seriesId}, '${escapeForAttribute(candidate.komga_series_id)}')">
                <div class="series-title">${escapeHtml(candidate.title || '(sans titre)')}</div>
                <div class="series-info">${candidate.total_volumes ?? '?'} tomes${candidate.status ? ` · ${escapeHtml(candidate.status)}` : ''}</div>
            </button>`).join('');
    } catch (error) {
        results.innerHTML = matchModalErrorHtml(error.message);
    }
}

async function selectVerificationKomgaCandidate(seriesId, komgaSeriesId) {
    try {
        const response = await fetch(`/api/series/${seriesId}/komga-match`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ komga_series_id: komgaSeriesId })
        });
        const data = await response.json();
        if (!data.success) { alert('❌ ' + (data.error || 'Matching Komga impossible')); return; }
        document.getElementById('komga-match-modal').classList.remove('active');
        // Cette modale appartient à la table « Matching incorrect », pas à la
        // catégorie « Tomes possédés sans lien Komga ». Rafraîchir la ligne réelle
        // conserve les filtres actifs et reflète immédiatement le match choisi.
        _refreshMissingRowOrRemove(seriesId);
    } catch (error) {
        alert('❌ Erreur pendant le matching Komga');
    }
}

async function repairUnmatchedKomgaSeries(seriesId, button) {
    const originalHtml = button.innerHTML;
    const seriesGroup = button.closest('.verification-series-group');
    const seriesTitle = seriesGroup?.querySelector('.missing-series-link')?.textContent.trim() || '';
    let repairFailed = false;
    button.disabled = true;
    button.innerHTML = `<span class="btn-icon">${svgIcon('loader-circle', 'icon-spin')}</span>`;
    try {
        const response = await fetch(`/api/series/${seriesId}/komga-enrich`, { method: 'POST' });
        const data = await response.json();
        if (!data.success) {
            verificationKomgaManualSeriesIds.add(Number(seriesId));
            repairFailed = true;
            alert('❌ ' + (data.error || 'Impossible de réparer le lien Komga'));
        } else if (data.match_status === 'unmatched') {
            verificationKomgaManualSeriesIds.add(Number(seriesId));
            repairFailed = true;
            alert('⚠️ Aucun match Komga unique trouvé pour cette série.');
        } else {
            verificationKomgaManualSeriesIds.delete(Number(seriesId));
        }
        await runVerificationCategory('unmatched_owned_komga');
        if (repairFailed) await openVerificationKomgaMatcher(seriesId, seriesTitle);
    } catch (error) {
        verificationKomgaManualSeriesIds.add(Number(seriesId));
        alert('❌ Erreur pendant la réparation Komga');
    } finally {
        button.disabled = false;
        button.innerHTML = originalHtml;
    }
}

function renderDuplicateSeries(data) {
    const list = document.getElementById('duplicateSeriesList');
    const items = data.duplicate_series || [];
    document.getElementById('duplicateSeriesCount').textContent = items.length;
    const cleanupBtn = document.getElementById('verifCleanupDuplicateSeriesBtn');
    const cleanupItems = items.filter(item => item.cleanup_eligible);
    document.getElementById('verifCleanupDuplicateSeriesCount').textContent = 0;
    cleanupBtn.disabled = cleanupItems.length === 0;
    if (items.length === 0) {
        list.innerHTML = `<p class="help-text">${svgIcon('check')} Aucun doublon de série détecté.</p>`;
        return;
    }
    list.innerHTML = `
        <table class="series-table series-table-compact duplicate-series-table">
            <thead><tr><th><input type="checkbox" id="verifDuplicateSelectAll" aria-label="Sélectionner tous les doublons nettoyables" onchange="verifToggleSelectAllDuplicate(this)"> Série en doublon</th><th>Correspondance</th><th>Identifiant Komga</th></tr></thead>
            <tbody>
                ${items.map(item => `
                    <tr class="series-table-row">
                        <td><div class="duplicate-series-entry"><strong>Doublon :</strong> ${escapeHtml(item.duplicate_series_title)}<span class="help-text duplicate-series-meta">Sélectionnez la série à supprimer</span>${(item.deletable_series || []).map(series => `<label class="duplicate-series-entry"><input type="checkbox" class="verif-duplicate-select" value="${series.id}" aria-label="Supprimer ${escapeHtml(series.title)}" onchange="verifUpdateDuplicateSelectionCount()"> <a href="/series/${series.id}" class="missing-series-link">${escapeHtml(series.title)}</a><span class="help-text duplicate-series-meta">${series.volume_count != null ? `${series.volume_count} tomes` : 'nombre de tomes inconnu'}</span><span class="help-text duplicate-series-path">${escapeHtml(series.path || '')}</span></label>`).join('')}</div></td>
                        <td>${(item.populated_series || []).map((series, index) => `<div class="duplicate-series-entry"><strong>${index + 1} :</strong> <a href="/series/${series.id}" class="missing-series-link">${escapeHtml(series.title)}</a><span class="help-text duplicate-series-meta">${series.volume_count} fichiers</span><span class="help-text duplicate-series-path">${escapeHtml(series.path || '')}</span></div>`).join('')}${(item.komga_series || []).map((series, index) => `<div class="duplicate-series-entry"><strong>Komga ${index + 1} :</strong> <a href="${escapeHtml(series.url || '#')}" target="_blank" rel="noopener" class="missing-series-link">${escapeHtml(series.title || series.id || 'Série')}</a><span class=\"help-text duplicate-series-meta\">${series.volume_count != null ? `${series.volume_count} tomes` : 'nombre de tomes inconnu'}</span></div>`).join('')}</td>
                        <td><span class="help-text">${escapeHtml(item.komga_series_id)}</span></td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
    verifUpdateDuplicateSelectionCount();
}

function duplicateSeriesNumber(item) {
    const index = (item.populated_series || []).findIndex(series => series.id === item.duplicate_series_id);
    return index >= 0 ? index + 1 : '?';
}

async function cleanupDuplicateSeries() {
    const selectedCheckboxes = [...document.querySelectorAll('.verif-duplicate-select:checked')];
    const selected = selectedCheckboxes.map(cb => Number(cb.value));
    if (!selected.length) return;
    const selectedDescription = selectedCheckboxes.map(cb => {
        const row = cb.closest('tr');
        const title = row?.querySelector('.missing-series-link')?.textContent?.trim() || `ID ${cb.value}`;
        const itemNumber = row?.querySelector('.duplicate-series-meta')?.textContent?.match(/n°(\d+)/)?.[1] || '?';
        return `- n°${itemNumber} : ${title}`;
    }).join('\n');
    if (!confirm(`Les séries suivantes seront supprimées avec leur dossier et leurs fichiers :\n\n${selectedDescription}\n\nConfirmer la suppression ?`)) return;
    const button = document.getElementById('verifCleanupDuplicateSeriesBtn');
    button.disabled = true;
    try {
        const response = await fetch('/api/settings/verification/duplicate-series/cleanup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ series_ids: selected }) });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        alert(`${data.removed.length} fiche(s) supprimée(s).${data.skipped.length ? ` ${data.skipped.length} ignorée(s).` : ''}`);
        runVerificationCategory('duplicate_series');
    } catch (error) {
        alert('❌ Erreur lors du nettoyage: ' + error.message);
        button.disabled = false;
    }
}

function verifToggleSelectAllDuplicate(checkboxEl) {
    document.querySelectorAll('.verif-duplicate-select').forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateDuplicateSelectionCount();
}

function verifUpdateDuplicateSelectionCount() {
    const count = document.querySelectorAll('.verif-duplicate-select:checked').length;
    document.getElementById('verifCleanupDuplicateSeriesCount').textContent = count;
    document.getElementById('verifCleanupDuplicateSeriesBtn').disabled = count === 0;
}

function filterVerifUnmatchedOwnedTable(query) {
    if (query !== undefined) window.verifUnmatchedOwnedTitleFilter = String(query || '');
    const needle = (window.verifUnmatchedOwnedTitleFilter || '').trim().toLowerCase();
    document.querySelectorAll('#unmatchedOwnedList tbody tr').forEach(row => {
        const title = row.querySelector('.missing-series-link')?.textContent?.toLowerCase() || '';
        row.hidden = !!needle && !title.includes(needle);
    });
}

function verifToggleSelectAllUnmatchedOwned(checkboxEl) {
    document.querySelectorAll('.verif-unmatched-owned-select').forEach(cb => { if (!cb.closest('tr').hidden) cb.checked = checkboxEl.checked; });
    verifUpdateUnmatchedOwnedSelectionCount();
}

function verifUpdateUnmatchedOwnedSelectionCount() {
    const count = document.querySelectorAll('.verif-unmatched-owned-select:checked').length;
    document.getElementById('verifUnmatchedOwnedSelectedCount').textContent = count;
    document.getElementById('verifBulkUpdateUnmatchedOwnedBtn').disabled = count === 0;
}

async function verifBulkUpdateUnmatchedOwned() {
    const selected = [...document.querySelectorAll('.verif-unmatched-owned-select:checked')];
    if (!selected.length) return;

    const volumeIds = selected.map(cb => Number(cb.dataset.volumeId));

    const btn = document.getElementById('verifBulkUpdateUnmatchedOwnedBtn');
    btn.disabled = true;
    document.getElementById('verifUnmatchedOwnedSelectAll').disabled = true;

    try {
        const response = await fetch('/api/bedetheque/link-volumes-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ volume_ids: volumeIds })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            btn.disabled = false;
            document.getElementById('verifUnmatchedOwnedSelectAll').disabled = false;
            return;
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        btn.disabled = false;
        document.getElementById('verifUnmatchedOwnedSelectAll').disabled = false;
        return;
    }

    showToast('verif-bulk-link-volumes', 'Rattachement Bédéthèque en cours...');
    pollLinkVolumesBatchProgress('verif-bulk-link-volumes', (result) => {
        if (result.failed && result.failed.length > 0) {
            alert(`⚠️ Rattachement terminé avec ${result.failed.length} ${pluralize(result.failed.length, 'échec')} sur ${result.total}: ${result.failed.map(f => f.title).join(', ')}.`);
        }
        runVerificationCategory('unmatched_owned_volumes');
    });
}

async function _verifResumeLinkVolumesBatchIfRunning() {
    try {
        const response = await fetch('/api/bedetheque/link-volumes-batch/progress');
        const data = await response.json();
        if (!data.running) return;
    } catch (error) {
        return;
    }
    pollLinkVolumesBatchProgress('verif-bulk-link-volumes', () => {
        runVerificationCategory('unmatched_owned_volumes');
    });
}

// Supprime le fichier corrompu et sa ligne (voir DELETE /api/volumes/<id> côté Flask,
// même endpoint que le menu ⚙️ d'un tome sur la fiche série) - le tome redevient un
// placeholder manquant (update_series_stats recalcule missing_volumes), donc
// re-téléchargeable normalement via la recherche existante plutôt que de rester
// indéfiniment sur un fichier tronqué.
async function verifDeleteInvalidFile(volumeId, seriesTitle, buttonEl) {
    if (!confirm(`Supprimer définitivement le fichier invalide de "${seriesTitle}" ? Cette action est irréversible.`)) return;

    const button = buttonEl;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';

    try {
        const response = await fetch(`/api/volumes/${volumeId}`, { method: 'DELETE' });
        const data = await response.json();

        if (data.success) {
            button.innerHTML = svgIcon('check');
            runVerificationCategory('invalid_files');
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            button.innerHTML = originalHtml;
            button.disabled = false;
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        button.innerHTML = originalHtml;
        button.disabled = false;
    }
}

// Replie/déplie une section entière ("une icône pour cacher les détails de chaque
// section") - distinct du masquage par ligne (verifHideItem, en attente que la
// validation de fichiers existe - elle existe maintenant, mais les deux mécanismes
// restent utiles indépendamment: masquer UNE ligne déjà traitée vs replier TOUTE une
// section qu'on ne veut pas regarder pour l'instant). Purement visuel, pas persisté.
function verifToggleSection(listId, btnEl) {
    const list = document.getElementById(listId);
    if (!list) return;
    const collapsed = list.style.display === 'none';
    list.style.display = collapsed ? '' : 'none';
    btnEl.style.transform = collapsed ? '' : 'rotate(-90deg)';
}

// Envoie la MAJ métadonnées pour une série et attend qu'elle soit vraiment terminée
// (l'écriture ComicInfo tourne en arrière-plan côté serveur, voir
// _write_series_volumes_metadata_async) - factorisé hors de verifUpdateMetadata pour être
// réutilisé tel quel par la sélection multiple (verifBulkUpdateMetadata), qui a besoin
// d'attendre une série avant de lancer la suivante plutôt que de les bombarder toutes en
// parallèle (Bédéthèque est scrapé avec un rate-limit anti-bot volontaire).
async function _verifRequestUpdateMetadata(seriesId, onProgress) {
    try {
        const response = await fetch(`/api/bedetheque/update-metadata/series/${seriesId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope: 'all' })
        });
        const data = await response.json();

        if (data.success && (data.already_running || data.started)) {
            return await _verifAwaitMetadataWriteProgress(seriesId, onProgress);
        } else if (data.success) {
            return { success: true };
        }
        return { success: false, error: data.error || 'Erreur inconnue' };
    } catch (error) {
        return { success: false, error: error.message };
    }
}

function _verifAwaitMetadataWriteProgress(seriesId, onProgress) {
    return new Promise(resolve => {
        const interval = setInterval(async () => {
            try {
                const response = await fetch(`/api/bedetheque/update-metadata/series/${seriesId}/progress`);
                const data = await response.json();
                if (!data.progress) {
                    clearInterval(interval);
                    resolve({ success: true });
                } else if (onProgress) {
                    onProgress(data.progress);
                }
            } catch (error) {
                clearInterval(interval);
                resolve({ success: false, error: error.message });
            }
        }, 2000);
    });
}

// Requête simple (pas de progression à suivre: un seul tome, une seule page Bédéthèque à
// relire, pas la boucle multi-tomes rate-limitée de _verifRequestUpdateMetadata) - même
// endpoint que le menu ⚙️ d'un tome sur la fiche série (library.js, updateVolumeMetadata).
async function _verifRequestUpdateMetadataVolume(volumeId) {
    try {
        const response = await fetch(`/api/bedetheque/update-metadata/volume/${volumeId}`, { method: 'POST' });
        const data = await response.json();
        return data.success ? { success: true } : { success: false, error: data.error || 'Erreur inconnue' };
    } catch (error) {
        return { success: false, error: error.message };
    }
}

async function verifUpdateMetadata(seriesId, volumeId, seriesTitle, buttonEl) {
    const button = buttonEl;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';
    const toastId = `verif-metadata-${volumeId}`;
    showToast(toastId, `Mise à jour des métadonnées: ${seriesTitle}...`, { href: `/series/${seriesId}` });

    const result = await _verifRequestUpdateMetadataVolume(volumeId);
    dismissToast(toastId);

    if (result.success) {
        button.innerHTML = svgIcon('check');
        runVerificationCategory('missing_metadata');
    } else if (result.error === 'Impossible de trouver la série sur Bedetheque') {
        // Cas rare ici (une ligne "volume" suppose normalement déjà un match ou un lien
        // par tome) - même message que verifOpenBedethequeMatchModal pour rediriger
        button.innerHTML = originalHtml;
        button.disabled = false;
        alert('❌ Aucune correspondance automatique trouvée sur Bédéthèque. Utilisez le bouton "Matcher Bédéthèque" pour cette série.');
    } else {
        button.innerHTML = originalHtml;
        button.disabled = false;
        alert('❌ Erreur: ' + result.error);
    }
}

// Coche/décoche toutes les lignes "MAJ métadonnées" d'un coup (le bouton "Matcher
// Bédéthèque" des séries non matchées n'a pas de case: chaque match doit être confirmé
// individuellement, pas de sélection multiple possible pour cette action-là)
function verifToggleSelectAllMeta(checkboxEl) {
    document.querySelectorAll('.verif-meta-select').forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateMetaSelectionCount();
}

function verifUpdateMetaSelectionCount() {
    const count = document.querySelectorAll('.verif-meta-select:checked').length;
    document.getElementById('verifMetaSelectedCount').textContent = count;
    document.getElementById('verifBulkUpdateBtn').disabled = count === 0;
}

// Auto-match par titre (recherche + meilleur résultat, sans confirmation) puis attend la
// fin de l'écriture ComicInfo qui s'enchaîne - même mécanisme que POST /enrich-batch
// (bouton "Enrichissement par Lot" de /bedetheque-enrich), réutilisé ici pour la
// sélection multiple de lignes "Série non matchée". Contrairement à
// _verifRequestUpdateMetadata (MAJ d'une série DÉJÀ matchée), c'est volontairement une
// recherche floue non confirmée: acceptable ici parce que l'utilisateur choisit
// explicitement de lancer un matching en masse (voir la discussion sur
// update_metadata_series, qui elle ne doit jamais matcher sans confirmation en arrière-plan
// d'une action qui n'est pas un matching) - à utiliser en connaissance de cause sur des
// titres ambigus.
// write_volumes: false - ne fait QUE poser series.bedetheque_url + aligner series.title,
// ne touche à aucun ComicInfo de tome (voir le paramètre côté Flask, POST /enrich/<id>):
// un matching non confirmé (recherche floue, en masse) ne doit jamais réécrire les tomes
// avant que l'utilisateur ait vérifié que le match trouvé est le bon - contrairement au
// scope 'all' de _verifRequestUpdateMetadata, qui lui suppose une série déjà matchée.
async function _verifRequestMatchByTitle(seriesId, title) {
    try {
        const response = await fetch(`/api/bedetheque/enrich/${seriesId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ search_by: 'title', value: title, write_volumes: false })
        });
        const data = await response.json();
        if (!data.success) return { success: false, error: data.error || 'Erreur inconnue' };
        return { success: true };
    } catch (error) {
        return { success: false, error: error.message };
    }
}

async function verifBulkUpdateMetadata() {
    const seriesEntries = new Map();
    const volumeEntries = [];
    for (const cb of document.querySelectorAll('.verif-meta-select:checked')) {
        const seriesId = Number(cb.dataset.seriesId);
        if (cb.dataset.type === 'series') {
            seriesEntries.set(seriesId, { type: 'series', id: seriesId, title: cb.dataset.title });
        } else {
            volumeEntries.push({ type: 'volume', id: Number(cb.dataset.volumeId), seriesId, title: cb.dataset.title });
        }
    }
    const entries = [...seriesEntries.values(), ...volumeEntries.filter(v => !seriesEntries.has(v.seriesId))];
    if (entries.length === 0) return;

    const btn = document.getElementById('verifBulkUpdateBtn');
    btn.disabled = true;
    document.getElementById('verifMetaSelectAll').disabled = true;
    const toastId = 'verif-bulk-metadata';
    const failed = [];

    for (let i = 0; i < entries.length; i++) {
        const entry = entries[i];
        const entrySeriesId = entry.type === 'series' ? entry.id : entry.seriesId;
        showToast(toastId, `Correction ${i + 1}/${entries.length}: ${entry.title}...`, { href: `/series/${entrySeriesId}` });

        if (entry.type === 'series') {
            const autoResult = await _verifRequestMatchByTitle(entry.id, entry.title);
            if (autoResult.success) continue;
            // Recherche automatique infructueuse: bascule sur la modale de matching
            // manuel pour CETTE série avant de continuer - la boucle attend la décision
            // de l'utilisateur (match choisi ou modale fermée sans matcher) plutôt que
            // de la compter comme un échec silencieux
            showToast(toastId, `Correction ${i + 1}/${entries.length}: ${entry.title} - recherche automatique infructueuse, sélection manuelle...`, { href: `/series/${entrySeriesId}` });
            const manualResult = await verifAwaitManualMatch(entry.id, entry.title);
            if (!manualResult.matched) failed.push(entry);
            continue;
        }

        const result = await _verifRequestUpdateMetadataVolume(entry.id);
        if (!result.success) failed.push(entry);
    }

    dismissToast(toastId);

    if (failed.length > 0) {
        alert(`⚠️ Correction terminée avec ${failed.length} ${pluralize(failed.length, 'échec')} sur ${entries.length}: ${failed.map(f => f.title).join(', ')}.`);
    }

    runVerificationCategory('missing_metadata');
}

// Rafraîchissement après un match Bédéthèque réussi (auto ou manuel via la modale) - par
// défaut "Métadonnées manquantes" (seul appelant historique de verifMatchSeries/
// verifConfirmBedethequeMatch), mais réassignable juste avant un appel pour un autre
// appelant qui réutilise ce même flux de matching plutôt que d'en dupliquer un second
// (voir verifMatchingBedethequeButtonClick plus bas - "Matching incorrect", migré depuis
// /bedetheque-enrich, "matching incorrect il faut le deplacer dans verification").
let _verifMatchRefreshCallback = () => runVerificationCategory('missing_metadata');

// ===== MATCHING BEDETHEQUE (pour une ligne "Série non matchée sur Bédéthèque") =====
// Bouton "Matcher" d'une ligne seule: tente d'abord une recherche automatique par titre
// (silencieuse, sans ouvrir de modale) - ce n'est que si elle échoue (résultat ambigu/
// introuvable) qu'on bascule sur la modale de recherche/URL manuelle. Même logique que
// verifBulkUpdateMetadata pour la sélection multiple (voir plus haut), factorisée dans
// verifAwaitManualMatch pour ne pas dupliquer l'ouverture de modale entre les deux flux.
async function verifMatchSeries(seriesId, seriesTitle, buttonEl) {
    const button = buttonEl;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';
    const toastId = `verif-match-${seriesId}`;
    showToast(toastId, `Recherche automatique: ${seriesTitle}...`, { href: `/series/${seriesId}` });

    const autoResult = await _verifRequestMatchByTitle(seriesId, seriesTitle);
    dismissToast(toastId);

    if (autoResult.success) {
        button.innerHTML = svgIcon('check');
        _verifMatchRefreshCallback();
        return;
    }

    // Échec de la recherche automatique (introuvable): la modale gère elle-même le
    // rafraîchissement de la ligne en cas de match confirmé (verifConfirmBedethequeMatch)
    button.innerHTML = originalHtml;
    button.disabled = false;
    await verifAwaitManualMatch(seriesId, seriesTitle);
}

// Version allégée de openBedethequeMatchModal (static/js/library.js): cette page n'a pas
// les globals seriesData/currentSeriesDetail de la fiche série, le titre vient
// directement de l'appelant (déjà connu, pas besoin de le résoudre).
function verifOpenBedethequeMatchModal(seriesId, seriesTitle) {
    const modal = document.getElementById('bedetheque-match-modal');
    const body = document.getElementById('bedetheque-match-modal-body');
    modal.classList.add('active');
    modal.dataset.seriesId = seriesId;

    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/bedetheque-logo.png',
        title: 'Matcher manuellement sur Bedetheque',
        helpText: "Recherche automatique infructueuse pour cette série. Cherchez par titre ou collez directement l'URL de la fiche série Bedetheque.",
        queryId: 'bedetheque-match-query',
        queryPlaceholder: 'Entrez le nom de série ou adresse Bedetheque...',
        prefillValue: seriesTitle,
        resultsId: 'bedetheque-match-results',
        searchOnclick: `verifSearchBedethequeMatchCandidates(${seriesId})`,
        autoSearch: true,
    });

    wireMatchModalEnterKeys('bedetheque-match-query', () => verifSearchBedethequeMatchCandidates(seriesId));
    verifSearchBedethequeMatchCandidates(seriesId);
}

// Résolue par closeBedethequeMatchModal (fermeture, avec ou sans match confirmé) - permet
// à un appelant (verifMatchSeries, verifBulkUpdateMetadata) d'attendre que l'utilisateur
// ait fini d'interagir avec la modale avant de continuer, plutôt que de la traiter comme
// un simple fire-and-forget.
let _verifPendingMatchResolve = null;
let _verifPendingMatchWasSuccessful = false;

function verifAwaitManualMatch(seriesId, seriesTitle) {
    return new Promise(resolve => {
        _verifPendingMatchResolve = resolve;
        verifOpenBedethequeMatchModal(seriesId, seriesTitle);
    });
}

function closeBedethequeMatchModal() {
    document.getElementById('bedetheque-match-modal').classList.remove('active');
    if (_verifPendingMatchResolve) {
        const resolve = _verifPendingMatchResolve;
        const matched = _verifPendingMatchWasSuccessful;
        _verifPendingMatchResolve = null;
        _verifPendingMatchWasSuccessful = false;
        resolve({ matched });
    }
}

async function verifSearchBedethequeMatchCandidates(seriesId) {
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
            `verifConfirmBedethequeMatch(${seriesId}, '${escapeForAttribute(r.url)}')`,
            r.title, escapeHtml(r.genre || ''), index, 'verif-bedetheque-match'
        )).join('');
        loadBedethequeMatchCandidateCovers(data.results, 'verif-bedetheque-match');
    } catch (error) {
        resultsEl.innerHTML = matchModalErrorHtml(error.message);
    }
}

// Enregistre le match choisi (candidat cliqué ou URL collée) - write_volumes: true (par
// défaut), contrairement au matching auto par titre (_verifRequestMatchByTitle, non
// confirmé) et à sa version en masse (verifBulkUpdateMetadata): ici l'utilisateur a
// explicitement choisi CE candidat précis dans les résultats de recherche (ou collé une
// URL exacte), donc le match est confirmé au même titre qu'un matching manuel depuis la
// fiche série (voir openBedethequeMatchModal/confirmBedethequeMatch dans library.js, même
// comportement) - pas de raison de différer l'écriture des tomes dans ce cas précis.
async function verifConfirmBedethequeMatch(seriesId, url) {
    const resultsEl = document.getElementById('bedetheque-match-results');
    resultsEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>Enregistrement du match...</p></div>';

    try {
        const response = await fetch(`/api/bedetheque/enrich/${seriesId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ search_by: 'url', value: url })
        });
        const data = await response.json();

        if (!data.success) {
            alert('❌ ' + (data.error || "Cette URL ne correspond à aucune série Bedetheque"));
            resultsEl.innerHTML = '';
            return;
        }

        _verifPendingMatchWasSuccessful = true;
        closeBedethequeMatchModal();
        _verifMatchRefreshCallback();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Exécute un renommage (fichier ou dossier) - factorisé pour être réutilisé tel quel par
// la sélection multiple (verifBulkRenameItems). volumeId null = renommage du dossier de
// la série (jamais les fichiers qu'il contient, voir execute_rename côté Flask).
async function _verifRequestRename(seriesId, volumeId, isFolder) {
    try {
        const body = isFolder ? {} : { volume_id: volumeId };
        const response = await fetch(`/api/series/${seriesId}/rename/execute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        if (response.ok && data.success !== false) return { success: true };
        return { success: false, error: data.error || 'Erreur inconnue' };
    } catch (error) {
        return { success: false, error: error.message };
    }
}

async function verifRenameItem(seriesId, volumeId, isFolder, buttonEl) {
    const button = buttonEl;
    button.disabled = true;
    button.innerHTML = '⏳';

    const result = await _verifRequestRename(seriesId, volumeId, isFolder);

    if (result.success) {
        // Pas de requestKomgaScan() ici: execute_rename déclenche déjà
        // trigger_scan_async() côté serveur (voir blueprints/library/routes.py)
        button.innerHTML = svgIcon('check');
        runVerificationCategory('misnamed');
    } else {
        button.disabled = false;
        button.innerHTML = `${svgIcon('pencil')} Renommer`;
        alert('❌ Erreur: ' + result.error);
    }
}

// Coche/décoche toutes les lignes de nommage d'un coup
function verifToggleSelectAllRename(checkboxEl) {
    document.querySelectorAll('.verif-rename-select').forEach(cb => { cb.checked = checkboxEl.checked; });
    verifUpdateRenameSelectionCount();
}

function verifUpdateRenameSelectionCount() {
    const count = document.querySelectorAll('.verif-rename-select:checked').length;
    document.getElementById('verifRenameSelectedCount').textContent = count;
    document.getElementById('verifBulkRenameBtn').disabled = count === 0;
}

// Renomme toutes les lignes cochées, une par une (renommage de fichiers sur disque:
// mieux vaut sérialiser plutôt que risquer des accès concurrents sur le même dossier
// série si plusieurs lignes cochées partagent la même série - ex: dossier + un de ses
// tomes tous les deux mal nommés).
async function verifBulkRenameItems() {
    const items = [...document.querySelectorAll('.verif-rename-select:checked')].map(cb => ({
        seriesId: Number(cb.dataset.seriesId),
        volumeId: cb.dataset.volumeId ? Number(cb.dataset.volumeId) : null,
        isFolder: cb.dataset.isFolder === '1',
        title: cb.dataset.title,
    }));
    if (items.length === 0) return;

    const btn = document.getElementById('verifBulkRenameBtn');
    btn.disabled = true;
    document.getElementById('verifRenameSelectAll').disabled = true;
    const toastId = 'verif-bulk-rename';
    const failed = [];

    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        showToast(toastId, `Renommage ${i + 1}/${items.length}: ${item.title}...`, { href: `/series/${item.seriesId}` });
        const result = await _verifRequestRename(item.seriesId, item.volumeId, item.isFolder);
        if (!result.success) failed.push(item);
    }

    dismissToast(toastId);

    if (failed.length > 0) {
        alert(`⚠️ Renommage terminé avec ${failed.length} ${pluralize(failed.length, 'échec')} sur ${items.length}: ${failed.map(f => f.title).join(', ')}.`);
    }

    runVerificationCategory('misnamed');
}

// ===== MATCHING INCORRECT (migré depuis /bedetheque-enrich - "matching incorrect il
// faut le deplacer dans verification") =====
// Contrairement aux autres catégories ci-dessus (un seul appel à GET /api/settings/
// verification?type=X), celle-ci a toujours eu son propre endpoint dédié
// (GET /api/bedetheque/missing-matches, blueprint bedetheque) - conservé tel quel plutôt
// que de le fondre dans le dispatcher générique runVerificationCategory, pour ne pas
// risquer de régresser un comportement déjà en place ailleurs.
//
// Le matching Bédéthèque (bouton "Matcher" d'une ligne) réutilise directement
// verifMatchSeries/verifOpenBedethequeMatchModal ci-dessus (même modale
// #bedetheque-match-modal, déjà présente sur cette page pour "Métadonnées manquantes")
// via verifMatchingBedethequeButtonClick, qui se contente de rediriger
// _verifMatchRefreshCallback vers le rafraîchissement de LA LIGNE concernée plutôt que
// de dupliquer une seconde implémentation quasi identique. Le matching EBDZ, lui, n'a pas
// d'équivalent existant sur cette page (aucune autre catégorie de Vérification n'en a
// besoin) - ses fonctions ci-dessous sont la version déplacée telle quelle depuis
// bedetheque-enrich.js (enrichOpenEbdzMatchModal etc., noms inchangés: aucune collision
// possible puisqu'ils n'existaient pas ici).
let _ebdzConfigured = false;
let _komgaConfigured = false;
let missingSeriesSort = {field: 'title', dir: 1};

async function loadMissingMatches() {
    const container = document.getElementById('missing-table-container');
    const countEl = document.getElementById('missing-count');
    container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const response = await fetch('/api/bedetheque/missing-matches');
        const data = await response.json();

        if (!data.success) {
            container.innerHTML = `<p style="color:#dc3545;">${svgIcon('circle-x')} ${escapeHtml(data.error || 'Erreur inconnue')}</p>`;
            return;
        }

        _ebdzConfigured = !!data.ebdz_configured;
        _komgaConfigured = !!data.komga_configured;
        const series = data.series || [];
        countEl.textContent = `${series.length} ${pluralize(series.length, 'série')} à matcher.`;

        if (series.length === 0) {
            container.innerHTML = `<p class="help-text">${svgIcon('check')} Toutes les séries sont matchées sur les sources configurées.</p>`;
            return;
        }

        container.innerHTML = `
            <table class="series-table series-table-compact">
                <thead>
                    <tr>
                        <th class="volume-table-select-cell"><input type="checkbox" id="missing-series-select-all" aria-label="Sélectionner toutes les séries visibles" onchange="toggleAllMissingSeries(this.checked)"></th>
                        <th class="volume-table-sortable" onclick="sortMissingSeries()"><div class="th-filterable-row"><span class="th-filterable-label">Série</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><input class="series-table-filter-input th-filterable-control" type="text" value="${escapeHtml(window.missingTitleFilter || '')}" placeholder="Filtrer..." aria-label="Filtrer les séries" oninput="_syncFilterControlActive(this); filterMissingSeriesTable(this.value)"></span></div></th>
                        <th class="missing-match-status-cell">${_missingStatusFilterHeaderHtml('bedetheque', 'Bédéthèque')}</th>
                        ${_ebdzConfigured ? `<th class="missing-match-status-cell">${_missingStatusFilterHeaderHtml('ebdz', 'EBDZ')}</th>` : ''}
                        ${_komgaConfigured ? `<th class="missing-match-status-cell">${_missingStatusFilterHeaderHtml('komga', 'Komga')}</th>` : ''}
                    </tr>
                </thead>
                <tbody id="missing-table-body">
                    ${series.map(_missingMatchRowHtml).join('')}
                </tbody>
            </table>
        `;
        // Ré-applique les filtres persistés (Série/Bédéthèque/EBDZ, voir
        // filterMissingSeriesTable) - sans ça, un rechargement (matching effectué,
        // rafraîchissement manuel...) affichait de nouvelles lignes toutes visibles alors
        // que les contrôles de filtre restaurés ci-dessus affichaient encore l'ancienne
        // sélection, un décalage entre ce que montrent les <select>/l'input et ce qui est
        // effectivement filtré.
        filterMissingSeriesTable();
        initClearableSearchInputs(container);
    } catch (error) {
        container.innerHTML = `<p style="color:#dc3545;">${svgIcon('circle-x')} ${escapeHtml(error.message)}</p>`;
    }
}

function _missingStatusFilterHeaderHtml(field, label) {
    const current = (window.missingStatusFilters && window.missingStatusFilters[field]) || '';
    return `<div class="th-filterable-row"><span class="th-filterable-label">${label}</span><span class="th-filterable-filter" onclick="event.stopPropagation()"><span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span><select class="series-table-filter-select th-filterable-control" aria-label="Filtrer par statut ${escapeHtml(label)}" onchange="_syncFilterControlActive(this); window.missingStatusFilters=Object.assign(window.missingStatusFilters||{},{'${field}':this.value}); filterMissingSeriesTable()">
        <option value="">Tous</option>
        <option value="1"${current === '1' ? ' selected' : ''}>✓</option>
        <option value="0"${current === '0' ? ' selected' : ''}>✗</option>
    </select></span></div>`;
}

function sortMissingSeries() {
    missingSeriesSort.dir *= -1;
    const body = document.getElementById('missing-table-body');
    if (!body) return;
    const rows = [...body.querySelectorAll('tr')];
    rows.sort((a, b) => missingSeriesSort.dir * (a.querySelector('.missing-series-link')?.textContent || '').localeCompare(b.querySelector('.missing-series-link')?.textContent || '', 'fr'));
    rows.forEach(row => body.appendChild(row));
    const arrow = document.getElementById('missing-sort-arrow');
    if (arrow) arrow.textContent = missingSeriesSort.dir === 1 ? '↑' : '↓';
}

function _missingMatchRowHtml(s) {
    return `
        <tr class="series-table-row" id="missing-row-${s.id}">
            <td class="volume-table-select-cell"><input type="checkbox" class="missing-series-checkbox" value="${s.id}" aria-label="Sélectionner ${escapeHtml(s.title)}" onchange="syncMissingSeriesSelectAll()"></td>
            <td><a class="missing-series-link" href="/series/${s.id}">${escapeHtml(s.title)}</a></td>
            <td class="missing-match-status-cell" data-source="bedetheque" data-matched="${s.bedetheque_matched ? '1' : '0'}">${s.bedetheque_matched
                ? (s.bedetheque_url ? `<a href="${escapeHtml(s.bedetheque_url)}" target="_blank" rel="noopener" class="btn-icon-only" data-tooltip="Ouvrir sur Bédéthèque" aria-label="Ouvrir sur Bédéthèque"><span style="color:#28a745;">${svgIcon('check')}</span></a>` : `<span style="color:#28a745;">${svgIcon('check')}</span>`)
                : `<button type="button" class="btn-icon-only" data-tooltip="Matcher sur Bédéthèque" aria-label="Matcher sur Bédéthèque" onclick="verifMatchingBedethequeButtonClick(${s.id}, '${escapeForAttribute(s.title)}', this)"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:18px;height:18px;object-fit:contain;"></button>`}</td>
            ${_ebdzConfigured ? `<td class="missing-match-status-cell" data-source="ebdz" data-matched="${s.ebdz_matched ? '1' : '0'}">${s.ebdz_matched
                ? (s.ebdz_thread_url ? `<a href="${escapeHtml(s.ebdz_thread_url)}" target="_blank" rel="noopener" class="btn-icon-only" data-tooltip="Ouvrir le thread EBDZ" aria-label="Ouvrir le thread EBDZ"><span style="color:#28a745;">${svgIcon('check')}</span></a>` : `<span style="color:#28a745;">${svgIcon('check')}</span>`)
                : `<button type="button" class="btn-icon-only" data-tooltip="Matcher sur EBDZ" aria-label="Matcher sur EBDZ" onclick="enrichOpenEbdzMatchModal(${s.id}, '${escapeForAttribute(s.title)}')"><img src="/static/img/ebdz-logo.png" alt="EBDZ" style="width:18px;height:18px;object-fit:contain;"></button>`}</td>` : ''}
            ${_komgaConfigured ? `<td class="missing-match-status-cell" data-source="komga" data-matched="${s.komga_matched ? '1' : '0'}">${s.komga_matched
                ? (s.komga_url ? `<a href="${escapeHtml(s.komga_url)}" target="_blank" rel="noopener" class="btn-icon-only" data-tooltip="Ouvrir Komga" aria-label="Ouvrir Komga"><span style="color:#28a745;">${svgIcon('check')}</span></a>` : `<span style="color:#28a745;">${svgIcon('check')}</span>`)
                : `<button type="button" class="btn-icon-only" data-tooltip="Matcher sur Komga" aria-label="Matcher sur Komga" onclick="openVerificationKomgaMatcher(${s.id}, '${escapeForAttribute(s.title)}')"><img src="/static/img/komga-logo.svg" alt="Komga" style="width:18px;height:18px;object-fit:contain;"></button>`}</td>` : ''}
        </tr>
    `;
}

// Retire une ligne de la table une fois les deux sources matchées (sinon un rechargement
// complet de la table à chaque petit match serait plus lent et ferait sauter le scroll)
function _refreshMissingRowOrRemove(seriesId) {
    fetch(`/api/bedetheque/missing-matches`).then(r => r.json()).then(data => {
        const s = (data.series || []).find(x => x.id === seriesId);
        const row = document.getElementById(`missing-row-${seriesId}`);
        if (!row) return;
        if (!s) {
            row.remove();
            const countEl = document.getElementById('missing-count');
            const remaining = document.querySelectorAll('#missing-table-body tr').length;
            countEl.textContent = `${remaining} ${pluralize(remaining, 'série')} à matcher.`;
            if (remaining === 0) loadMissingMatches();
        } else {
            row.outerHTML = _missingMatchRowHtml(s);
            // La ligne recréée doit respecter immédiatement les filtres encore actifs.
            filterMissingSeriesTable();
        }
    });
}

// Redirige _verifMatchRefreshCallback (déclaré plus haut) vers un rafraîchissement de
// CETTE ligne seule avant de déléguer à verifMatchSeries - évite de dupliquer une
// deuxième implémentation de la recherche auto + repli modale, déjà écrite pour
// "Métadonnées manquantes" (voir le commentaire en tête de section).
async function verifMatchingBedethequeButtonClick(seriesId, seriesTitle, buttonEl) {
    _verifMatchRefreshCallback = () => _refreshMissingRowOrRemove(seriesId);
    await verifMatchSeries(seriesId, seriesTitle, buttonEl);
}

// --- Matching EBDZ (version allégée de openEbdzMatchModal, static/js/library.js) ---

let _bulkEbdzMatchQueue = [];

function startBulkEbdzMatchQueue(rows) {
    _bulkEbdzMatchQueue = [...rows];
    _bulkEbdzMatchAdvance();
}

function _bulkEbdzMatchAdvance() {
    const next = _bulkEbdzMatchQueue.shift();
    if (!next) {
        document.getElementById('ebdz-match-modal').classList.remove('active');
        return;
    }
    if (_bulkEbdzMatchQueue.length > 0) {
        showToast('enrich-ebdz-queue', `${_bulkEbdzMatchQueue.length + 1} série(s) restante(s) à matcher sur EBDZ.`, { icon: 'search', autoHideMs: 3000 });
    }
    enrichOpenEbdzMatchModal(next.id, next.title, true);
}

// _isBulkStep: true uniquement quand appelée par _bulkEbdzMatchAdvance ci-dessus - tout
// autre appel (bouton EBDZ d'une ligne isolée) n'est pas un parcours groupé et doit donc
// annuler une éventuelle file laissée en cours par un "Matcher EBDZ" groupé précédent que
// l'utilisateur aurait interrompu (même garde que openManualEditModal, library.js).
function enrichOpenEbdzMatchModal(seriesId, seriesTitle, _isBulkStep = false) {
    if (!_isBulkStep) _bulkEbdzMatchQueue = [];

    const modal = document.getElementById('ebdz-match-modal');
    const body = document.getElementById('ebdz-match-modal-body');
    modal.classList.add('active');
    modal.dataset.seriesId = seriesId;

    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/ebdz-logo.png',
        title: 'Matcher manuellement sur EBDZ',
        queryId: 'ebdz-match-query',
        queryPlaceholder: 'Titre à rechercher sur EBDZ, ou URL directe du thread...',
        prefillValue: seriesTitle,
        resultsId: 'ebdz-match-results',
        searchOnclick: `enrichSearchEbdzMatchCandidates(${seriesId})`,
        autoSearch: true,
    });

    wireMatchModalEnterKeys('ebdz-match-query', () => enrichSearchEbdzMatchCandidates(seriesId));
    enrichSearchEbdzMatchCandidates(seriesId);
}

// Fermeture (× ou match confirmé, voir enrichConfirmEbdzMatch) - avance la file groupée
// s'il en reste une, sinon ferme normalement.
function closeEbdzMatchModal() {
    if (_bulkEbdzMatchQueue.length > 0) { _bulkEbdzMatchAdvance(); return; }
    document.getElementById('ebdz-match-modal').classList.remove('active');
}

async function enrichSearchEbdzMatchCandidates(seriesId) {
    const resultsEl = document.getElementById('ebdz-match-results');
    const query = document.getElementById('ebdz-match-query').value.trim();

    if (/^https?:\/\//i.test(query)) {
        await enrichConfirmEbdzMatch(seriesId, { thread_url: query });
        return;
    }

    resultsEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const params = query ? `?q=${encodeURIComponent(query)}` : '';
        const response = await fetch(`/api/series/${seriesId}/ebdz-candidates${params}`);
        const data = await response.json();

        if (!data.success) {
            resultsEl.innerHTML = matchModalErrorHtml(data.error || 'Erreur inconnue');
            return;
        }
        if (data.candidates.length === 0) {
            resultsEl.innerHTML = matchModalNoResultsHtml('Aucun thread EBDZ trouvé pour cette recherche');
            return;
        }
        resultsEl.innerHTML = data.candidates.map(c => matchCandidateCardHtml(
            `enrichConfirmEbdzMatch(${seriesId}, { thread_id: ${c.thread_id} })`,
            c.thread_title,
            `${svgIcon('folder')} ${escapeHtml(c.forum_category || '')} • ${c.file_count} ${pluralize(c.file_count, 'fichier')}${c.volumes.length ? ` • Volumes: ${c.volumes.join(', ')}` : ''}`
        )).join('');
    } catch (error) {
        resultsEl.innerHTML = matchModalErrorHtml(error.message);
    }
}

async function enrichConfirmEbdzMatch(seriesId, payload) {
    try {
        const response = await fetch(`/api/series/${seriesId}/ebdz-match`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();

        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            return;
        }

        closeEbdzMatchModal();
        _refreshMissingRowOrRemove(seriesId);
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

async function matchSelectedMissingSeriesBedetheque() {
    const selected = [...document.querySelectorAll('.missing-series-checkbox:checked')];
    if (!selected.length) { showToast('enrich-no-selection', 'Sélectionne au moins une série.', {icon: 'info', autoHideMs: 3500}); return; }
    showToast('enrich-selected', `Matching Bédéthèque de ${selected.length} série(s) lancé...`, {icon: 'search', autoHideMs: 4000});
    for (const checkbox of selected) {
        const row = checkbox.closest('tr');
        const button = row?.querySelectorAll('.missing-match-status-cell')[0]?.querySelector('button');
        if (!button) continue; // déjà matchée côté Bédéthèque, rien à faire
        const title = row?.querySelector('.missing-series-link')?.textContent || '';
        await verifMatchingBedethequeButtonClick(Number(checkbox.value), title, button);
    }
    loadMissingMatches();
    _updateMissingBulkActionsBar();
}

// EBDZ n'a pas d'équivalent automatique (voir le commentaire sur
// startBulkEbdzMatchQueue/_bulkEbdzMatchAdvance plus haut) - cette action ne fait
// qu'amorcer la file de modales à réviser une par une, jamais de matching en boucle
// silencieuse comme pour Bédéthèque ci-dessus.
function startBulkEbdzMatchFromSelection() {
    const selected = [...document.querySelectorAll('.missing-series-checkbox:checked')];
    if (!selected.length) { showToast('enrich-no-selection', 'Sélectionne au moins une série.', {icon: 'info', autoHideMs: 3500}); return; }
    const rows = selected.map(checkbox => {
        const row = checkbox.closest('tr');
        const hasEbdzButton = !!row?.querySelectorAll('.missing-match-status-cell')[1]?.querySelector('button');
        if (!hasEbdzButton) return null; // déjà matchée côté EBDZ (ou EBDZ pas configuré)
        return { id: Number(checkbox.value), title: row.querySelector('.missing-series-link')?.textContent || '' };
    }).filter(Boolean);
    if (!rows.length) { showToast('enrich-no-selection', "Aucune série sélectionnée n'a besoin d'un matching EBDZ.", {icon: 'info', autoHideMs: 4000}); return; }
    startBulkEbdzMatchQueue(rows);
}

function filterMissingSeriesTable(query) {
    if (query !== undefined) window.missingTitleFilter = String(query || '');
    const needle = (window.missingTitleFilter || '').trim().toLowerCase();
    const statusFilters = window.missingStatusFilters || {};
    document.querySelectorAll('#missing-table-body tr').forEach(row => {
        const title = row.querySelector('.missing-series-link')?.textContent?.toLowerCase() || '';
        // Les colonnes sont dynamiques: EBDZ et Komga peuvent être absents
        // indépendamment. Lire le champ via data-source évite que le filtre Komga
        // utilise par erreur la deuxième colonne (ou qu'il ne soit jamais appliqué).
        const statusCells = row.querySelectorAll('.missing-match-status-cell');
        const statusBySource = {};
        statusCells.forEach(cell => { statusBySource[cell.dataset.source] = cell.dataset.matched || ''; });
        row.hidden = (!!needle && !title.includes(needle))
            || Object.entries(statusFilters).some(([source, expected]) => expected && statusBySource[source] !== expected);
    });
    syncMissingSeriesSelectAll();
}

function toggleAllMissingSeries(checked) {
    document.querySelectorAll('#missing-table-body tr:not([hidden]) .missing-series-checkbox').forEach(cb => { cb.checked = checked; });
    syncMissingSeriesSelectAll();
}

function syncMissingSeriesSelectAll() {
    const all = [...document.querySelectorAll('#missing-table-body tr:not([hidden]) .missing-series-checkbox')];
    const selected = all.filter(cb => cb.checked);
    const master = document.getElementById('missing-series-select-all');
    if (master) {
        master.checked = all.length > 0 && selected.length === all.length;
        master.indeterminate = selected.length > 0 && selected.length < all.length;
    }
    _updateMissingBulkActionsBar();
}

// Les résultats de Vérification sont volumineux: toutes les sections restent repliées
// par défaut au chargement. Le bouton de chaque carte conserve l'ouverture à la demande.
function verifCollapseAllSections() {
    [
        ['missingMetadataList', 'missingMetadataToggle'],
        ['misnamedList', 'misnamedToggle'],
        ['missing-table-container', 'matchingToggle'],
        ['invalidFilesList', 'invalidFilesToggle'],
        ['unmatchedOwnedList', 'unmatchedOwnedToggle'],
        ['unmatchedOwnedKomgaList', 'unmatchedOwnedKomgaToggle'],
        ['duplicateSeriesList', 'duplicateSeriesToggle'],
        ['misplacedFoldersList', 'folderPlacementToggle'],
    ].forEach(([listId, buttonId]) => {
        const list = document.getElementById(listId);
        const button = document.getElementById(buttonId);
        if (list) list.style.display = 'none';
        if (button) button.style.transform = 'rotate(-90deg)';
    });
}

function _updateMissingSelectionBulkActionsBar() {
    const bar = document.getElementById('missing-bulk-actions-bar');
    if (!bar) return;
    const count = document.querySelectorAll('.missing-series-checkbox:checked').length;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="volume-bulk-actions-count">${count} ${pluralize(count, 'série')} ${pluralize(count, 'sélectionnée')}</span>
        <button class="btn-neutral-sm" onclick="matchSelectedMissingSeriesBedetheque()">Matcher Bédéthèque</button>
        ${_ebdzConfigured ? `<button class="btn-neutral-sm" onclick="startBulkEbdzMatchFromSelection()">Matcher EBDZ</button>` : ''}
        <button class="btn-neutral-sm" onclick="toggleAllMissingSeries(false)">Annuler la sélection</button>
    `;
}
function _updateMissingBulkActionsBar() { _updateMissingSelectionBulkActionsBar(); }

document.addEventListener('DOMContentLoaded', () => {
    verifCollapseAllSections();
    runVerificationCategory('missing_metadata');
    runVerificationCategory('misnamed');
    runVerificationCategory('misplaced_folders');
    runVerificationCategory('invalid_files');
    runVerificationCategory('unmatched_owned_volumes');
    runVerificationCategory('unmatched_owned_komga');
    runVerificationCategory('duplicate_series');
    loadMissingMatches();
    _verifResumeLinkVolumesBatchIfRunning();
});
