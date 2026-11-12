// Configuration > Upscaling.
// « Dans ce navigateur » : modèles du lecteur téléchargés ou non (comme dans l'application
// Android, propres à chaque appareil) et réglage par défaut du lecteur.
// « Sur le serveur » : upscaling hors ligne de tomes entiers (molette d'un tome,
// Vérification > Albums non HD) : permis ou non, modèle utilisé, file d'attente et versions
// SD gardées (restaurer ou supprimer).

const UPSCALE_ICONS = {
    download: '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
};

function _upscaleIcon(name) {
    return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${UPSCALE_ICONS[name]}</svg>`;
}

function _upscaleEscape(text) {
    return String(text ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadUpscaleSettings() {
    renderUpscaleThreshold();
    renderUpscaleBrowser();
}

// Seuil de basse définition : une page dont le plus grand côté ne dépasse pas cette valeur
// est « Basse déf. » et peut être upscalée (lecteur, molettes, page Upscaling).
const UPSCALE_THRESHOLD_HINTS = {
    1200: 'Seulement les scans vraiment petits.',
    1600: 'Conseillé : au-delà, un écran d\'ordinateur affiche déjà la page sans l\'agrandir.',
    2000: 'Pour lire sur une grande tablette ou un écran 1440p.',
    2400: 'Presque tout sauf la HD : calcul et fichiers bien plus lourds, gain souvent faible.',
};

async function renderUpscaleThreshold(saving) {
    const box = document.getElementById('upscaleThreshold');
    if (!box) return;
    let data = null;
    try { data = await (await fetch('/api/upscale/threshold')).json(); } catch (e) { /* rendu par défaut */ }
    const current = data?.max_side || bullarrLowDefMaxSide();
    const choices = data?.choices || [1200, 1600, 2000, 2400];
    box.innerHTML = `
        <p class="help-text upscale-threshold-text">Une page dont le plus grand côté ne dépasse pas ce seuil est en basse définition : l'album porte l'étiquette « Basse déf. », peut être upscalé sur le serveur (molettes, page <a href="/upscaling">Upscaling</a>) et le bouton HD du lecteur l'améliore.</p>
        <div class="upscale-defaults upscale-threshold">
            <div><strong>Seuil</strong><span>${_upscaleEscape(UPSCALE_THRESHOLD_HINTS[current] || '')}</span></div>
            <div class="upscale-segmented" role="group" aria-label="Seuil de basse définition">${choices.map(value =>
                `<button type="button" class="upscale-segment${value === current ? ' active' : ''}" onclick="setUpscaleThreshold(${value})"${saving ? ' disabled' : ''}>${value} px</button>`).join('')}</div>
        </div>`;
}

async function setUpscaleThreshold(value) {
    try {
        const response = await fetch('/api/settings/upscale', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ max_side: value }) });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');
        bullarrSetLowDefMaxSide(data.max_side);
        if (typeof showToast === 'function') showToast('upscale-threshold', `Basse définition : pages de ${data.max_side} px au plus`, { icon: 'check', autoHideMs: 2500 });
    } catch (error) {
        alert('❌ ' + error.message);
    }
    renderUpscaleThreshold();
}

async function renderUpscaleBrowser() {
    const box = document.getElementById('upscaleBrowser');
    if (!box) return;
    const installed = await comicUpscaleInstalled();
    const storage = comicUpscaleStorageSupported();
    const gpuOk = !!navigator.gpu && !!(await navigator.gpu.requestAdapter().catch(() => null));
    const rows = Object.entries(COMIC_UPSCALE_MODELS).map(([model, spec]) => {
        const pending = comicUpscaleStore.downloading[model];
        let state;
        let action;
        if (pending) {
            state = `<span class="upscale-badge">Téléchargement…</span>`;
            action = `<div class="upscale-progress"><span style="width:${Math.round(pending.progress * 100)}%"></span></div><span class="upscale-progress-label">${Math.round(pending.progress * 100)} %</span>`;
        } else if (installed[model]) {
            state = `<span class="upscale-badge upscale-badge-ok">${_upscaleIcon('check')} Téléchargé</span>`;
            action = `<button type="button" class="upscale-icon-btn danger" onclick="deleteUpscaleModel('${model}')" data-tooltip="Supprimer de ce navigateur" aria-label="Supprimer ${_upscaleEscape(spec.label)} de ce navigateur">${_upscaleIcon('trash')}</button>`;
        } else {
            state = `<span class="upscale-badge">Non téléchargé</span>`;
            const size = comicUpscaleFormatSize(comicUpscaleDownloadBytes(model));
            action = `<button type="button" class="upscale-icon-btn" onclick="downloadUpscaleModel('${model}')" data-tooltip="Télécharger (${size})" aria-label="Télécharger ${_upscaleEscape(spec.label)} (${size})"${storage ? '' : ' disabled'}>${_upscaleIcon('download')}</button>`;
        }
        return `<div class="upscale-model-row">
            <div class="upscale-model-text"><strong>${_upscaleEscape(spec.label)}</strong><span>${_upscaleEscape(spec.detail)}</span><span class="upscale-model-size">${comicUpscaleFormatSize(spec.bytes)}</span></div>
            <div class="upscale-model-action">${state}${action}</div>
        </div>`;
    }).join('');
    const segment = (name, options, current) => `<div class="upscale-segmented" role="group">${options.map(([value, label]) =>
        `<button type="button" class="upscale-segment${value === current ? ' active' : ''}" onclick="setUpscaleDefault('${name}', '${value}')">${_upscaleEscape(label)}</button>`).join('')}</div>`;
    box.innerHTML = `
        <div class="upscale-line"><span>WebGPU</span><span class="upscale-badge ${gpuOk ? 'upscale-badge-ok' : 'upscale-badge-warn'}">${gpuOk ? 'Disponible' : 'Indisponible dans ce navigateur'}</span></div>
        ${storage ? '' : '<p class="help-text">Connexion non sécurisée : ce navigateur ne peut pas garder les modèles. Ouvrez Bullarr en https.</p>'}
        <div class="upscale-models">${rows}</div>
        <p class="help-text">Le premier téléchargement ajoute le moteur WebGPU (onnxruntime-web, ${comicUpscaleFormatSize(COMIC_UPSCALE_RUNTIME.bytes)}). Supprimer le dernier modèle le retire aussi. Rien n'est téléchargé tant que vous ne le demandez pas.</p>
        <div class="upscale-defaults">
            <div><strong>Dans le lecteur</strong><span>Réglage par défaut, aussi modifiable avec le bouton HD du lecteur (touche U).</span></div>
            ${segment('mode', Object.entries(COMIC_UPSCALE_MODES), comicUpscale.mode)}
        </div>`;
}

function setUpscaleDefault(name, value) {
    if (name === 'mode') {
        comicUpscale.mode = value;
        _saveComicUpscalePref('comic-reader-upscale-mode', value);
    }
    renderUpscaleBrowser();
}

async function downloadUpscaleModel(model) {
    try {
        await comicUpscaleDownload(model);
    } catch (error) {
        alert(error.message || String(error));
    }
    renderUpscaleBrowser();
}

async function deleteUpscaleModel(model) {
    if (!confirm(`Supprimer le modèle ${COMIC_UPSCALE_MODELS[model].label} de ce navigateur ?`)) return;
    await comicUpscaleDelete(model);
    renderUpscaleBrowser();
}

document.addEventListener('DOMContentLoaded', () => { if (document.getElementById('tab-upscale')) loadUpscaleSettings(); });
