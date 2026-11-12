// Page Upscaling (groupe Actions) : choix du modèle, comparaison des modèles sur un extrait
// de page, albums non HD à upscaler sur le serveur et albums upscalés (original SD gardé).
// Données : GET /api/upscale/volumes ; modèle : POST /api/settings/upscale ; aperçus :
// GET /api/upscale/preview ; actions : POST /api/upscale/batch, DELETE /api/volumes/<id>/upscale,
// POST .../upscale/restore, DELETE .../upscale/backup.
let upscalingData = null;
let upscalingPoll = null;
const upscalingRowModels = {};   // modèle choisi par ligne (Albums non HD), sinon celui par défaut
const UPSCALING_BUSY = ['queued', 'running'];
const UPSCALING_STATUS = { queued: 'En attente', running: 'En cours', done: 'Terminé', error: 'Erreur', cancelled: 'Annulé' };
const compareState = { cx: 0.5, cy: 0.45, run: 0 };

function _upEsc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _upBytes(bytes) {
    if (!bytes) return '—';
    return bytes >= 1e9 ? `${(bytes / 1e9).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} Go`
        : `${Math.max(1, Math.round(bytes / 1e6)).toLocaleString('fr-FR')} Mo`;
}

function _upDate(value) {
    if (!value) return '—';
    const date = new Date(String(value).replace(' ', 'T'));
    return isNaN(date) ? value : date.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });
}

function _upModelLabel(key) {
    return (upscalingData?.catalog?.[key]?.label) || key || '—';
}

// Ordre de l'application Android (le JSON du serveur trie les clés par ordre alphabétique).
function _upOrder() {
    return upscalingData?.order || Object.keys(upscalingData?.catalog || {});
}

function _upFamily(key) {
    return key.startsWith('cugan') ? 'Real-CUGAN' : key === 'quality' ? 'Real-ESRGAN · digital art' : 'Real-ESRGAN';
}

async function loadUpscaling() {
    clearTimeout(upscalingPoll);
    try {
        const response = await fetch('/api/upscale/volumes');
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur de chargement');
        const first = !upscalingData;
        upscalingData = data;
        renderUpscalingModels();
        renderUpscalingLowDefinition();
        renderUpscalingBackups();
        if (first) compareInit();
        const busy = (data.jobs || []).some(job => UPSCALING_BUSY.includes(job.status));
        if (busy) upscalingPoll = setTimeout(loadUpscaling, 4000);
    } catch (error) {
        document.getElementById('upscalingModels').innerHTML = `<p class="upscaling-muted">${svgIcon('triangle-alert')} ${_upEsc(error.message)}</p>`;
        upscalingPoll = setTimeout(loadUpscaling, 15000);
    }
}

// ----- Modèle -----
function renderUpscalingModels() {
    const data = upscalingData;
    const running = (data.jobs || []).find(job => job.status === 'running');
    const queued = (data.jobs || []).filter(job => job.status === 'queued').length;
    let badges;
    if (!data.runtime) {
        badges = `<span class="upscale-badge upscale-badge-warn">${svgIcon('triangle-alert')} Moteur ONNX absent du serveur</span>`;
    } else if (!data.server_enabled) {
        badges = `<span class="upscale-badge upscale-badge-warn">${svgIcon('triangle-alert')} Désactivé</span>
            <button type="button" class="btn btn-sm" onclick="upscalingSaveConfig({ server_enabled: true })">Activer</button>`;
    } else if (running) {
        badges = `<span class="upscale-badge upscale-badge-ok">${svgIcon('loader-circle', 'icon-spin')} Upscaling en cours</span>`;
    } else {
        badges = `<span class="upscale-badge upscale-badge-ok">${svgIcon('check')} Prêt</span>`;
    }
    if (queued) badges += `<span class="upscale-badge">${queued} en attente</span>`;
    document.getElementById('upscalingState').innerHTML = badges;

    document.getElementById('upscalingModels').innerHTML = _upOrder().map(key => [key, data.catalog[key]]).map(([key, spec]) => {
        const present = data.models?.[key]?.present !== false;
        const active = key === data.model;
        return `<button type="button" role="radio" aria-checked="${active}" class="upscaling-model${active ? ' active' : ''}"${present ? '' : ' disabled'} onclick="upscalingSaveConfig({ model: '${key}' })">
            <span class="upscaling-model-top"><strong>${_upEsc(spec.label)}</strong><span class="upscaling-model-check">${active ? svgIcon('check') : ''}</span></span>
            <span class="upscaling-model-family">${_upFamily(key)}</span>
            <span class="upscaling-model-detail">${_upEsc(present ? spec.detail : 'Fichier du modèle absent du serveur')}</span>
        </button>`;
    }).join('');

    document.getElementById('upscalingRunning').innerHTML = running ? `
        <div class="upscaling-running">
            <div class="upscaling-running-text"><span class="upscaling-muted">En cours · ${_upEsc(_upModelLabel(running.model))}</span>
                <strong>${_upEsc(running.series_title || '')}</strong><span class="upscaling-muted">${_upEsc(running.filename || '')}</span></div>
            ${_upProgress(running)}
            <button type="button" class="upscale-icon-btn danger" onclick="upscalingCancel(${running.volume_id})" data-tooltip="Annuler" aria-label="Annuler">${svgIcon('x')}</button>
        </div>` : '';
}

function _upProgress(job) {
    const pct = job.total ? Math.round(100 * (job.page || 0) / job.total) : 0;
    return `<span class="upscaling-progress"><span class="upscale-progress"><span style="width:${pct}%"></span></span>
        <span class="upscale-progress-label">${job.total ? `${job.page || 0}/${job.total}` : '…'}</span></span>`;
}

async function upscalingSaveConfig(values) {
    try {
        const response = await fetch('/api/settings/upscale', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values) });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur de sauvegarde');
        upscalingData.model = data.model;
        upscalingData.server_enabled = data.server_enabled;
        upscalingData.available = data.server_enabled && data.runtime;
        renderUpscalingModels();
        renderUpscalingLowDefinition();
        compareMarkActive();
        if (values.model) showToast('upscaling-model', `Modèle : ${_upModelLabel(data.model)}`, { icon: 'check', autoHideMs: 2500 });
    } catch (error) {
        alert('❌ ' + error.message);
    }
}

// ----- Comparaison des modèles -----
function compareInit() {
    const select = document.getElementById('compareVolume');
    const items = (upscalingData.low_definition || []);
    if (!items.length) {
        select.innerHTML = '<option value="">Aucun album en basse définition</option>';
        document.getElementById('compareRunBtn').disabled = true;
        return;
    }
    select.innerHTML = items.map(item => `<option value="${item.volume_id}" data-pages="${item.page_count || ''}">${_upEsc(item.series_title)} · ${_upEsc(item.filename)}</option>`).join('');
    compareVolumeChanged();
}

function compareSelectVolume(volumeId) {
    const select = document.getElementById('compareVolume');
    select.value = String(volumeId);
    compareVolumeChanged();
    document.getElementById('compare').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function compareVolumeChanged() {
    const option = document.getElementById('compareVolume').selectedOptions[0];
    const input = document.getElementById('comparePage');
    const pages = Number(option?.dataset.pages) || 0;
    input.max = pages || '';
    if (pages && Number(input.value) > pages) input.value = Math.min(3, pages);
    compareState.cx = 0.5;
    compareState.cy = 0.45;
    compareLoadPage();
}

function compareLoadPage() {
    const volumeId = document.getElementById('compareVolume').value;
    if (!volumeId) return;
    const page = Math.max(1, Number(document.getElementById('comparePage').value) || 1);
    document.getElementById('compareZone').hidden = true;
    document.getElementById('compareImage').src = `/api/volumes/${volumeId}/reader/page/${page}`;
    document.getElementById('compareGrid').innerHTML = '<p class="upscaling-muted">Placez la zone sur la page puis cliquez sur « Comparer ».</p>';
}

function compareDrawZone() {
    const image = document.getElementById('compareImage');
    const zone = document.getElementById('compareZone');
    if (!image.naturalWidth) return;
    // Extrait de 160 px de la page source, à l'échelle de l'image affichée.
    const scale = image.clientWidth / image.naturalWidth;
    const side = Math.min(160, image.naturalWidth, image.naturalHeight) * scale;
    const left = Math.min(Math.max(compareState.cx * image.clientWidth - side / 2, 0), image.clientWidth - side);
    const top = Math.min(Math.max(compareState.cy * image.clientHeight - side / 2, 0), image.clientHeight - side);
    Object.assign(zone.style, { width: `${side}px`, height: `${side}px`, left: `${image.offsetLeft + left}px`, top: `${image.offsetTop + top}px` });
    zone.hidden = false;
}

function compareMoveZone(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    compareState.cx = (event.clientX - rect.left) / rect.width;
    compareState.cy = (event.clientY - rect.top) / rect.height;
    compareDrawZone();
    compareRun();
}

async function compareRun() {
    const volumeId = document.getElementById('compareVolume').value;
    if (!volumeId) return;
    const page = Math.max(1, Number(document.getElementById('comparePage').value) || 1);
    const run = ++compareState.run;
    const models = ['original', ..._upOrder().filter(key => upscalingData.models?.[key]?.present !== false)];
    const grid = document.getElementById('compareGrid');
    grid.innerHTML = models.map(key => `
        <figure class="upscaling-compare-tile${key === upscalingData.model ? ' active' : ''}${key === 'original' ? ' original' : ''}" data-model="${key}"${key === 'original' ? '' : ` onclick="upscalingSaveConfig({ model: '${key}' })" title="Garder ce modèle"`}>
            <div class="upscaling-compare-img"><span class="upscaling-muted">${svgIcon('loader-circle', 'icon-spin')}</span></div>
            <figcaption><strong>${key === 'original' ? 'Original agrandi' : _upEsc(_upModelLabel(key))}</strong><span class="upscaling-muted" data-time>${key === 'original' ? 'Sans modèle' : 'En attente…'}</span></figcaption>
        </figure>`).join('');
    const params = `volume_id=${volumeId}&page=${page}&cx=${compareState.cx.toFixed(3)}&cy=${compareState.cy.toFixed(3)}`;
    // Un modèle à la fois : le serveur ne calcule qu'un upscaling à la fois.
    for (const key of models) {
        if (run !== compareState.run) return;
        const tile = grid.querySelector(`[data-model="${key}"]`);
        const time = tile.querySelector('[data-time]');
        if (key !== 'original') time.textContent = 'Calcul…';
        const started = performance.now();
        try {
            const response = await fetch(`/api/upscale/preview?${params}&model=${key}`);
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || `Erreur ${response.status}`);
            }
            const url = URL.createObjectURL(await response.blob());
            if (run !== compareState.run) return;
            tile.querySelector('.upscaling-compare-img').innerHTML = `<img src="${url}" alt="">`;
            const seconds = (performance.now() - started) / 1000;
            if (key !== 'original') time.textContent = seconds < 0.2 ? '' : `${seconds.toLocaleString('fr-FR', { maximumFractionDigits: 1 })} s`;
        } catch (error) {
            tile.querySelector('.upscaling-compare-img').innerHTML = `<span class="upscaling-muted">${_upEsc(error.message)}</span>`;
            time.textContent = '';
        }
    }
}

function compareMarkActive() {
    document.querySelectorAll('.upscaling-compare-tile').forEach(tile => tile.classList.toggle('active', tile.dataset.model === upscalingData.model));
}

// ----- Albums non HD -----
function _upStateCell(item) {
    const job = item.job;
    if (job && UPSCALING_BUSY.includes(job.status)) {
        const progress = job.status === 'running' ? _upProgress(job) : `<span class="upscale-badge">${UPSCALING_STATUS.queued}</span>`;
        return `<div class="upscaling-row-actions">${progress}<button type="button" class="upscale-icon-btn danger" onclick="upscalingCancel(${item.volume_id})" data-tooltip="Annuler" aria-label="Annuler">${svgIcon('x')}</button></div>`;
    }
    if (job && job.status === 'error') return `<span class="upscale-badge upscale-badge-warn" title="${_upEsc(job.error || '')}">Erreur</span>`;
    if (item.reason) return `<span class="upscaling-muted">${_upEsc(item.reason)}</span>`;
    return `<div class="upscaling-row-actions"><button type="button" class="upscale-icon-btn" onclick="compareSelectVolume(${item.volume_id})" data-tooltip="Comparer les modèles sur cet album" aria-label="Comparer les modèles">${svgIcon('eye')}</button></div>`;
}

function _upAlbumCell(item) {
    return `<div class="upscaling-album"><a href="/series/${item.series_id}">${_upEsc(item.series_title)}</a><span>${_upEsc(item.filename)}</span></div>`;
}

function _upRowModelCell(item, canSelect, busy) {
    if (busy) return `<span class="upscaling-muted">${_upEsc(_upModelLabel(item.job.model))}</span>`;
    if (!canSelect) return '<span class="upscaling-muted">—</span>';
    const chosen = upscalingRowModels[item.volume_id] || '';
    const options = [`<option value="">Par défaut (${_upEsc(_upModelLabel(upscalingData.model))})</option>`]
        .concat(_upOrder().map(key => `<option value="${key}"${key === chosen ? ' selected' : ''}>${_upEsc(_upModelLabel(key))}</option>`));
    return `<select class="upscaling-input upscaling-row-model${chosen ? ' custom' : ''}" onchange="upscalingSetRowModel(${item.volume_id}, this)" aria-label="Modèle pour ${_upEsc(item.filename)}">${options.join('')}</select>`;
}

function upscalingSetRowModel(volumeId, select) {
    if (select.value) upscalingRowModels[volumeId] = select.value;
    else delete upscalingRowModels[volumeId];
    select.classList.toggle('custom', Boolean(select.value));
    // Choisir un modèle pour une ligne la sélectionne
    const checkbox = select.closest('tr')?.querySelector('.upscaling-select');
    if (checkbox && !checkbox.checked) { checkbox.checked = true; upscalingUpdateCount(); }
}

function renderUpscalingLowDefinition() {
    const items = upscalingData.low_definition || [];
    const list = document.getElementById('upscalingLowDefinitionList');
    const controls = document.getElementById('upscalingBulkControls');
    document.getElementById('upscalingLowDefinitionCount').textContent = items.length;
    const selected = new Set([...document.querySelectorAll('.upscaling-select:checked')].map(cb => Number(cb.dataset.volumeId)));
    if (!items.length) {
        list.innerHTML = `<p class="upscaling-muted">${svgIcon('check')} Aucun album en basse définition.</p>`;
        controls.hidden = true;
        return;
    }
    const enabled = upscalingData.available;
    list.innerHTML = `<table class="upscaling-table"><thead><tr>
            <th class="upscaling-check"></th><th>Album</th><th>Modèle</th><th>Définition</th><th class="upscaling-num">Pages</th><th class="upscaling-num upscaling-size">Taille</th><th class="upscaling-end"></th>
        </tr></thead><tbody>${items.map(item => {
        const busy = item.job && UPSCALING_BUSY.includes(item.job.status);
        const canSelect = enabled && item.upscalable && !busy;
        const haystack = `${item.series_title} ${item.filename}`.toLowerCase();
        return `<tr data-filter="${_upEsc(haystack)}">
            <td class="upscaling-check">${canSelect ? `<input type="checkbox" class="upscaling-select" data-volume-id="${item.volume_id}"${selected.has(item.volume_id) ? ' checked' : ''} onchange="upscalingUpdateCount()" aria-label="Sélectionner ${_upEsc(item.filename)}">` : ''}</td>
            <td>${_upAlbumCell(item)}</td>
            <td>${_upRowModelCell(item, canSelect, busy)}</td>
            <td class="upscaling-nowrap">${item.page_width} × ${item.page_height}</td>
            <td class="upscaling-num">${item.page_count || '—'}</td>
            <td class="upscaling-num upscaling-size">${_upBytes(item.file_size)}</td>
            <td class="upscaling-end">${_upStateCell(item)}</td></tr>`;
    }).join('')}</tbody></table>`;
    controls.hidden = false;
    upscalingApplyFilter();
}

function upscalingApplyFilter() {
    const query = (document.getElementById('upscalingFilter')?.value || '').trim().toLowerCase();
    document.querySelectorAll('#upscalingLowDefinitionList tbody tr').forEach(row => {
        row.hidden = Boolean(query) && !row.dataset.filter.includes(query);
    });
    upscalingUpdateCount();
}

function upscalingToggleAll(checkbox) {
    document.querySelectorAll('.upscaling-select').forEach(cb => { if (!cb.closest('tr').hidden) cb.checked = checkbox.checked; });
    upscalingUpdateCount();
}

function upscalingUpdateCount() {
    const count = document.querySelectorAll('.upscaling-select:checked').length;
    document.getElementById('upscalingSelectedCount').textContent = count;
    document.getElementById('upscalingRunBtn').disabled = count === 0 || !upscalingData?.available;
}

async function upscalingRunSelected() {
    const ids = [...document.querySelectorAll('.upscaling-select:checked')].map(cb => Number(cb.dataset.volumeId));
    if (!ids.length) return;
    const items = ids.map(id => ({ volume_id: id, model: upscalingRowModels[id] || upscalingData.model }));
    const models = [...new Set(items.map(item => item.model))];
    const withModel = models.length === 1 ? `avec « ${_upModelLabel(models[0])} »` : `avec le modèle choisi pour chacun (${models.map(_upModelLabel).join(', ')})`;
    if (!confirm(`Upscaler ${ids.length} album(s) en HD ${withModel} ?\n\nChaque fichier est remplacé par sa version HD ; l'original SD est gardé et restaurable. Le serveur traite un tome à la fois, ce qui peut prendre longtemps.`)) return;
    const btn = document.getElementById('upscalingRunBtn');
    btn.disabled = true;
    try {
        const response = await fetch('/api/upscale/batch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        const refused = (data.errors || []).length;
        showToast('upscaling-batch', `${data.queued.length} album(s) en file d'attente${refused ? `, ${refused} refusé(s) : ${data.errors[0].error}` : ''}.`, { icon: 'sparkles', autoHideMs: 6000 });
        document.querySelectorAll('.upscaling-select:checked').forEach(cb => { cb.checked = false; delete upscalingRowModels[Number(cb.dataset.volumeId)]; });
        document.getElementById('upscalingSelectAll').checked = false;
        loadUpscaling();
    } catch (error) {
        alert('❌ ' + error.message);
        btn.disabled = false;
    }
}

async function upscalingCancel(volumeId) {
    await fetch(`/api/volumes/${volumeId}/upscale`, { method: 'DELETE' }).catch(() => null);
    loadUpscaling();
}

// ----- Albums upscalés -----
function renderUpscalingBackups() {
    const items = upscalingData.backups || [];
    const list = document.getElementById('upscalingBackupsList');
    document.getElementById('upscalingBackupsCount').textContent = items.length;
    document.getElementById('upscalingBackupsBytes').textContent = items.length ? `${_upBytes(upscalingData.backup_bytes)} de versions SD gardées` : '';
    if (!items.length) {
        list.innerHTML = '<p class="upscaling-muted">Aucun album upscalé pour l\'instant.</p>';
        return;
    }
    list.innerHTML = `<table class="upscaling-table"><thead><tr>
            <th>Album</th><th>Modèle</th><th>Upscalé le</th><th class="upscaling-num">Version SD</th><th class="upscaling-end"></th>
        </tr></thead><tbody>${items.map(item => `<tr>
            <td>${_upAlbumCell({ ...item, series_id: item.current_series_id || item.series_id, series_title: item.series_title || '—', filename: item.filename || '' })}</td>
            <td>${_upEsc(_upModelLabel(item.model))}</td>
            <td class="upscaling-nowrap">${_upDate(item.created_at)}</td>
            <td class="upscaling-num">${item.exists ? _upBytes(item.bytes) : '<span class="upscale-badge upscale-badge-warn">Absente</span>'}</td>
            <td class="upscaling-end"><div class="upscaling-row-actions">
                <button type="button" class="upscale-icon-btn" onclick="upscalingRestore(${item.volume_id})"${item.exists ? '' : ' disabled'} data-tooltip="Remettre la version SD" aria-label="Remettre la version SD">${svgIcon('undo')}</button>
                <button type="button" class="upscale-icon-btn danger" onclick="upscalingDeleteBackup(${item.volume_id})" data-tooltip="Supprimer la version SD (garder la HD)" aria-label="Supprimer la version SD">${svgIcon('trash-2')}</button>
            </div></td></tr>`).join('')}</tbody></table>`;
}

async function upscalingRestore(volumeId) {
    if (!confirm('Remettre la version SD d\'origine de cet album ? La version HD sera supprimée.')) return;
    try {
        const response = await fetch(`/api/volumes/${volumeId}/upscale/restore`, { method: 'POST' });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        showToast('upscaling-restore', 'Version SD remise en place.', { icon: 'check', autoHideMs: 4000 });
    } catch (error) {
        alert('❌ ' + error.message);
    }
    loadUpscaling();
}

async function upscalingDeleteBackup(volumeId) {
    if (!confirm('Supprimer définitivement la version SD gardée ? L\'album reste en HD, mais l\'upscaling ne pourra plus être annulé.')) return;
    try {
        const response = await fetch(`/api/volumes/${volumeId}/upscale/backup`, { method: 'DELETE' });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
    } catch (error) {
        alert('❌ ' + error.message);
    }
    loadUpscaling();
}

window.addEventListener('resize', () => compareDrawZone());
document.addEventListener('DOMContentLoaded', loadUpscaling);
