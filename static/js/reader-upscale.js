// Amélioration des pages en basse définition (côté le plus grand ≤ 1600 px) dans le lecteur
// intégré : Real-ESRGAN, rendu x2. Deux façons de calculer, au choix du lecteur (mémorisé) :
//  - Navigateur : onnxruntime-web sur la carte graphique (WebGPU), rien ne part au serveur ;
//  - Serveur : GET /api/volumes/<id>/reader/page/<n>/enhanced (onnxruntime sur le processeur,
//    gardé en cache), utile sur un appareil sans WebGPU ou avec un serveur puissant.
// La page d'origine s'affiche tout de suite, puis est remplacée dès que la version améliorée
// est prête. Les pages affichées passent en premier, puis les deux suivantes (préchargement).
const COMIC_UPSCALE_MAX_SIDE = 1600;
const COMIC_UPSCALE_TILE = 256;
const COMIC_UPSCALE_PAD = 12;
const COMIC_UPSCALE_PREFETCH = 2;
const COMIC_UPSCALE_CACHE = 16;
const COMIC_UPSCALE_ORT = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/';
const COMIC_UPSCALE_MODES = {
    off: 'Désactivée',
    browser: 'Navigateur',
    server: 'Serveur',
};
const COMIC_UPSCALE_MODELS = {
    fast: 'Rapide',
    quality: 'Qualité',
};

const comicUpscale = {
    mode: 'off',
    model: 'fast',
    serverAvailable: false,
    volumeId: null,
    cache: new Map(),     // clé -> { url: blob URL | null (page déjà en bonne définition) }
    queue: [],            // pages à calculer, par ordre de priorité
    running: null,        // clé en cours de calcul
    shown: [],
    wanted: [],          // pages affichées et préchargées en ce moment
    error: '',
    ortPromise: null,
    sessions: {},
};

(function _loadComicUpscalePrefs() {
    try {
        const mode = localStorage.getItem('comic-reader-upscale-mode');
        const model = localStorage.getItem('comic-reader-upscale-model');
        if (mode in COMIC_UPSCALE_MODES) comicUpscale.mode = mode;
        if (model in COMIC_UPSCALE_MODELS) comicUpscale.model = model;
    } catch (e) { /* stockage indisponible */ }
})();

function _saveComicUpscalePref(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* stockage indisponible */ }
}

function _comicUpscaleKey(number) {
    return `${comicUpscale.volumeId}:${number}:${comicUpscale.mode}:${comicUpscale.model}`;
}

// Adresse à afficher pour une page : la version améliorée si elle est déjà prête.
function comicReaderUpscaleUrl(number) {
    if (comicUpscale.mode === 'off') return null;
    return comicUpscale.cache.get(_comicUpscaleKey(number))?.url || null;
}

function comicReaderUpscaleOpen(volumeId, serverAvailable) {
    _comicUpscaleClear();
    comicUpscale.volumeId = volumeId;
    comicUpscale.serverAvailable = !!serverAvailable;
    comicUpscale.error = '';
    _renderComicUpscaleControls();
}

function comicReaderUpscaleClose() {
    _comicUpscaleClear();
    comicUpscale.volumeId = null;
    toggleComicReaderUpscaleMenu(false);
}

function _comicUpscaleClear() {
    comicUpscale.queue = [];
    comicUpscale.shown = [];
    comicUpscale.wanted = [];
    comicUpscale.cache.forEach(entry => { if (entry.url) URL.revokeObjectURL(entry.url); });
    comicUpscale.cache.clear();
    _renderComicUpscaleStatus();
}

// Appelé à chaque page affichée (mode pages et webtoon).
function comicReaderUpscaleShown(shown) {
    comicUpscale.shown = shown.slice();
    if (comicUpscale.mode === 'off' || !comicUpscale.volumeId || comicUpscale.error) {
        _renderComicUpscaleStatus();
        return;
    }
    const last = shown[shown.length - 1];
    const wanted = shown.slice();
    for (let n = last + 1; n <= Math.min(comicReader.count, last + COMIC_UPSCALE_PREFETCH); n++) wanted.push(n);
    comicUpscale.wanted = wanted;
    shown.forEach(number => {
        const entry = comicUpscale.cache.get(_comicUpscaleKey(number));
        if (entry?.url) _applyComicUpscale(number, entry.url);
    });
    // On ne garde que les pages utiles maintenant, dans l'ordre : affichées puis suivantes.
    comicUpscale.queue = wanted.filter(number => {
        const key = _comicUpscaleKey(number);
        return !comicUpscale.cache.has(key) && key !== comicUpscale.running;
    });
    _renderComicUpscaleStatus();
    _runComicUpscaleQueue();
}

async function _runComicUpscaleQueue() {
    if (comicUpscale.running) return;
    while (comicUpscale.queue.length && comicUpscale.mode !== 'off' && !comicUpscale.error) {
        const number = comicUpscale.queue.shift();
        const key = _comicUpscaleKey(number);
        const volumeId = comicUpscale.volumeId;
        if (comicUpscale.cache.has(key)) continue;
        comicUpscale.running = key;
        _renderComicUpscaleStatus();
        let url = null;
        try {
            url = comicUpscale.mode === 'server'
                ? await _comicUpscaleOnServer(volumeId, number, comicUpscale.model)
                : await _comicUpscaleInBrowser(volumeId, number, comicUpscale.model);
        } catch (error) {
            if (error instanceof _ComicUpscaleCancelled) { comicUpscale.running = null; continue; }
            comicUpscale.error = error.message || String(error);
            console.warn('Amélioration de la page impossible', error);
        }
        comicUpscale.running = null;
        // Le lecteur a pu changer de tome, de mode ou de modèle pendant le calcul.
        if (key !== _comicUpscaleKey(number) || volumeId !== comicUpscale.volumeId) {
            if (url) URL.revokeObjectURL(url);
            continue;
        }
        if (!comicUpscale.error) {
            comicUpscale.cache.set(key, { url });
            _trimComicUpscaleCache();
            if (url) _applyComicUpscale(number, url);
        }
        _renderComicUpscaleStatus();
    }
    comicUpscale.running = null;
    _renderComicUpscaleStatus();
}

function _trimComicUpscaleCache() {
    while (comicUpscale.cache.size > COMIC_UPSCALE_CACHE) {
        const [key, entry] = comicUpscale.cache.entries().next().value;
        const number = parseInt(key.split(':')[1], 10);
        if (comicUpscale.shown.includes(number)) break;
        if (entry.url) URL.revokeObjectURL(entry.url);
        comicUpscale.cache.delete(key);
    }
}

// Remplace la page d'origine par sa version améliorée là où elle est affichée.
function _applyComicUpscale(number, url) {
    document.querySelectorAll(`#comic-reader-pages .comic-reader-image[data-page="${number}"], #comic-reader-webtoon [data-page="${number}"]`)
        .forEach(image => {
            if (image.src === url) return;
            if (!image.dataset.originalSrc) image.dataset.originalSrc = image.getAttribute('src') || '';
            image.dataset.enhanced = '1';
            image.src = url;
        });
}

// Retour aux pages d'origine (amélioration désactivée, autre modèle).
function _restoreComicUpscaleOriginals() {
    document.querySelectorAll('#comic-reader [data-enhanced="1"]').forEach(image => {
        const number = parseInt(image.dataset.page, 10);
        image.src = image.dataset.originalSrc || _comicReaderPageUrl(number);
        delete image.dataset.enhanced;
        delete image.dataset.originalSrc;
    });
}

class _ComicUpscaleCancelled extends Error {}

// ----- Serveur -----
async function _comicUpscaleOnServer(volumeId, number, model) {
    const response = await fetch(`/api/volumes/${volumeId}/reader/page/${number}/enhanced?model=${model}`);
    if (response.status === 204) return null;
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || `Serveur : HTTP ${response.status}`);
    }
    return URL.createObjectURL(await response.blob());
}

// ----- Navigateur (WebGPU) -----
function _loadComicUpscaleOrt() {
    if (!comicUpscale.ortPromise) {
        comicUpscale.ortPromise = (async () => {
            if (!navigator.gpu || !(await navigator.gpu.requestAdapter().catch(() => null))) {
                throw new Error('WebGPU indisponible dans ce navigateur : choisissez « Serveur »');
            }
            if (!window.ort) {
                await new Promise((resolve, reject) => {
                    const script = document.createElement('script');
                    script.src = `${COMIC_UPSCALE_ORT}ort.webgpu.min.js`;
                    script.crossOrigin = 'anonymous';
                    script.onload = resolve;
                    script.onerror = () => reject(new Error('Chargement de onnxruntime-web impossible'));
                    document.head.appendChild(script);
                });
            }
            window.ort.env.wasm.wasmPaths = COMIC_UPSCALE_ORT;
            return window.ort;
        })();
        comicUpscale.ortPromise.catch(() => { comicUpscale.ortPromise = null; });
    }
    return comicUpscale.ortPromise;
}

async function _comicUpscaleSession(model) {
    const ort = await _loadComicUpscaleOrt();
    if (!comicUpscale.sessions[model]) {
        comicUpscale.sessions[model] = ort.InferenceSession.create(`/static/models/realesrgan-${model}.onnx`, {
            executionProviders: ['webgpu'], graphOptimizationLevel: 'all',
        });
        comicUpscale.sessions[model].catch(() => { delete comicUpscale.sessions[model]; });
    }
    return { ort, session: await comicUpscale.sessions[model] };
}

// Découpe d'un côté de `size` px (même calcul que blueprints/library/upscale.py) :
// [début de la zone gardée, sa fin, début de la tuile lue].
function _comicUpscaleSpans(size) {
    if (size <= COMIC_UPSCALE_TILE) return [[0, size, 0]];
    const core = COMIC_UPSCALE_TILE - 2 * COMIC_UPSCALE_PAD;
    const spans = [];
    for (let start = 0; start < size;) {
        const end = Math.min(start + core, size);
        spans.push([start, end, Math.min(Math.max(0, start - COMIC_UPSCALE_PAD), size - COMIC_UPSCALE_TILE)]);
        start = end;
    }
    return spans;
}

async function _comicUpscaleInBrowser(volumeId, number, model) {
    const response = await fetch(`/api/volumes/${volumeId}/reader/page/${number}`);
    if (!response.ok) throw new Error(`Page ${number} : HTTP ${response.status}`);
    const bitmap = await createImageBitmap(await response.blob());
    const width = bitmap.width;
    const height = bitmap.height;
    if (!width || !height || Math.max(width, height) > COMIC_UPSCALE_MAX_SIDE) {
        bitmap.close?.();
        return null;
    }
    const { ort, session } = await _comicUpscaleSession(model);
    const source = document.createElement('canvas');
    source.width = width;
    source.height = height;
    const sourceContext = source.getContext('2d', { willReadFrequently: true });
    sourceContext.drawImage(bitmap, 0, 0);
    bitmap.close?.();
    const pixels = sourceContext.getImageData(0, 0, width, height).data;
    const output = document.createElement('canvas');
    output.width = width * 2;
    output.height = height * 2;
    const outputContext = output.getContext('2d');
    outputContext.imageSmoothingEnabled = true;
    outputContext.imageSmoothingQuality = 'high';
    const piece = document.createElement('canvas');
    const pieceContext = piece.getContext('2d');
    const th = Math.min(COMIC_UPSCALE_TILE, height);
    const tw = Math.min(COMIC_UPSCALE_TILE, width);
    const plane = th * tw;
    const input = new Float32Array(3 * plane);
    const inputName = session.inputNames[0];
    for (const [y0, y1, ry] of _comicUpscaleSpans(height)) {
        for (const [x0, x1, rx] of _comicUpscaleSpans(width)) {
            for (let y = 0; y < th; y++) {
                let src = ((ry + y) * width + rx) * 4;
                let dst = y * tw;
                for (let x = 0; x < tw; x++, src += 4, dst++) {
                    input[dst] = pixels[src] / 255;
                    input[plane + dst] = pixels[src + 1] / 255;
                    input[2 * plane + dst] = pixels[src + 2] / 255;
                }
            }
            const results = await session.run({ [inputName]: new ort.Tensor('float32', input, [1, 3, th, tw]) });
            const result = results[session.outputNames[0]];
            const data = result.data;
            const ow = tw * 4;
            const oplane = th * 4 * ow;
            const cw = (x1 - x0) * 4;
            const ch = (y1 - y0) * 4;
            const ox = (x0 - rx) * 4;
            const oy = (y0 - ry) * 4;
            const image = new ImageData(cw, ch);
            const rgba = image.data;
            for (let y = 0; y < ch; y++) {
                let src = (oy + y) * ow + ox;
                let dst = y * cw * 4;
                for (let x = 0; x < cw; x++, src++, dst += 4) {
                    rgba[dst] = data[src] * 255 + 0.5;
                    rgba[dst + 1] = data[oplane + src] * 255 + 0.5;
                    rgba[dst + 2] = data[2 * oplane + src] * 255 + 0.5;
                    rgba[dst + 3] = 255;
                }
            }
            result.dispose?.();
            piece.width = cw;
            piece.height = ch;
            pieceContext.putImageData(image, 0, 0);
            // Sortie x4 ramenée à x2 : assez pour l'écran, quatre fois moins de mémoire.
            outputContext.drawImage(piece, x0 * 2, y0 * 2, (x1 - x0) * 2, (y1 - y0) * 2);
            // Laisse respirer la page entre deux tuiles.
            await new Promise(resolve => setTimeout(resolve, 0));
            // Abandon si le lecteur est passé à autre chose (tome, mode, modèle, page éloignée).
            if (volumeId !== comicUpscale.volumeId || comicUpscale.mode !== 'browser' || comicUpscale.model !== model
                || !comicUpscale.wanted.includes(number)) throw new _ComicUpscaleCancelled();
        }
    }
    const blob = await new Promise(resolve => output.toBlob(resolve, 'image/jpeg', 0.92));
    if (!blob) throw new Error('Encodage de la page améliorée impossible');
    return URL.createObjectURL(blob);
}

// ----- Bouton et menu du lecteur -----
function setComicReaderUpscale(mode, model) {
    const changed = (mode && mode !== comicUpscale.mode) || (model && model !== comicUpscale.model);
    if (mode in COMIC_UPSCALE_MODES) { comicUpscale.mode = mode; _saveComicUpscalePref('comic-reader-upscale-mode', mode); }
    if (model in COMIC_UPSCALE_MODELS) { comicUpscale.model = model; _saveComicUpscalePref('comic-reader-upscale-model', model); }
    if (changed) {
        comicUpscale.error = '';
        _restoreComicUpscaleOriginals();
        _comicUpscaleClear();
        if (comicUpscale.mode !== 'off' && comicReader.count) {
            comicReaderUpscaleShown(_comicReaderSpreadPages(comicReader.page, comicReader.count, _comicReaderSpread()));
        }
    }
    _renderComicUpscaleControls();
}

// « U » ou le bouton : passe à la façon suivante (Désactivée → Navigateur → Serveur).
function cycleComicReaderUpscale() {
    const modes = Object.keys(COMIC_UPSCALE_MODES).filter(mode => mode !== 'server' || comicUpscale.serverAvailable);
    setComicReaderUpscale(modes[(modes.indexOf(comicUpscale.mode) + 1) % modes.length]);
}

function toggleComicReaderUpscaleMenu(force) {
    const overlay = document.getElementById('comic-reader');
    if (!overlay) return;
    let menu = document.getElementById('comic-reader-upscale-menu');
    const show = force === undefined ? !menu || menu.hidden : !!force;
    if (!menu) {
        if (!show) return;
        menu = document.createElement('div');
        menu.id = 'comic-reader-upscale-menu';
        menu.className = 'comic-reader-upscale-menu';
        menu.addEventListener('click', event => {
            event.stopPropagation();
            const option = event.target.closest('[data-upscale-mode], [data-upscale-model]');
            if (!option || option.disabled) return;
            setComicReaderUpscale(option.dataset.upscaleMode, option.dataset.upscaleModel);
        });
        overlay.appendChild(menu);
        document.addEventListener('click', event => {
            if (!menu.hidden && !menu.contains(event.target) && !event.target.closest('#comic-reader-upscale-btn')) menu.hidden = true;
        });
    }
    menu.hidden = !show;
    if (show) {
        const button = document.getElementById('comic-reader-upscale-btn');
        const rect = button.getBoundingClientRect();
        menu.style.top = `${rect.bottom + 6}px`;
        menu.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
    }
    _renderComicUpscaleControls();
}

function _renderComicUpscaleControls() {
    const button = document.getElementById('comic-reader-upscale-btn');
    if (button) {
        button.classList.toggle('active', comicUpscale.mode !== 'off');
        const label = comicUpscale.mode === 'off' ? 'Désactivée'
            : `${COMIC_UPSCALE_MODES[comicUpscale.mode]} · ${COMIC_UPSCALE_MODELS[comicUpscale.model]}`;
        button.dataset.tooltip = `Amélioration des pages en basse définition : ${label} (U)`;
        button.setAttribute('aria-label', `Amélioration des pages : ${label}`);
    }
    const menu = document.getElementById('comic-reader-upscale-menu');
    if (menu && !menu.hidden) {
        const option = (attr, value, label, current, disabled, hint) =>
            `<button type="button" class="comic-reader-upscale-option${current ? ' active' : ''}" data-${attr}="${value}"${disabled ? ' disabled' : ''}${hint ? ` data-tooltip="${hint}"` : ''}>${label}</button>`;
        menu.innerHTML = `
            <div class="comic-reader-upscale-title">Amélioration des pages en basse définition</div>
            <div class="comic-reader-upscale-row">${Object.entries(COMIC_UPSCALE_MODES).map(([mode, label]) =>
                option('upscale-mode', mode, label, mode === comicUpscale.mode, mode === 'server' && !comicUpscale.serverAvailable,
                    mode === 'browser' ? 'Calcul sur la carte graphique de cet appareil (WebGPU)'
                        : mode === 'server' ? (comicUpscale.serverAvailable ? 'Calcul sur le serveur, gardé en cache' : 'Indisponible sur ce serveur') : '')).join('')}</div>
            <div class="comic-reader-upscale-subtitle">Modèle</div>
            <div class="comic-reader-upscale-row">${Object.entries(COMIC_UPSCALE_MODELS).map(([model, label]) =>
                option('upscale-model', model, label, model === comicUpscale.model, false,
                    model === 'fast' ? 'Léger, quelques secondes par page' : 'Plus fin, beaucoup plus lent')).join('')}</div>
            <div class="comic-reader-upscale-note">Pages de ${COMIC_UPSCALE_MAX_SIDE} px au plus, agrandies x2 (Real-ESRGAN).</div>`;
    }
    _renderComicUpscaleStatus();
}

function _renderComicUpscaleStatus() {
    const status = document.getElementById('comic-reader-upscale-status');
    if (!status) return;
    let text = '';
    let tooltip = '';
    if (comicUpscale.mode !== 'off' && comicUpscale.volumeId) {
        if (comicUpscale.error) {
            text = 'Amélioration impossible';
            tooltip = comicUpscale.error;
        } else {
            const pending = comicUpscale.shown.some(number => {
                const key = _comicUpscaleKey(number);
                return key === comicUpscale.running || (!comicUpscale.cache.has(key) && comicUpscale.queue.includes(number));
            });
            if (pending) text = 'Amélioration…';
        }
    }
    status.textContent = text;
    status.hidden = !text;
    status.classList.toggle('error', !!comicUpscale.error);
    if (tooltip) status.dataset.tooltip = tooltip; else delete status.dataset.tooltip;
}
