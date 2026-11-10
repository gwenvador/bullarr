// Lecteur de BD intégré (fiche série) : lit un tome page par page directement dans son
// fichier (GET /api/volumes/<id>/reader, /reader/page/<n> et /reader/thumb/<n>, CBZ, CBR ou PDF).
// Clic sur la moitié droite : page suivante ; moitié gauche : page précédente. Molette,
// touches ← → / Espace / Origine / Fin, glissement du doigt, curseur de position, plein
// écran (bouton ou touche F). Ajustement de l'image (bouton ou touche Z) : page entière,
// largeur de l'écran ou hauteur de l'écran. Bande de miniatures de toutes les pages
// (bouton ou touche M). Zoom (boutons − / +, touches + − 0, Ctrl/⌘ + molette ou pincement
// du pavé tactile, pincement au doigt) avec déplacement de l'image à la souris ou au doigt.
// Deux pages côte à côte (bouton ou touche D) : la couverture seule, puis les doubles pages
// 2-3, 4-5… comme dans un album ouvert. Mode webtoon (bouton ou touche W) : toutes les pages
// à la suite, en défilement vertical continu. Sens manga (bouton ou touche R, mémorisé par
// série, d'office pour une série d'origine Asie) : clics, flèches, glissements, double page et
// miniatures de droite à gauche. Tome précédent / suivant (boutons ⏮ ⏭, touches [ ]) ; à la
// dernière page, tourner la page ouvre le tome suivant (et à la première, le précédent).
// La dernière page lue de chaque tome, l'ajustement, l'affichage des miniatures, le mode deux
// pages et le mode webtoon sont mémorisés. Bouton ? (ou touche ?) : liste des raccourcis.
const comicReader = {
    volumeId: null,
    page: 1,
    count: 0,
    title: '',
    wheelLock: 0,
    touchX: null,
    fit: 'page',
    thumbs: true,
    spread: false,
    zoom: 1,
    panX: 0,
    panY: 0,
    drag: null,
    pinch: null,
    suppressClick: false,
    progressTimer: null,
    progressPending: null,
    progressReady: false,
    webtoon: false,
    rtl: false,
    seriesId: null,
    previous: null,
    next: null,
    webtoonFrame: null,
    webtoonSettleUntil: 0,
};

// Deux pages côte à côte : jamais en webtoon (pages à la suite).
function _comicReaderSpread() {
    return comicReader.spread && !comicReader.webtoon;
}

// Sens de lecture effectif : le webtoon se lit toujours de haut en bas.
function _comicReaderRtl() {
    return comicReader.rtl && !comicReader.webtoon;
}

const COMIC_READER_ZOOM_MAX = 6;
const COMIC_READER_ZOOM_STEP = 1.4;

const COMIC_READER_FITS = {
    page: { label: 'Page entière', next: 'width' },
    width: { label: 'Largeur', next: 'height' },
    height: { label: 'Hauteur', next: 'page' },
};

// Pages affichées ensemble. Mode deux pages : la page 1 (couverture) seule, puis des paires
// qui commencent à une page paire (2-3, 4-5…) ; la dernière page est seule si elle est impaire.
function _comicReaderNormalizePage(number, count, spread) {
    const page = Math.min(Math.max(1, number), Math.max(1, count));
    return spread && page > 1 && page % 2 === 1 ? page - 1 : page;
}

function _comicReaderSpreadPages(start, count, spread) {
    return spread && start > 1 && start + 1 <= count ? [start, start + 1] : [start];
}

// Première page de la vue suivante / précédente (null : déjà au bout).
function _comicReaderStepTarget(page, step, count, spread) {
    let target;
    if (spread) {
        if (step > 0) target = page === 1 ? 2 : page + 2;
        else target = page <= 2 ? 1 : page - 2;
    } else {
        target = page + step;
    }
    return target < 1 || target > count || target === page ? null : target;
}

// Raccourcis alignés sur ceux du lecteur Komga (L / R / V / W, C, D, F, M, T, H, Échap).
// Chaque ligne : [touches (tableau = touches clavier, texte = geste), description].
const COMIC_READER_SHORTCUTS = [
    ['Navigation', [
        [['→'], 'Page suivante (← en sens manga)'],
        [['←'], 'Page précédente (→ en sens manga)'],
        [['Espace'], 'Page suivante'],
        [['Maj + Espace'], 'Page précédente'],
        [['PageDown'], 'Page suivante'],
        [['PageUp'], 'Page précédente'],
        [['Début'], 'Première page'],
        [['Fin'], 'Dernière page'],
        [['['], 'Tome précédent'],
        [[']'], 'Tome suivant'],
    ]],
    ['Lecture', [
        [['L'], 'Gauche → droite'],
        [['R'], 'Droite → gauche (manga), mémorisé par série'],
        [['V', 'W'], 'Webtoon : défilement vertical continu'],
        [['C', 'Z'], 'Ajustement : page entière, largeur, hauteur'],
        [['D'], 'Une ou deux pages côte à côte'],
        [['F'], 'Plein écran'],
    ]],
    ['Menus', [
        [['M'], 'Afficher / masquer les barres'],
        [['T'], 'Afficher / masquer les miniatures'],
        [['H', '?'], 'Afficher / masquer cette aide'],
        [['Échap'], 'Fermer l\'aide, puis le lecteur'],
    ]],
    ['Zoom et gestes', [
        [['+', '−'], 'Zoomer / dézoomer'],
        [['0'], 'Revenir à 100 %'],
        ['Ctrl / ⌘ + molette', 'Zoomer autour du pointeur'],
        ['Clic droite / gauche', 'Page suivante / précédente'],
        ['Glisser un doigt', 'Page suivante / précédente'],
    ]],
];

function _comicReaderHelpHtml() {
    const escape = text => String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const keysHtml = keys => Array.isArray(keys)
        ? keys.map(key => `<kbd>${escape(key)}</kbd>`).join('<span class="comic-reader-help-or">ou</span>')
        : `<span class="comic-reader-help-gesture">${escape(keys)}</span>`;
    const groups = COMIC_READER_SHORTCUTS.map(([title, rows]) => `
        <section class="comic-reader-help-group">
            <h3>${escape(title)}</h3>
            <table><thead><tr><th>Touche</th><th>Description</th></tr></thead><tbody>
            ${rows.map(([keys, label]) => `<tr><td class="comic-reader-help-keys">${keysHtml(keys)}</td><td>${escape(label)}</td></tr>`).join('')}
            </tbody></table>
        </section>`).join('');
    return `<div class="comic-reader-help-card" role="document"><button type="button" class="comic-reader-help-x" aria-label="Fermer l'aide">×</button><div class="comic-reader-help-grid">${groups}</div></div>`;
}

// « M » : masque la barre du haut et le pied (miniatures, curseur), comme Komga.
function toggleComicReaderChrome(force) {
    const overlay = document.getElementById('comic-reader');
    if (!overlay) return;
    const hide = force === undefined ? !overlay.classList.contains('chrome-hidden') : !!force;
    overlay.classList.toggle('chrome-hidden', hide);
    requestAnimationFrame(() => { if (!comicReader.webtoon) _layoutComicReaderPages(); });
}

// Sens de lecture explicite (L / R) : quitte le webtoon si besoin.
function setComicReaderMode(mode) {
    if (mode === 'webtoon') {
        if (!comicReader.webtoon) toggleComicReaderWebtoon();
        return;
    }
    if (comicReader.webtoon) toggleComicReaderWebtoon();
    if (comicReader.rtl !== (mode === 'rtl')) toggleComicReaderRtl();
}

function toggleComicReaderHelp(force) {
    const help = document.getElementById('comic-reader-help');
    if (!help) return;
    const show = force === undefined ? help.hidden : !!force;
    help.hidden = !show;
    document.getElementById('comic-reader-help-btn')?.classList.toggle('active', show);
}

// Envoi de la page lue à Komga (côté serveur, voir POST /api/volumes/<id>/reader/progress) : groupé
// (2,5 s après le dernier changement de page) et jamais bloquant. Rien n'est envoyé à l'ouverture,
// seulement une fois que l'on a tourné une page, pour ne pas écraser la progression existante.
const COMIC_READER_PROGRESS_DELAY_MS = 2500;

function _scheduleComicReaderProgress() {
    const shown = _comicReaderSpreadPages(comicReader.page, comicReader.count, _comicReaderSpread());
    const page = shown[shown.length - 1];
    comicReader.progressPending = { volumeId: comicReader.volumeId, page, completed: page >= comicReader.count };
    clearTimeout(comicReader.progressTimer);
    comicReader.progressTimer = setTimeout(_flushComicReaderProgress, COMIC_READER_PROGRESS_DELAY_MS);
}

function _flushComicReaderProgress() {
    clearTimeout(comicReader.progressTimer);
    const pending = comicReader.progressPending;
    comicReader.progressPending = null;
    if (!pending) return;
    fetch(`/api/volumes/${pending.volumeId}/reader/progress`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ page: pending.page, completed: pending.completed })
    }).catch(() => { /* Komga est facultatif : on ne gêne jamais la lecture */ });
}

function _comicReaderStorageKey(volumeId) {
    return `comic-reader-page-${volumeId}`;
}

function _comicReaderPageUrl(number) {
    return `/api/volumes/${comicReader.volumeId}/reader/page/${number}`;
}

function _comicReaderThumbUrl(number) {
    return `/api/volumes/${comicReader.volumeId}/reader/thumb/${number}`;
}

function _comicReaderPref(key, fallback) {
    try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; }
}

function _comicReaderSavePref(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* stockage indisponible */ }
}

function _buildComicReader() {
    let overlay = document.getElementById('comic-reader');
    if (overlay) return overlay;
    comicReader.fit = COMIC_READER_FITS[_comicReaderPref('comic-reader-fit', 'page')] ? _comicReaderPref('comic-reader-fit', 'page') : 'page';
    comicReader.thumbs = _comicReaderPref('comic-reader-thumbs', '1') !== '0';
    comicReader.spread = _comicReaderPref('comic-reader-spread', '0') === '1';
    comicReader.webtoon = _comicReaderPref('comic-reader-webtoon', '0') === '1';
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
                <button type="button" class="comic-reader-btn" data-reader-action="zoom-out" aria-label="Dézoomer" data-tooltip="Dézoomer (−)">−</button>
                <button type="button" class="comic-reader-btn comic-reader-btn-text" data-reader-action="zoom-reset" id="comic-reader-zoom" aria-label="Zoom : revenir à 100 %" data-tooltip="Revenir à 100 % (0)">100 %</button>
                <button type="button" class="comic-reader-btn" data-reader-action="zoom-in" aria-label="Zoomer" data-tooltip="Zoomer (+)">+</button>
                <button type="button" class="comic-reader-btn" data-reader-action="prev-volume" id="comic-reader-prev-volume" aria-label="Tome précédent" data-tooltip="Tome précédent ([)" hidden>⏮</button>
                <button type="button" class="comic-reader-btn" data-reader-action="next-volume" id="comic-reader-next-volume" aria-label="Tome suivant" data-tooltip="Tome suivant (])" hidden>⏭</button>
                <button type="button" class="comic-reader-btn comic-reader-btn-text" data-reader-action="webtoon" id="comic-reader-webtoon-btn" aria-label="Mode webtoon" data-tooltip="Mode webtoon : défilement vertical continu (W ou V)">↕<span class="comic-reader-btn-label">Webtoon</span></button>
                <button type="button" class="comic-reader-btn comic-reader-btn-text" data-reader-action="rtl" id="comic-reader-rtl-btn" aria-label="Sens manga" data-tooltip="Sens manga : droite → gauche (R, L pour revenir)">⇆<span class="comic-reader-btn-label">Manga</span></button>
                <button type="button" class="comic-reader-btn comic-reader-btn-spread" data-reader-action="spread" id="comic-reader-spread-btn" aria-label="Deux pages côte à côte" data-tooltip="Deux pages côte à côte (D)">▯▯</button>
                <button type="button" class="comic-reader-btn comic-reader-btn-text" data-reader-action="fit" id="comic-reader-fit" aria-label="Ajustement de l'image" data-tooltip="Ajustement de l'image : page entière, largeur ou hauteur de l'écran (C)"></button>
                <button type="button" class="comic-reader-btn" data-reader-action="thumbs" id="comic-reader-thumbs-btn" aria-label="Miniatures des pages" data-tooltip="Miniatures des pages (T)">▦</button>
                <button type="button" class="comic-reader-btn" data-reader-action="left" id="comic-reader-left-btn" aria-label="Page précédente" data-tooltip="Page précédente (←)">‹</button>
                <button type="button" class="comic-reader-btn" data-reader-action="right" id="comic-reader-right-btn" aria-label="Page suivante" data-tooltip="Page suivante (→)">›</button>
                <button type="button" class="comic-reader-btn" data-reader-action="fullscreen" aria-label="Plein écran" data-tooltip="Plein écran (F)">⛶</button>
                <button type="button" class="comic-reader-btn" data-reader-action="help" id="comic-reader-help-btn" aria-label="Raccourcis clavier" data-tooltip="Raccourcis clavier (H)">?</button>
                <button type="button" class="comic-reader-btn" data-reader-action="close" aria-label="Fermer le lecteur" data-tooltip="Fermer (Échap)">×</button>
            </div>
        </div>
        <div class="comic-reader-stage" id="comic-reader-stage" tabindex="-1">
            <div class="comic-reader-pages" id="comic-reader-pages">
                <img class="comic-reader-image" id="comic-reader-image" alt="">
                <img class="comic-reader-image" id="comic-reader-image-2" alt="" hidden>
            </div>
            <div class="comic-reader-webtoon" id="comic-reader-webtoon" hidden></div>
            <div class="comic-reader-status" id="comic-reader-status"></div>
        </div>
        <div class="comic-reader-footer">
            <div class="comic-reader-edge" id="comic-reader-edge" hidden></div>
            <div class="comic-reader-thumbs" id="comic-reader-thumbs" aria-label="Miniatures des pages"></div>
            <input type="range" class="comic-reader-slider" id="comic-reader-slider" min="1" max="1" value="1" aria-label="Aller à la page">
        </div>
        <div class="comic-reader-help" id="comic-reader-help" hidden></div>`;
    document.body.appendChild(overlay);
    const help = overlay.querySelector('#comic-reader-help');
    help.innerHTML = _comicReaderHelpHtml();
    help.addEventListener('click', event => { event.stopPropagation(); toggleComicReaderHelp(false); });

    overlay.querySelectorAll('[data-reader-action]').forEach(button => {
        button.addEventListener('click', event => {
            event.stopPropagation();
            const action = button.dataset.readerAction;
            if (action === 'left') stepComicReader(_comicReaderRtl() ? 1 : -1);
            else if (action === 'right') stepComicReader(_comicReaderRtl() ? -1 : 1);
            else if (action === 'prev-volume') openComicReaderNeighbour(-1);
            else if (action === 'next-volume') openComicReaderNeighbour(1);
            else if (action === 'webtoon') toggleComicReaderWebtoon();
            else if (action === 'rtl') toggleComicReaderRtl();
            else if (action === 'fit') cycleComicReaderFit();
            else if (action === 'spread') toggleComicReaderSpread();
            else if (action === 'help') toggleComicReaderHelp();
            else if (action === 'zoom-in') zoomComicReaderBy(COMIC_READER_ZOOM_STEP);
            else if (action === 'zoom-out') zoomComicReaderBy(1 / COMIC_READER_ZOOM_STEP);
            else if (action === 'zoom-reset') resetComicReaderZoom();
            else if (action === 'thumbs') toggleComicReaderThumbs();
            else if (action === 'fullscreen') toggleComicReaderFullscreen();
            else if (action === 'close') closeComicReader();
        });
    });
    const stage = overlay.querySelector('#comic-reader-stage');
    stage.addEventListener('click', event => {
        // Un glissement pour déplacer l'image agrandie ne tourne pas la page ; image
        // agrandie, un simple clic non plus (on revient à 100 % avec 0 ou le bouton).
        if (comicReader.suppressClick) { comicReader.suppressClick = false; return; }
        if (comicReader.zoom > 1 || comicReader.webtoon) return;  // webtoon : on fait défiler
        const rect = stage.getBoundingClientRect();
        const left = event.clientX - rect.left < rect.width / 2;
        stepComicReader(left === !_comicReaderRtl() ? -1 : 1);
    });
    // Webtoon : la page en cours est celle qui occupe le milieu de l'écran.
    stage.addEventListener('scroll', () => {
        if (!comicReader.webtoon || comicReader.webtoonFrame) return;
        comicReader.webtoonFrame = requestAnimationFrame(() => {
            comicReader.webtoonFrame = null;
            _trackComicReaderWebtoonPage();
        });
    }, { passive: true });
    overlay.querySelector('#comic-reader-edge').addEventListener('click', event => {
        const button = event.target.closest('[data-neighbour]');
        if (button) { event.stopPropagation(); openComicReaderNeighbour(parseInt(button.dataset.neighbour, 10)); }
    });
    stage.addEventListener('wheel', event => {
        if (comicReader.webtoon) return;  // défilement natif
        // Ctrl/⌘ + molette (et pincement du pavé tactile) : zoom autour du pointeur.
        if (event.ctrlKey || event.metaKey) {
            event.preventDefault();
            const rect = stage.getBoundingClientRect();
            const delta = Math.max(-30, Math.min(30, event.deltaY));
            zoomComicReaderBy(Math.exp(-delta * 0.01), event.clientX - rect.left, event.clientY - rect.top);
            return;
        }
        // Image zoomée : la molette la déplace.
        if (comicReader.zoom > 1) {
            event.preventDefault();
            _panComicReader(-event.deltaX, -event.deltaY);
            return;
        }
        // Image agrandie (largeur / hauteur) : la molette fait défiler l'image.
        if (comicReader.fit !== 'page') return;
        event.preventDefault();
        const now = Date.now();
        if (now < comicReader.wheelLock || Math.abs(event.deltaY) < 10) return;
        comicReader.wheelLock = now + 350;
        stepComicReader(event.deltaY > 0 ? 1 : -1);
    }, { passive: false });
    stage.addEventListener('mousedown', event => {
        if (event.button !== 0 || comicReader.zoom <= 1) return;
        comicReader.drag = { x: event.clientX, y: event.clientY, panX: comicReader.panX, panY: comicReader.panY, moved: false };
        stage.classList.add('dragging');
        event.preventDefault();
    });
    document.addEventListener('mousemove', event => {
        const drag = comicReader.drag;
        if (!drag) return;
        const dx = event.clientX - drag.x;
        const dy = event.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        comicReader.panX = drag.panX + dx;
        comicReader.panY = drag.panY + dy;
        _clampComicReaderPan();
        _applyComicReaderZoom();
    });
    document.addEventListener('mouseup', () => {
        const drag = comicReader.drag;
        if (!drag) return;
        comicReader.drag = null;
        stage.classList.remove('dragging');
        if (drag.moved) comicReader.suppressClick = true;
    });
    const touchPoint = touch => {
        const rect = stage.getBoundingClientRect();
        return { x: touch.clientX - rect.left, y: touch.clientY - rect.top };
    };
    stage.addEventListener('touchstart', event => {
        if (comicReader.webtoon) return;  // défilement natif
        if (event.touches.length === 2) {
            // Pincement : zoom autour du milieu des deux doigts.
            const [a, b] = [touchPoint(event.touches[0]), touchPoint(event.touches[1])];
            comicReader.pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, zoom: comicReader.zoom };
            comicReader.touchX = null;
            comicReader.drag = null;
            return;
        }
        comicReader.touchX = event.changedTouches[0].clientX;
        if (comicReader.zoom > 1) {
            comicReader.drag = { x: event.touches[0].clientX, y: event.touches[0].clientY, panX: comicReader.panX, panY: comicReader.panY, moved: false, touch: true };
        }
    }, { passive: true });
    stage.addEventListener('touchmove', event => {
        if (comicReader.pinch && event.touches.length === 2) {
            event.preventDefault();
            const [a, b] = [touchPoint(event.touches[0]), touchPoint(event.touches[1])];
            const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
            _setComicReaderZoom(comicReader.pinch.zoom * dist / comicReader.pinch.dist, (a.x + b.x) / 2, (a.y + b.y) / 2);
        } else if (comicReader.drag && comicReader.drag.touch && event.touches.length === 1) {
            event.preventDefault();
            const drag = comicReader.drag;
            drag.moved = true;
            comicReader.panX = drag.panX + event.touches[0].clientX - drag.x;
            comicReader.panY = drag.panY + event.touches[0].clientY - drag.y;
            _clampComicReaderPan();
            _applyComicReaderZoom();
        }
    }, { passive: false });
    stage.addEventListener('touchend', event => {
        if (comicReader.webtoon) return;
        if (comicReader.pinch) {
            if (event.touches.length < 2) comicReader.pinch = null;
            comicReader.touchX = null;
            return;
        }
        if (comicReader.drag && comicReader.drag.touch) {
            const moved = comicReader.drag.moved;
            comicReader.drag = null;
            if (moved) event.preventDefault();  // pas de clic fantôme après un déplacement
        }
        if (comicReader.touchX === null || comicReader.zoom > 1) { comicReader.touchX = null; return; }
        const dx = event.changedTouches[0].clientX - comicReader.touchX;
        comicReader.touchX = null;
        // Image plus large que l'écran (mode hauteur) : le glissement sert à la parcourir.
        if (comicReader.fit === 'height' && stage.scrollWidth > stage.clientWidth + 2) return;
        if (Math.abs(dx) > 40) {
            event.preventDefault();  // pas de clic fantôme après un glissement
            stepComicReader((dx < 0) === !_comicReaderRtl() ? 1 : -1);
        }
    });
    const thumbs = overlay.querySelector('#comic-reader-thumbs');
    // Molette verticale sur la bande : défilement horizontal.
    thumbs.addEventListener('wheel', event => {
        if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
        event.preventDefault();
        thumbs.scrollLeft += event.deltaY;
    }, { passive: false });
    thumbs.addEventListener('click', event => {
        const thumb = event.target.closest('[data-page]');
        if (thumb) showComicReaderPage(parseInt(thumb.dataset.page, 10));
    });
    const slider = overlay.querySelector('#comic-reader-slider');
    slider.addEventListener('input', () => {
        document.getElementById('comic-reader-counter').textContent = `${slider.value} / ${comicReader.count}`;
    });
    slider.addEventListener('change', () => showComicReaderPage(parseInt(slider.value, 10)));
    overlay.querySelectorAll('.comic-reader-image').forEach(image => {
        image.addEventListener('load', () => {
            document.getElementById('comic-reader-status').textContent = '';
            _layoutComicReaderPages();
        });
        image.addEventListener('error', () => {
            document.getElementById('comic-reader-status').textContent = `Page ${image.dataset.page || comicReader.page} illisible`;
        });
    });
    // La taille des pages dépend de celle de l'écran : redimensionnement, plein écran, rotation.
    window.addEventListener('resize', () => {
        if (!overlay.hidden) { _layoutComicReaderPages(); _clampComicReaderPan(); _applyComicReaderZoom(); }
    });
    // Phase de capture : le lecteur reçoit les touches avant la page (la fiche série utilise
    // ← → pour passer à la série voisine) et ne les laisse pas passer tant qu'il est ouvert.
    document.addEventListener('keydown', _comicReaderKeydown, true);
    _applyComicReaderFit();
    _applyComicReaderThumbs();
    _applyComicReaderWebtoon();
    return overlay;
}

function _comicReaderKeydown(event) {
    const overlay = document.getElementById('comic-reader');
    if (!overlay || overlay.hidden) return;
    event.stopImmediatePropagation();
    const keys = {
        ArrowRight: () => stepComicReader(_comicReaderRtl() ? -1 : 1), ArrowLeft: () => stepComicReader(_comicReaderRtl() ? 1 : -1),
        '[': () => openComicReaderNeighbour(-1), ']': () => openComicReaderNeighbour(1),
        w: () => toggleComicReaderWebtoon(), W: () => toggleComicReaderWebtoon(),
        v: () => setComicReaderMode('webtoon'), V: () => setComicReaderMode('webtoon'),
        l: () => setComicReaderMode('ltr'), L: () => setComicReaderMode('ltr'),
        r: () => setComicReaderMode('rtl'), R: () => setComicReaderMode('rtl'),
        ' ': () => stepComicReader(event.shiftKey ? -1 : 1), PageDown: () => stepComicReader(1),
        PageUp: () => stepComicReader(-1), Home: () => showComicReaderPage(1),
        End: () => showComicReaderPage(comicReader.count), Escape: () => closeComicReader(),
        f: () => toggleComicReaderFullscreen(), F: () => toggleComicReaderFullscreen(),
        z: () => cycleComicReaderFit(), Z: () => cycleComicReaderFit(),
        c: () => cycleComicReaderFit(), C: () => cycleComicReaderFit(),
        t: () => toggleComicReaderThumbs(), T: () => toggleComicReaderThumbs(),
        m: () => toggleComicReaderChrome(), M: () => toggleComicReaderChrome(),
        d: () => toggleComicReaderSpread(), D: () => toggleComicReaderSpread(),
        '?': () => toggleComicReaderHelp(), h: () => toggleComicReaderHelp(), H: () => toggleComicReaderHelp(),
        '+': () => zoomComicReaderBy(COMIC_READER_ZOOM_STEP), '=': () => zoomComicReaderBy(COMIC_READER_ZOOM_STEP),
        '-': () => zoomComicReaderBy(1 / COMIC_READER_ZOOM_STEP), _: () => zoomComicReaderBy(1 / COMIC_READER_ZOOM_STEP),
        0: () => resetComicReaderZoom(),
    };
    if (event.target && event.target.matches && event.target.matches('input[type="range"]')
        && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) return;
    const handler = keys[event.key];
    if (!handler) return;
    // Échap ferme d'abord l'aide des raccourcis, puis le lecteur.
    if (event.key === 'Escape' && !document.getElementById('comic-reader-help')?.hidden) {
        event.preventDefault();
        toggleComicReaderHelp(false);
        return;
    }
    // Échap en plein écran : le navigateur quitte d'abord le plein écran, on ne ferme pas.
    if (event.key === 'Escape' && document.fullscreenElement) return;
    event.preventDefault();
    handler();
}

function _applyComicReaderFit() {
    const stage = document.getElementById('comic-reader-stage');
    if (!stage) return;
    stage.classList.remove('fit-page', 'fit-width', 'fit-height');
    stage.classList.add(`fit-${comicReader.fit}`);
    const button = document.getElementById('comic-reader-fit');
    if (button) {
        const label = COMIC_READER_FITS[comicReader.fit].label;
        button.innerHTML = `⤢<span class="comic-reader-btn-label">${label}</span>`;
        button.setAttribute('aria-label', `Ajustement de l'image : ${label}`);
    }
}

// Zoom : l'image est agrandie par une transformation (échelle + décalage) sur la page
// affichée entière ; le décalage est borné pour ne jamais laisser de vide à côté de l'image.
function _comicReaderZoomGeometry() {
    const stage = document.getElementById('comic-reader-stage');
    const image = document.getElementById('comic-reader-pages');
    if (!stage || !image) return null;
    const sw = stage.clientWidth;
    const sh = stage.clientHeight;
    const w = image.offsetWidth;
    const h = image.offsetHeight;
    return { stage, image, sw, sh, w, h, left0: (sw - w) / 2, top0: (sh - h) / 2 };
}

function _clampComicReaderPan() {
    const g = _comicReaderZoomGeometry();
    if (!g) return;
    const z = comicReader.zoom;
    const clampAxis = (pan, stageSize, size, start) => {
        const shown = size * z;
        if (shown <= stageSize) return (stageSize - shown) / 2 - start;
        return Math.min(0, Math.max(stageSize - shown, start + pan)) - start;
    };
    comicReader.panX = clampAxis(comicReader.panX, g.sw, g.w, g.left0);
    comicReader.panY = clampAxis(comicReader.panY, g.sh, g.h, g.top0);
}

function _applyComicReaderZoom() {
    const stage = document.getElementById('comic-reader-stage');
    const image = document.getElementById('comic-reader-pages');
    if (!stage || !image) return;
    const zoomed = comicReader.zoom > 1;
    stage.classList.toggle('zoomed', zoomed);
    image.style.transform = zoomed
        ? `translate(${comicReader.panX}px, ${comicReader.panY}px) scale(${comicReader.zoom})` : '';
    const label = document.getElementById('comic-reader-zoom');
    if (label) label.textContent = `${Math.round(comicReader.zoom * 100)} %`;
}

function _setComicReaderZoom(zoom, cx, cy) {
    if (comicReader.webtoon) return;  // webtoon : zoom du navigateur (Ctrl +)
    const next = Math.min(COMIC_READER_ZOOM_MAX, zoom);
    if (next <= 1.01) { resetComicReaderZoom(); return; }
    const stage = document.getElementById('comic-reader-stage');
    if (!stage) return;
    const previous = comicReader.zoom;
    if (previous <= 1) {
        // Entrée en zoom : la page est d'abord affichée entière, sans défilement.
        stage.classList.add('zoomed');
        stage.scrollTop = 0;
        stage.scrollLeft = 0;
        comicReader.panX = 0;
        comicReader.panY = 0;
        _layoutComicReaderPages('page');
    }
    const g = _comicReaderZoomGeometry();
    if (!g) return;
    const x = cx === undefined ? g.sw / 2 : cx;
    const y = cy === undefined ? g.sh / 2 : cy;
    const ratio = next / previous;
    comicReader.panX = x - g.left0 - ratio * (x - g.left0 - comicReader.panX);
    comicReader.panY = y - g.top0 - ratio * (y - g.top0 - comicReader.panY);
    comicReader.zoom = next;
    _clampComicReaderPan();
    _applyComicReaderZoom();
}

function zoomComicReaderBy(factor, cx, cy) {
    _setComicReaderZoom(comicReader.zoom * factor, cx, cy);
}

function resetComicReaderZoom() {
    comicReader.zoom = 1;
    comicReader.panX = 0;
    comicReader.panY = 0;
    comicReader.drag = null;
    comicReader.pinch = null;
    document.getElementById('comic-reader-stage')?.classList.remove('dragging');
    _applyComicReaderZoom();
    _layoutComicReaderPages();
}

function _panComicReader(dx, dy) {
    comicReader.panX += dx;
    comicReader.panY += dy;
    _clampComicReaderPan();
    _applyComicReaderZoom();
}

// Taille des pages affichées : toutes à la même hauteur, côte à côte. « Page entière » les fait
// tenir dans l'écran, « Largeur » remplit la largeur (défilement vertical), « Hauteur » remplit la
// hauteur (défilement horizontal). Deux passes : la barre de défilement réduit la zone utile.
function _layoutComicReaderPages(fitOverride) {
    const stage = document.getElementById('comic-reader-stage');
    const images = [...document.querySelectorAll('#comic-reader-pages .comic-reader-image')].filter(image => !image.hidden);
    if (!stage || !images.length) return;
    const fit = fitOverride || (comicReader.zoom > 1 ? 'page' : comicReader.fit);
    const aspects = images.map(image => (image.naturalWidth && image.naturalHeight) ? image.naturalWidth / image.naturalHeight : 0);
    const known = aspects.find(Boolean);
    if (!known) return;  // aucune page encore chargée
    const ratios = aspects.map(aspect => aspect || known);
    const sum = ratios.reduce((total, ratio) => total + ratio, 0);
    for (let pass = 0; pass < 2; pass++) {
        const width = stage.clientWidth;
        const height = stage.clientHeight;
        const pageHeight = fit === 'width' ? width / sum : fit === 'height' ? height : Math.min(height, width / sum);
        images.forEach((image, index) => {
            image.style.height = `${pageHeight}px`;
            image.style.width = `${pageHeight * ratios[index]}px`;
        });
    }
}

function _applyComicReaderSpread() {
    document.getElementById('comic-reader-spread-btn')?.classList.toggle('active', comicReader.spread);
    // Sens manga : la première page de la double page à droite, miniatures et curseur aussi.
    const rtl = _comicReaderRtl();
    document.getElementById('comic-reader-pages')?.classList.toggle('rtl', rtl);
    const strip = document.getElementById('comic-reader-thumbs');
    if (strip) strip.dir = rtl ? 'rtl' : 'ltr';
    const slider = document.getElementById('comic-reader-slider');
    if (slider) slider.style.direction = rtl ? 'rtl' : '';
    const rtlButton = document.getElementById('comic-reader-rtl-btn');
    if (rtlButton) { rtlButton.classList.toggle('active', comicReader.rtl); rtlButton.hidden = comicReader.webtoon; }
    const spreadButton = document.getElementById('comic-reader-spread-btn');
    if (spreadButton) spreadButton.hidden = comicReader.webtoon;
}

function _comicReaderRtlKey() {
    return `comic-reader-rtl-${comicReader.seriesId}`;
}

function toggleComicReaderRtl() {
    if (comicReader.webtoon) return;
    comicReader.rtl = !comicReader.rtl;
    if (comicReader.seriesId) _comicReaderSavePref(_comicReaderRtlKey(), comicReader.rtl ? '1' : '0');
    _applyComicReaderSpread();
    if (comicReader.count) showComicReaderPage(comicReader.page);
}

function toggleComicReaderSpread() {
    comicReader.spread = !comicReader.spread;
    _comicReaderSavePref('comic-reader-spread', comicReader.spread ? '1' : '0');
    _applyComicReaderSpread();
    if (comicReader.count) showComicReaderPage(comicReader.page);
}

function _clearComicReaderImages() {
    document.querySelectorAll('#comic-reader-pages .comic-reader-image').forEach(image => {
        image.removeAttribute('src');
        image.removeAttribute('data-page');
        image.style.width = '';
        image.style.height = '';
    });
    const second = document.getElementById('comic-reader-image-2');
    if (second) second.hidden = true;
}

function cycleComicReaderFit() {
    resetComicReaderZoom();
    comicReader.fit = COMIC_READER_FITS[comicReader.fit].next;
    _comicReaderSavePref('comic-reader-fit', comicReader.fit);
    _applyComicReaderFit();
    _layoutComicReaderPages();
    const stage = document.getElementById('comic-reader-stage');
    if (stage) { stage.scrollTop = 0; stage.scrollLeft = 0; }
}

function _applyComicReaderThumbs() {
    const strip = document.getElementById('comic-reader-thumbs');
    if (strip) strip.hidden = !comicReader.thumbs;
    document.getElementById('comic-reader-thumbs-btn')?.classList.toggle('active', comicReader.thumbs);
}

function toggleComicReaderThumbs() {
    comicReader.thumbs = !comicReader.thumbs;
    _comicReaderSavePref('comic-reader-thumbs', comicReader.thumbs ? '1' : '0');
    _applyComicReaderThumbs();
    if (comicReader.thumbs) _highlightComicReaderThumb();
}

function _buildComicReaderThumbs() {
    const strip = document.getElementById('comic-reader-thumbs');
    if (!strip) return;
    const parts = [];
    for (let n = 1; n <= comicReader.count; n++) {
        parts.push(`<button type="button" class="comic-reader-thumb" data-page="${n}" aria-label="Page ${n}"><img src="${_comicReaderThumbUrl(n)}" alt="" loading="lazy" decoding="async"><span>${n}</span></button>`);
    }
    strip.innerHTML = parts.join('');
}

function _highlightComicReaderThumb() {
    const strip = document.getElementById('comic-reader-thumbs');
    if (!strip || strip.hidden) return;
    strip.querySelectorAll('.comic-reader-thumb.active').forEach(el => el.classList.remove('active'));
    const shown = _comicReaderSpreadPages(comicReader.page, comicReader.count, _comicReaderSpread());
    shown.forEach(number => strip.querySelector(`[data-page="${number}"]`)?.classList.add('active'));
    const current = strip.querySelector(`[data-page="${shown[0]}"]`);
    if (!current) return;
    // Centre la miniature de la page en cours dans la bande, sans faire défiler la page.
    const stripRect = strip.getBoundingClientRect();
    const thumbRect = current.getBoundingClientRect();
    const target = strip.scrollLeft + (thumbRect.left - stripRect.left) - (strip.clientWidth - thumbRect.width) / 2;
    // En sens manga (dir=rtl), scrollLeft est négatif : pas de borne à 0.
    strip.scrollTo({ left: strip.dir === 'rtl' ? target : Math.max(0, target), behavior: 'smooth' });
}

function showComicReaderPage(number) {
    if (!comicReader.count) return;
    if (comicReader.webtoon) {
        const page = Math.min(Math.max(1, number), comicReader.count);
        const target = document.querySelector(`#comic-reader-webtoon [data-page="${page}"]`);
        // Le défilement provoqué ici n'est pas une lecture : le suivi l'ignore un instant.
        comicReader.webtoonSettleUntil = Date.now() + 700;
        target?.scrollIntoView({ block: 'start' });
        // Une fois l'image chargée (hauteur réelle), on se recale dessus.
        if (target && !target.complete) {
            target.addEventListener('load', () => {
                if (Date.now() < comicReader.webtoonSettleUntil + 1500) target.scrollIntoView({ block: 'start' });
            }, { once: true });
        }
        _comicReaderPageShown(page, [page]);
        return;
    }
    const page = _comicReaderNormalizePage(number, comicReader.count, _comicReaderSpread());
    comicReader.page = page;
    resetComicReaderZoom();
    const shown = _comicReaderSpreadPages(page, comicReader.count, _comicReaderSpread());
    const first = document.getElementById('comic-reader-image');
    const second = document.getElementById('comic-reader-image-2');
    document.getElementById('comic-reader-status').textContent = '';
    first.dataset.page = String(shown[0]);
    first.alt = `${comicReader.title} - page ${shown[0]}`;
    first.src = _comicReaderPageUrl(shown[0]);
    if (shown[1]) {
        second.hidden = false;
        second.dataset.page = String(shown[1]);
        second.alt = `${comicReader.title} - page ${shown[1]}`;
        second.src = _comicReaderPageUrl(shown[1]);
    } else {
        second.hidden = true;
        second.removeAttribute('src');
        second.removeAttribute('data-page');
    }
    const stage = document.getElementById('comic-reader-stage');
    if (stage) { stage.scrollTop = 0; stage.scrollLeft = 0; }
    _layoutComicReaderPages();
    _comicReaderPageShown(page, shown);
    // Précharge la vue suivante pour tourner sans attente.
    const next = _comicReaderStepTarget(page, 1, comicReader.count, _comicReaderSpread());
    if (next) {
        _comicReaderSpreadPages(next, comicReader.count, _comicReaderSpread()).forEach(number => {
            const preload = new Image();
            preload.src = _comicReaderPageUrl(number);
        });
    }
}

// Ce qui suit l'affichage d'une page, en mode pages comme en webtoon : compteur, curseur,
// miniatures, reprise, envoi à Komga, encart du tome voisin.
function _comicReaderPageShown(page, shown) {
    comicReader.page = page;
    document.getElementById('comic-reader-counter').textContent =
        `${shown.length > 1 ? `${shown[0]}-${shown[1]}` : shown[0]} / ${comicReader.count}`;
    document.getElementById('comic-reader-slider').value = String(page);
    _highlightComicReaderThumb();
    _comicReaderSavePref(_comicReaderStorageKey(comicReader.volumeId), String(page));
    if (comicReader.progressReady) _scheduleComicReaderProgress();
    else comicReader.progressReady = true;  // premier affichage : pas d'envoi
    _updateComicReaderEdge(shown);
}

// Encart « Tome précédent » à la première page, « Fin de l'album · Tome suivant » à la dernière.
function _updateComicReaderEdge(shown) {
    const edge = document.getElementById('comic-reader-edge');
    if (!edge) return;
    const escape = text => String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const last = shown[shown.length - 1] >= comicReader.count;
    if (last) {
        edge.innerHTML = `<span>Fin de l'album</span>` + (comicReader.next
            ? `<button type="button" class="comic-reader-edge-btn" data-neighbour="1">Tome suivant : ${escape(comicReader.next.label)} ⏭</button>`
            : '<span class="comic-reader-edge-muted">dernier tome de la série</span>');
        edge.hidden = false;
    } else if (shown[0] === 1 && comicReader.previous) {
        edge.innerHTML = `<button type="button" class="comic-reader-edge-btn" data-neighbour="-1">⏮ Tome précédent : ${escape(comicReader.previous.label)}</button>`;
        edge.hidden = false;
    } else {
        edge.hidden = true;
    }
}

function stepComicReader(step) {
    if (comicReader.webtoon) {
        const target = comicReader.page + step;
        if (target >= 1 && target <= comicReader.count) showComicReaderPage(target);
        else openComicReaderNeighbour(step > 0 ? 1 : -1);
        return;
    }
    const target = _comicReaderStepTarget(comicReader.page, step, comicReader.count, _comicReaderSpread());
    if (target !== null) showComicReaderPage(target);
    // Au bout de l'album, tourner la page ouvre le tome voisin.
    else openComicReaderNeighbour(step > 0 ? 1 : -1);
}

function openComicReaderNeighbour(direction) {
    const target = direction > 0 ? comicReader.next : comicReader.previous;
    if (target) openComicReader(target.volume_id);
}

// ===== Mode webtoon : toutes les pages à la suite, défilement vertical continu =====
function _buildComicReaderWebtoon() {
    const container = document.getElementById('comic-reader-webtoon');
    if (!container) return;
    const parts = [];
    for (let n = 1; n <= comicReader.count; n++) {
        parts.push(`<img class="comic-reader-webtoon-page" data-page="${n}" src="${_comicReaderPageUrl(n)}" alt="${comicReader.title.replace(/"/g, '&quot;')} - page ${n}" loading="lazy" decoding="async">`);
    }
    container.innerHTML = parts.join('');
}

function _trackComicReaderWebtoonPage() {
    if (Date.now() < (comicReader.webtoonSettleUntil || 0)) return;
    const stage = document.getElementById('comic-reader-stage');
    const pages = document.querySelectorAll('#comic-reader-webtoon .comic-reader-webtoon-page');
    if (!stage || !pages.length) return;
    const middle = stage.getBoundingClientRect().top + stage.clientHeight / 2;
    let page = comicReader.page;
    for (const image of pages) {
        const rect = image.getBoundingClientRect();
        if (rect.top <= middle && rect.bottom >= middle) { page = parseInt(image.dataset.page, 10); break; }
    }
    // Tout en bas : la dernière page est atteinte même si elle est courte.
    if (stage.scrollTop + stage.clientHeight >= stage.scrollHeight - 4) page = comicReader.count;
    if (page !== comicReader.page) _comicReaderPageShown(page, [page]);
}

function _applyComicReaderWebtoon() {
    const stage = document.getElementById('comic-reader-stage');
    if (!stage) return;
    const webtoon = comicReader.webtoon;
    stage.classList.toggle('webtoon', webtoon);
    document.getElementById('comic-reader-pages').hidden = webtoon;
    document.getElementById('comic-reader-webtoon').hidden = !webtoon;
    document.getElementById('comic-reader-webtoon-btn')?.classList.toggle('active', webtoon);
    for (const id of ['comic-reader-fit', 'comic-reader-zoom']) {
        const el = document.getElementById(id);
        if (el) el.hidden = webtoon;
    }
    document.querySelectorAll('[data-reader-action="zoom-in"], [data-reader-action="zoom-out"]').forEach(el => { el.hidden = webtoon; });
    _applyComicReaderSpread();
}

function toggleComicReaderWebtoon() {
    resetComicReaderZoom();
    comicReader.webtoon = !comicReader.webtoon;
    _comicReaderSavePref('comic-reader-webtoon', comicReader.webtoon ? '1' : '0');
    _applyComicReaderWebtoon();
    if (!comicReader.count) return;
    const page = comicReader.page;
    if (comicReader.webtoon) {
        _buildComicReaderWebtoon();
        _clearComicReaderImages();
        requestAnimationFrame(() => showComicReaderPage(page));
    } else {
        document.getElementById('comic-reader-webtoon').innerHTML = '';
        showComicReaderPage(page);
    }
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
    _flushComicReaderProgress();  // lecture précédente encore en attente d'envoi
    comicReader.volumeId = volumeId;
    comicReader.count = 0;
    comicReader.progressReady = false;
    comicReader.previous = null;
    comicReader.next = null;
    resetComicReaderZoom();
    document.getElementById('comic-reader-title').textContent = 'Chargement…';
    document.getElementById('comic-reader-counter').textContent = '';
    _clearComicReaderImages();
    _applyComicReaderSpread();
    document.getElementById('comic-reader-status').textContent = '';
    document.getElementById('comic-reader-thumbs').innerHTML = '';
    document.getElementById('comic-reader-webtoon').innerHTML = '';
    document.getElementById('comic-reader-edge').hidden = true;
    overlay.hidden = false;
    document.body.classList.add('comic-reader-open');
    document.getElementById('comic-reader-stage').focus({ preventScroll: true });
    try {
        const response = await fetch(`/api/volumes/${volumeId}/reader`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.success) throw new Error(data.error || `HTTP ${response.status}`);
        if (!data.page_count) throw new Error('Aucune page lisible dans ce fichier');
        comicReader.count = data.page_count;
        comicReader.title = [data.series_title, data.label].filter(Boolean).join(' - ');
        comicReader.seriesId = data.series_id;
        comicReader.previous = data.previous || null;
        comicReader.next = data.next || null;
        // Sens manga : choix mémorisé pour la série, sinon d'office pour une origine Asie.
        const savedRtl = data.series_id ? _comicReaderPref(_comicReaderRtlKey(), null) : null;
        comicReader.rtl = savedRtl === null ? !!data.manga : savedRtl === '1';
        const prevButton = document.getElementById('comic-reader-prev-volume');
        const nextButton = document.getElementById('comic-reader-next-volume');
        prevButton.hidden = !comicReader.previous;
        nextButton.hidden = !comicReader.next;
        if (comicReader.previous) prevButton.dataset.tooltip = `Tome précédent : ${comicReader.previous.label} ([)`;
        if (comicReader.next) nextButton.dataset.tooltip = `Tome suivant : ${comicReader.next.label} (])`;
        _applyComicReaderWebtoon();
        const subtitle = data.title && !comicReader.title.includes(data.title) ? ` · ${data.title}` : '';
        document.getElementById('comic-reader-title').textContent = comicReader.title + subtitle;
        const slider = document.getElementById('comic-reader-slider');
        slider.max = String(data.page_count);
        _buildComicReaderThumbs();
        // Reprise : la page atteinte d'après Komga (tout lecteur, tout appareil), sinon la
        // dernière page lue dans ce navigateur.
        let start = data.resume_page
            || (data.read_completed ? 1 : parseInt(_comicReaderPref(_comicReaderStorageKey(volumeId), '1'), 10))
            || 1;
        // Album fini lors de la dernière lecture : on repart du début.
        if (start >= data.page_count) start = 1;
        if (comicReader.webtoon) {
            _buildComicReaderWebtoon();
            requestAnimationFrame(() => showComicReaderPage(start));
        } else {
            showComicReaderPage(start);
        }
    } catch (error) {
        document.getElementById('comic-reader-title').textContent = 'Lecture impossible';
        document.getElementById('comic-reader-status').textContent = error.message;
    }
}

function closeComicReader() {
    const overlay = document.getElementById('comic-reader');
    if (!overlay) return;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    _flushComicReaderProgress();
    resetComicReaderZoom();
    toggleComicReaderHelp(false);
    overlay.hidden = true;
    document.body.classList.remove('comic-reader-open');
    _clearComicReaderImages();
    document.getElementById('comic-reader-thumbs').innerHTML = '';
    document.getElementById('comic-reader-webtoon').innerHTML = '';
    // Laisse l'envoi de la page partir avant de relire l'avancement (rangée « Continuer la lecture »).
    setTimeout(() => document.dispatchEvent(new CustomEvent('comic-reader-closed')), 800);
}
