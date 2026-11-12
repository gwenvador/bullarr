// Amélioration des pages en basse définition (côté le plus grand ≤ seuil réglable, 1600 px par défaut) dans le lecteur
// intégré, calculée dans le navigateur : onnxruntime-web sur la carte graphique (WebGPU),
// rien ne part au serveur. Mêmes modèles que l'application Android (Real-ESRGAN et
// Real-CUGAN, convertis en ONNX), page rendue x2. Sur le serveur, l'upscaling se fait hors
// ligne pour tout un tome (molette du tome, Vérification > Albums non HD).
// La page d'origine s'affiche tout de suite, puis est remplacée dès que la version améliorée
// est prête. Les pages affichées passent en premier, puis les deux suivantes (préchargement).
// Seuil réglable (Configuration > Upscaling), partagé par nav.js ; 1600 px par défaut.
function comicUpscaleMaxSide() { return typeof bullarrLowDefMaxSide === 'function' ? bullarrLowDefMaxSide() : 1600; }
const COMIC_UPSCALE_TILE = 256;
const COMIC_UPSCALE_PAD = 12;
const COMIC_UPSCALE_PREFETCH = 2;
const COMIC_UPSCALE_CACHE = 16;
const COMIC_UPSCALE_ORT = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.22.0/dist/';
const COMIC_UPSCALE_MODES = {
    off: 'Désactivée',
    browser: 'Activée',
};
// Même catalogue que blueprints/library/upscale.py et l'application Android. scale : facteur
// du modèle ; border : bordure consommée par le modèle ; even : côtés pairs exigés.
const COMIC_UPSCALE_MODELS = {
    fast: { label: 'Rapide', file: 'realesrgan-fast.onnx', bytes: 2492874, scale: 4, border: 0, even: false,
        detail: 'Nettoie le grain et le flou JPEG, lettrage net. Conseillé.' },
    general: { label: 'Général', file: 'realesrgan-general.onnx', bytes: 4866394, scale: 4, border: 0, even: false,
        detail: 'Real-ESRGAN polyvalent ×4 : traits très nets, un peu plus lent que Rapide.' },
    general_wdn: { label: 'Général débruité', file: 'realesrgan-general-wdn.onnx', bytes: 4866394, scale: 4, border: 0, even: false,
        detail: 'Comme Général, en lissant davantage le grain des scans abîmés.' },
    cugan_se: { label: 'Real-CUGAN', file: 'realcugan-se.onnx', bytes: 5159347, scale: 2, border: 18, even: true,
        detail: 'Fidèle au dessin d’origine, garde la trame et les textures.' },
    cugan_se_dn3: { label: 'Real-CUGAN débruité', file: 'realcugan-se-denoise3x.onnx', bytes: 5159347, scale: 2, border: 18, even: true,
        detail: 'Aplats propres et traits nets, efface le grain du papier.' },
    cugan_pro: { label: 'Real-CUGAN Pro', file: 'realcugan-pro.onnx', bytes: 5159347, scale: 2, border: 18, even: true,
        detail: 'Version plus récente de Real-CUGAN, un peu plus nette.' },
    cugan_pro_dn3: { label: 'Real-CUGAN Pro débruité', file: 'realcugan-pro-denoise3x.onnx', bytes: 5159347, scale: 2, border: 18, even: true,
        detail: 'Real-CUGAN Pro avec débruitage fort.' },
    quality: { label: 'Qualité', file: 'realesrgan-quality.onnx', bytes: 17938609, scale: 4, border: 0, even: false,
        detail: 'Real-ESRGAN anime ×4 : traits et lettrage les plus fins, aplats lissés. Beaucoup plus lent.' },
};
Object.values(COMIC_UPSCALE_MODELS).forEach(spec => { spec.url = `/static/models/${spec.file}`; });

const comicUpscale = {
    mode: 'off',
    model: 'fast',
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
        // « server » : ancienne valeur (calcul page par page sur le serveur, retiré).
        if (mode in COMIC_UPSCALE_MODES) comicUpscale.mode = mode;
        else if (mode === 'server') comicUpscale.mode = 'browser';
        if (model in COMIC_UPSCALE_MODELS) comicUpscale.model = model;
    } catch (e) { /* stockage indisponible */ }
})();

// ----- Modèles téléchargés dans ce navigateur (Configuration > Upscaling) -----
// Comme dans l'application Android : rien n'est téléchargé tant qu'on ne l'a pas demandé.
// Les modèles et le moteur WebGPU (onnxruntime-web) sont gardés dans le Cache Storage du
// navigateur ; « Supprimer » libère la place.
const COMIC_UPSCALE_STORE = 'bullarr-upscale-v1';
const COMIC_UPSCALE_RUNTIME = { url: `${COMIC_UPSCALE_ORT}ort-wasm-simd-threaded.jsep.wasm`, bytes: 21872216 };
const COMIC_UPSCALE_FILES = COMIC_UPSCALE_MODELS;
const comicUpscaleStore = { installed: null, downloading: {} };

function comicUpscaleStorageSupported() {
    return typeof caches !== 'undefined' && window.isSecureContext;
}

function comicUpscaleFormatSize(bytes) {
    if (!bytes) return '0 Mo';
    if (bytes >= 1e9) return `${(bytes / 1e9).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} Go`;
    return bytes >= 1e6 ? `${(bytes / 1e6).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} Mo`
        : `${Math.max(1, Math.round(bytes / 1e3))} Ko`;
}

// { <modèle>: bool, runtime: bool }, mis en cache après le premier appel.
async function comicUpscaleInstalled(refresh) {
    if (comicUpscaleStore.installed && !refresh) return comicUpscaleStore.installed;
    const installed = { runtime: false };
    Object.keys(COMIC_UPSCALE_FILES).forEach(model => { installed[model] = false; });
    if (comicUpscaleStorageSupported()) {
        try {
            const cache = await caches.open(COMIC_UPSCALE_STORE);
            installed.runtime = !!(await cache.match(COMIC_UPSCALE_RUNTIME.url));
            for (const [model, file] of Object.entries(COMIC_UPSCALE_FILES)) installed[model] = !!(await cache.match(file.url));
        } catch (e) { /* stockage indisponible */ }
    }
    comicUpscaleStore.installed = installed;
    return installed;
}

// Taille à télécharger pour un modèle (moteur WebGPU compris s'il manque).
function comicUpscaleDownloadBytes(model) {
    const installed = comicUpscaleStore.installed || {};
    return COMIC_UPSCALE_FILES[model].bytes + (installed.runtime ? 0 : COMIC_UPSCALE_RUNTIME.bytes);
}

async function _comicUpscaleFetchInto(cache, url, onBytes) {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Téléchargement impossible (HTTP ${response.status})`);
    const reader = response.body.getReader();
    const chunks = [];
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        onBytes(value.length);
    }
    await cache.put(url, new Response(new Blob(chunks), { headers: { 'Content-Type': 'application/octet-stream' } }));
}

// Télécharge un modèle (et le moteur s'il manque). onProgress(0..1).
async function comicUpscaleDownload(model, onProgress) {
    if (!comicUpscaleStorageSupported()) throw new Error('Ce navigateur ne permet pas de garder les modèles (connexion non sécurisée)');
    if (comicUpscaleStore.downloading[model]) return comicUpscaleStore.downloading[model].promise;
    const entry = { progress: 0 };
    entry.promise = (async () => {
        const installed = await comicUpscaleInstalled(true);
        const cache = await caches.open(COMIC_UPSCALE_STORE);
        const files = [COMIC_UPSCALE_FILES[model]];
        if (!installed.runtime) files.push(COMIC_UPSCALE_RUNTIME);
        const total = files.reduce((sum, file) => sum + file.bytes, 0);
        let received = 0;
        for (const file of files) {
            await _comicUpscaleFetchInto(cache, file.url, bytes => {
                received += bytes;
                entry.progress = Math.min(1, received / total);
                onProgress?.(entry.progress);
                document.dispatchEvent(new CustomEvent('comic-upscale-store'));
            });
        }
        await comicUpscaleInstalled(true);
    })();
    comicUpscaleStore.downloading[model] = entry;
    document.dispatchEvent(new CustomEvent('comic-upscale-store'));
    try {
        await entry.promise;
    } finally {
        delete comicUpscaleStore.downloading[model];
        document.dispatchEvent(new CustomEvent('comic-upscale-store'));
    }
}

// Supprime un modèle ; le moteur part avec le dernier modèle.
async function comicUpscaleDelete(model) {
    if (!comicUpscaleStorageSupported()) return;
    const cache = await caches.open(COMIC_UPSCALE_STORE);
    await cache.delete(COMIC_UPSCALE_FILES[model].url);
    delete comicUpscale.sessions[model];
    const installed = await comicUpscaleInstalled(true);
    if (!Object.keys(COMIC_UPSCALE_FILES).some(name => installed[name])) {
        await caches.delete(COMIC_UPSCALE_STORE);
        await comicUpscaleInstalled(true);
    }
    document.dispatchEvent(new CustomEvent('comic-upscale-store'));
}

async function _comicUpscaleStored(url) {
    if (!comicUpscaleStorageSupported()) return null;
    const response = await (await caches.open(COMIC_UPSCALE_STORE)).match(url);
    return response ? response.arrayBuffer() : null;
}

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

function comicReaderUpscaleOpen(volumeId) {
    _comicUpscaleClear();
    comicUpscale.volumeId = volumeId;
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
    comicUpscale.missing = null;
    comicUpscale.cache.forEach(entry => { if (entry.url) URL.revokeObjectURL(entry.url); });
    comicUpscale.cache.clear();
    _renderComicUpscaleStatus();
}

// Appelé à chaque page affichée (mode pages et webtoon).
function comicReaderUpscaleShown(shown) {
    comicUpscale.shown = shown.slice();
    if (comicUpscale.mode === 'off' || !comicUpscale.volumeId || comicUpscale.error || comicUpscale.missing) {
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
            url = await _comicUpscaleInBrowser(volumeId, number, comicUpscale.model);
        } catch (error) {
            if (error instanceof _ComicUpscaleCancelled) { comicUpscale.running = null; continue; }
            if (error instanceof _ComicUpscaleMissing) {
                comicUpscale.running = null;
                comicUpscale.missing = error.model;
                comicUpscale.queue = [];
                break;
            }
            comicUpscale.error = error.message || String(error);
            console.warn('Amélioration de la page impossible', error);
        }
        comicUpscale.running = null;
        // Le lecteur a pu changer de tome pendant le calcul : résultat inutile.
        if (volumeId !== comicUpscale.volumeId) {
            if (url) URL.revokeObjectURL(url);
            continue;
        }
        if (!comicUpscale.error) {
            // Gardé même si HD a été désactivé ou le modèle changé entre-temps (il resservira),
            // mais affiché seulement s'il correspond toujours au réglage en cours.
            comicUpscale.cache.set(key, { url });
            _trimComicUpscaleCache();
            if (url && key === _comicUpscaleKey(number)) _applyComicUpscale(number, url);
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
        if (comicUpscale.shown.includes(number) && key === _comicUpscaleKey(number)) break;
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
// Modèle pas encore téléchargé dans ce navigateur : on le propose au lieu d'une erreur.
class _ComicUpscaleMissing extends Error {
    constructor(model) { super(`Modèle ${COMIC_UPSCALE_MODELS[model].label} non téléchargé dans ce navigateur`); this.model = model; }
}

// ----- Navigateur (WebGPU) -----
function _loadComicUpscaleOrt() {
    if (!comicUpscale.ortPromise) {
        comicUpscale.ortPromise = (async () => {
            if (!navigator.gpu || !(await navigator.gpu.requestAdapter().catch(() => null))) {
                throw new Error('WebGPU indisponible dans ce navigateur');
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
            // Moteur téléchargé dans ce navigateur : pas de nouveau téléchargement de 21 Mo.
            const wasm = await _comicUpscaleStored(COMIC_UPSCALE_RUNTIME.url);
            if (wasm) window.ort.env.wasm.wasmBinary = wasm;
            return window.ort;
        })();
        comicUpscale.ortPromise.catch(() => { comicUpscale.ortPromise = null; });
    }
    return comicUpscale.ortPromise;
}

async function _comicUpscaleSession(model) {
    const installed = await comicUpscaleInstalled();
    if (comicUpscaleStorageSupported() && !installed[model]) throw new _ComicUpscaleMissing(model);
    const ort = await _loadComicUpscaleOrt();
    if (!comicUpscale.sessions[model]) {
        comicUpscale.sessions[model] = (async () => {
            const source = (await _comicUpscaleStored(COMIC_UPSCALE_FILES[model].url)) || COMIC_UPSCALE_FILES[model].url;
            return ort.InferenceSession.create(source, { executionProviders: ['webgpu'], graphOptimizationLevel: 'all' });
        })();
        comicUpscale.sessions[model].catch(() => { delete comicUpscale.sessions[model]; });
    }
    return { ort, session: await comicUpscale.sessions[model] };
}

function _comicUpscaleMargin(spec) {
    return spec.border || COMIC_UPSCALE_PAD;
}

// Découpe d'un côté de `size` px (même calcul que tile_spans, blueprints/library/upscale.py) :
// [début, fin] des zones gardées ; chaque tuile lue déborde de la marge tout autour.
function _comicUpscaleSpans(size, spec) {
    let core = COMIC_UPSCALE_TILE - 2 * _comicUpscaleMargin(spec);
    if (spec.even) core -= core % 2;
    const spans = [];
    for (let start = 0; start < size; start += core) spans.push([start, Math.min(start + core, size)]);
    return spans;
}

// Indice en miroir hors de l'image (comme numpy.pad(mode='reflect')).
function _comicUpscaleReflect(index, size) {
    if (index < 0) index = -index;
    if (index >= size) index = 2 * size - 2 - index;
    return Math.max(0, Math.min(size - 1, index));
}

async function _comicUpscaleInBrowser(volumeId, number, model) {
    const response = await fetch(`/api/volumes/${volumeId}/reader/page/${number}`);
    if (!response.ok) throw new Error(`Page ${number} : HTTP ${response.status}`);
    const bitmap = await createImageBitmap(await response.blob());
    const width = bitmap.width;
    const height = bitmap.height;
    if (!width || !height || Math.max(width, height) > comicUpscaleMaxSide()) {
        bitmap.close?.();
        return null;
    }
    const spec = COMIC_UPSCALE_MODELS[model];
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
    const margin = _comicUpscaleMargin(spec);
    const scale = spec.scale;
    // Real-CUGAN : côtés pairs (une rangée en miroir de plus si besoin).
    const fullWidth = width + (spec.even ? width % 2 : 0);
    const fullHeight = height + (spec.even ? height % 2 : 0);
    const inputName = session.inputNames[0];
    for (const [y0, y1] of _comicUpscaleSpans(fullHeight, spec)) {
        for (const [x0, x1] of _comicUpscaleSpans(fullWidth, spec)) {
            const tw = x1 - x0 + 2 * margin;
            const th = y1 - y0 + 2 * margin;
            const plane = tw * th;
            const input = new Float32Array(3 * plane);
            for (let y = 0; y < th; y++) {
                const row = _comicUpscaleReflect(y0 + y - margin, height) * width;
                let dst = y * tw;
                for (let x = 0; x < tw; x++, dst++) {
                    const src = (row + _comicUpscaleReflect(x0 + x - margin, width)) * 4;
                    input[dst] = pixels[src] / 255;
                    input[plane + dst] = pixels[src + 1] / 255;
                    input[2 * plane + dst] = pixels[src + 2] / 255;
                }
            }
            const results = await session.run({ [inputName]: new ort.Tensor('float32', input, [1, 3, th, tw]) });
            const result = results[session.outputNames[0]];
            const data = result.data;
            const oh = result.dims[2];
            const ow = result.dims[3];
            const oplane = oh * ow;
            // Zone gardée, sans ce qui dépasse de l'image (rangée ajoutée pour la parité).
            const keepWidth = Math.min(x1, width) - x0;
            const keepHeight = Math.min(y1, height) - y0;
            const cw = keepWidth * scale;
            const ch = keepHeight * scale;
            const offset = spec.border ? 0 : margin * scale;
            const image = new ImageData(cw, ch);
            const rgba = image.data;
            for (let y = 0; y < ch; y++) {
                let src = (offset + y) * ow + offset;
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
            // Sortie ramenée à x2 : assez pour l'écran, et quatre fois moins de mémoire en x4.
            outputContext.drawImage(piece, x0 * 2, y0 * 2, keepWidth * 2, keepHeight * 2);
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
        // Les pages déjà calculées restent en mémoire (clé = tome, page, mode, modèle) :
        // désactiver puis réactiver HD, ou revenir à un modèle déjà essayé, les réaffiche
        // aussitôt au lieu de les recalculer. Seule la file d'attente repart de zéro.
        comicUpscale.queue = [];
        comicUpscale.missing = null;
        if (comicUpscale.mode !== 'off' && comicReader.count) {
            comicReaderUpscaleShown(_comicReaderSpreadPages(comicReader.page, comicReader.count, _comicReaderSpread()));
        }
    }
    _renderComicUpscaleControls();
}

// « U » : active ou désactive l'amélioration.
function cycleComicReaderUpscale() {
    setComicReaderUpscale(comicUpscale.mode === 'off' ? 'browser' : 'off');
}

// Bouton HD de l'en-tête : allumé, un clic revient à la version d'origine des pages ;
// éteint, il active l'amélioration (et ouvre le choix du modèle s'il reste à télécharger).
function comicReaderHdClick() {
    if (comicUpscale.mode !== 'off') {
        setComicReaderUpscale('off');
        const panel = document.getElementById('comic-reader-settings');
        if (panel && !panel.hidden && panel.dataset.scope === 'hd') toggleComicReaderSettings(false);
        return;
    }
    setComicReaderUpscale('browser');
    const installed = comicUpscaleStore.installed;
    if (comicUpscaleStorageSupported() && installed && !installed[comicUpscale.model]) toggleComicReaderSettings(true, 'hd');
}

function toggleComicReaderUpscaleMenu(force) {
    toggleComicReaderSettings(force, 'hd');
}

// Panneau « HD » du lecteur (reader.js) : marche/arrêt puis liste des modèles, chacun avec
// son état dans ce navigateur (téléchargé ou à télécharger).
function comicReaderUpscaleSettingsHtml(segment, section) {
    const off = comicUpscale.mode === 'off';
    const storage = comicUpscaleStorageSupported();
    const installed = comicUpscaleStore.installed || {};
    const models = Object.entries(COMIC_UPSCALE_MODELS).map(([model, spec]) => {
        const pending = comicUpscaleStore.downloading[model];
        const ready = !storage || installed[model];
        const state = pending
            ? `<span class="comic-reader-model-state">${Math.round(pending.progress * 100)} %</span>`
            : ready ? `<span class="comic-reader-model-state ready" data-tooltip="Téléchargé dans ce navigateur">${_readerIcon('check')}</span>`
                : `<span class="comic-reader-model-state missing" data-tooltip="À télécharger (${comicUpscaleFormatSize(comicUpscaleDownloadBytes(model))})">${_readerIcon('download')}</span>`;
        return `<button type="button" class="comic-reader-model${model === comicUpscale.model ? ' active' : ''}" data-setting="upscale-model" data-value="${model}" aria-pressed="${model === comicUpscale.model}">
            <span class="comic-reader-model-text"><strong>${spec.label}</strong><small>${spec.detail}</small></span>${state}</button>`;
    }).join('');
    let download = '';
    if (!off && storage && comicUpscaleStore.installed && !installed[comicUpscale.model]) {
        const pending = comicUpscaleStore.downloading[comicUpscale.model];
        download = pending
            ? `<div class="comic-reader-settings-download"><div class="comic-reader-progress"><span style="width:${Math.round(pending.progress * 100)}%"></span></div><span>${Math.round(pending.progress * 100)} %</span></div>`
            : `<div class="comic-reader-settings-download"><span>${COMIC_UPSCALE_MODELS[comicUpscale.model].label} n’est pas encore dans ce navigateur.</span><button type="button" class="comic-reader-settings-action" data-setting="upscale-download" data-value="${comicUpscale.model}" data-tooltip="Télécharger le modèle">${_readerIcon('download')} ${comicUpscaleFormatSize(comicUpscaleDownloadBytes(comicUpscale.model))}</button></div>`;
    }
    return section('Amélioration HD', ['U'], `
        ${segment('upscale-mode', Object.entries(COMIC_UPSCALE_MODES), comicUpscale.mode)}
        <div class="comic-reader-models"${off ? ' hidden' : ''}>${models}</div>
        ${download}
        <p class="comic-reader-settings-note">Pages de ${comicUpscaleMaxSide()} px au plus, agrandies x2 sur la carte graphique de cet appareil (WebGPU).</p>`);
}

function _renderComicUpscaleControls() {
    const button = document.getElementById('comic-reader-hd-btn');
    if (button) {
        const on = comicUpscale.mode !== 'off';
        button.classList.toggle('active', on);
        button.setAttribute('aria-pressed', String(on));
        button.dataset.tooltip = on
            ? `HD activée (${COMIC_UPSCALE_MODELS[comicUpscale.model].label}) : cliquer pour revenir à la version d'origine (U)`
            : 'Activer l\'amélioration HD des pages en basse définition (U)';
    }
    if (typeof _renderComicReaderSettings === 'function') _renderComicReaderSettings();
    _renderComicUpscaleStatus();
}

function _renderComicUpscaleStatus() {
    const status = document.getElementById('comic-reader-upscale-status');
    if (!status) return;
    let text = '';
    let tooltip = '';
    if (comicUpscale.mode !== 'off' && comicUpscale.volumeId) {
        if (comicUpscale.missing) {
            text = 'Modèle HD à télécharger';
            tooltip = 'Ouvrez le menu HD pour télécharger le modèle';
        } else if (comicUpscale.error) {
            text = 'Amélioration impossible';
            tooltip = comicUpscale.error;
        } else {
            const pending = comicUpscale.shown.some(number => {
                const key = _comicUpscaleKey(number);
                return key === comicUpscale.running || (!comicUpscale.cache.has(key) && comicUpscale.queue.includes(number));
            });
            if (pending) text = 'Amélioration HD…';
            else if (comicUpscale.shown.some(number => comicUpscale.cache.get(_comicUpscaleKey(number))?.url)) text = 'HD';
        }
    }
    status.textContent = text;
    status.hidden = !text;
    status.classList.toggle('error', !!comicUpscale.error || !!comicUpscale.missing);
    status.classList.toggle('done', text === 'HD');
    if (tooltip) status.dataset.tooltip = tooltip; else delete status.dataset.tooltip;
}

async function comicReaderUpscaleDownload(model) {
    try {
        await comicUpscaleDownload(model);
        comicUpscale.missing = null;
        comicUpscale.error = '';
        if (comicUpscale.volumeId && comicReader.count) {
            comicReaderUpscaleShown(_comicReaderSpreadPages(comicReader.page, comicReader.count, _comicReaderSpread()));
        }
    } catch (error) {
        comicUpscale.error = error.message || String(error);
    }
    _renderComicUpscaleControls();
}

document.addEventListener('comic-upscale-store', () => {
    if (typeof _renderComicReaderSettings === 'function') _renderComicReaderSettings();
});
comicUpscaleInstalled();
