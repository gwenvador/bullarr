// Lecteur de BD intégré (fiche série) : lit un tome page par page directement dans son
// fichier (GET /api/volumes/<id>/reader et /reader/page/<n>, CBZ, CBR ou PDF).
// Clic sur la moitié droite : page suivante ; moitié gauche : page précédente. Molette,
// touches ← → / Espace / Origine / Fin, glissement du doigt, curseur de position, plein
// écran (bouton ou touche F). La dernière page lue de chaque tome est mémorisée.
const comicReader = {
    volumeId: null,
    page: 1,
    count: 0,
    title: '',
    wheelLock: 0,
    touchX: null,
};

function _comicReaderStorageKey(volumeId) {
    return `comic-reader-page-${volumeId}`;
}

function _comicReaderPageUrl(number) {
    return `/api/volumes/${comicReader.volumeId}/reader/page/${number}`;
}

function _buildComicReader() {
    let overlay = document.getElementById('comic-reader');
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'comic-reader';
    overlay.className = 'comic-reader';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Lecteur de BD');
    overlay.hidden = true;
    overlay.innerHTML = `
        <div class="comic-reader-bar">
            <div class="comic-reader-title" id="comic-reader-title"></div>
            <div class="comic-reader-counter" id="comic-reader-counter" aria-live="polite"></div>
            <div class="comic-reader-actions">
                <button type="button" class="comic-reader-btn" data-reader-action="prev" aria-label="Page précédente" data-tooltip="Page précédente (←)">‹</button>
                <button type="button" class="comic-reader-btn" data-reader-action="next" aria-label="Page suivante" data-tooltip="Page suivante (→)">›</button>
                <button type="button" class="comic-reader-btn" data-reader-action="fullscreen" aria-label="Plein écran" data-tooltip="Plein écran (F)">⛶</button>
                <button type="button" class="comic-reader-btn" data-reader-action="close" aria-label="Fermer le lecteur" data-tooltip="Fermer (Échap)">×</button>
            </div>
        </div>
        <div class="comic-reader-stage" id="comic-reader-stage">
            <img class="comic-reader-image" id="comic-reader-image" alt="">
            <div class="comic-reader-status" id="comic-reader-status"></div>
        </div>
        <div class="comic-reader-footer">
            <input type="range" class="comic-reader-slider" id="comic-reader-slider" min="1" max="1" value="1" aria-label="Aller à la page">
        </div>`;
    document.body.appendChild(overlay);

    overlay.querySelectorAll('[data-reader-action]').forEach(button => {
        button.addEventListener('click', event => {
            event.stopPropagation();
            const action = button.dataset.readerAction;
            if (action === 'prev') stepComicReader(-1);
            else if (action === 'next') stepComicReader(1);
            else if (action === 'fullscreen') toggleComicReaderFullscreen();
            else if (action === 'close') closeComicReader();
        });
    });
    const stage = overlay.querySelector('#comic-reader-stage');
    stage.addEventListener('click', event => {
        const rect = stage.getBoundingClientRect();
        stepComicReader(event.clientX - rect.left < rect.width / 2 ? -1 : 1);
    });
    stage.addEventListener('wheel', event => {
        event.preventDefault();
        const now = Date.now();
        if (now < comicReader.wheelLock || Math.abs(event.deltaY) < 10) return;
        comicReader.wheelLock = now + 350;
        stepComicReader(event.deltaY > 0 ? 1 : -1);
    }, { passive: false });
    stage.addEventListener('touchstart', event => { comicReader.touchX = event.changedTouches[0].clientX; }, { passive: true });
    stage.addEventListener('touchend', event => {
        if (comicReader.touchX === null) return;
        const dx = event.changedTouches[0].clientX - comicReader.touchX;
        comicReader.touchX = null;
        if (Math.abs(dx) > 40) {
            event.preventDefault();  // pas de clic fantôme après un glissement
            stepComicReader(dx < 0 ? 1 : -1);
        }
    });
    const slider = overlay.querySelector('#comic-reader-slider');
    slider.addEventListener('input', () => {
        document.getElementById('comic-reader-counter').textContent = `${slider.value} / ${comicReader.count}`;
    });
    slider.addEventListener('change', () => showComicReaderPage(parseInt(slider.value, 10)));
    const image = overlay.querySelector('#comic-reader-image');
    image.addEventListener('load', () => { document.getElementById('comic-reader-status').textContent = ''; });
    image.addEventListener('error', () => {
        document.getElementById('comic-reader-status').textContent = `Page ${comicReader.page} illisible`;
    });
    // Phase de capture : le lecteur reçoit les touches avant la page (la fiche série utilise
    // ← → pour passer à la série voisine) et ne les laisse pas passer tant qu'il est ouvert.
    document.addEventListener('keydown', _comicReaderKeydown, true);
    return overlay;
}

function _comicReaderKeydown(event) {
    const overlay = document.getElementById('comic-reader');
    if (!overlay || overlay.hidden) return;
    event.stopImmediatePropagation();
    const keys = {
        ArrowRight: () => stepComicReader(1), ArrowLeft: () => stepComicReader(-1),
        ' ': () => stepComicReader(event.shiftKey ? -1 : 1), PageDown: () => stepComicReader(1),
        PageUp: () => stepComicReader(-1), Home: () => showComicReaderPage(1),
        End: () => showComicReaderPage(comicReader.count), Escape: () => closeComicReader(),
        f: () => toggleComicReaderFullscreen(), F: () => toggleComicReaderFullscreen(),
    };
    if (event.target && event.target.matches && event.target.matches('input[type="range"]')
        && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) return;
    const handler = keys[event.key];
    if (!handler) return;
    // Échap en plein écran : le navigateur quitte d'abord le plein écran, on ne ferme pas.
    if (event.key === 'Escape' && document.fullscreenElement) return;
    event.preventDefault();
    handler();
}

function showComicReaderPage(number) {
    if (!comicReader.count) return;
    const page = Math.min(Math.max(1, number), comicReader.count);
    comicReader.page = page;
    const image = document.getElementById('comic-reader-image');
    document.getElementById('comic-reader-status').textContent = '';
    image.src = _comicReaderPageUrl(page);
    image.alt = `${comicReader.title} - page ${page}`;
    document.getElementById('comic-reader-counter').textContent = `${page} / ${comicReader.count}`;
    const slider = document.getElementById('comic-reader-slider');
    slider.value = String(page);
    try { localStorage.setItem(_comicReaderStorageKey(comicReader.volumeId), String(page)); } catch (e) { /* stockage indisponible */ }
    // Précharge les deux pages suivantes pour tourner sans attente.
    for (let next = page + 1; next <= Math.min(page + 2, comicReader.count); next++) {
        const preload = new Image();
        preload.src = _comicReaderPageUrl(next);
    }
}

function stepComicReader(step) {
    const target = comicReader.page + step;
    if (target < 1 || target > comicReader.count) return;
    showComicReaderPage(target);
}

function toggleComicReaderFullscreen() {
    const overlay = document.getElementById('comic-reader');
    if (!overlay) return;
    if (document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
    } else if (overlay.requestFullscreen) {
        overlay.requestFullscreen().catch(() => {});
    }
}

async function openComicReader(volumeId) {
    const overlay = _buildComicReader();
    comicReader.volumeId = volumeId;
    comicReader.count = 0;
    document.getElementById('comic-reader-title').textContent = 'Chargement…';
    document.getElementById('comic-reader-counter').textContent = '';
    document.getElementById('comic-reader-image').removeAttribute('src');
    document.getElementById('comic-reader-status').textContent = '';
    overlay.hidden = false;
    document.body.classList.add('comic-reader-open');
    try {
        const response = await fetch(`/api/volumes/${volumeId}/reader`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) throw new Error(data.error || `HTTP ${response.status}`);
        if (!data.page_count) throw new Error('Aucune page lisible dans ce fichier');
        comicReader.count = data.page_count;
        comicReader.title = [data.series_title, data.label].filter(Boolean).join(' - ');
        const subtitle = data.title && !comicReader.title.includes(data.title) ? ` · ${data.title}` : '';
        document.getElementById('comic-reader-title').textContent = comicReader.title + subtitle;
        const slider = document.getElementById('comic-reader-slider');
        slider.max = String(data.page_count);
        let start = 1;
        try { start = parseInt(localStorage.getItem(_comicReaderStorageKey(volumeId)) || '1', 10) || 1; } catch (e) { start = 1; }
        // Album fini lors de la dernière lecture : on repart du début.
        if (start >= data.page_count) start = 1;
        showComicReaderPage(start);
    } catch (error) {
        document.getElementById('comic-reader-title').textContent = 'Lecture impossible';
        document.getElementById('comic-reader-status').textContent = error.message;
    }
}

function closeComicReader() {
    const overlay = document.getElementById('comic-reader');
    if (!overlay) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    overlay.hidden = true;
    document.body.classList.remove('comic-reader-open');
    document.getElementById('comic-reader-image').removeAttribute('src');
}
