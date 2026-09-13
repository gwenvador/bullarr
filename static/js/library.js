if (typeof window.libraryId === 'undefined') {
    const params = new URLSearchParams(window.location.search);
    const urlLibraryId = params.get('libraryId');
    window.libraryId = urlLibraryId ? parseInt(urlLibraryId) : null;
}

let seriesData = [];
// "la page /library/2 il n'y a pas de bouton pour selectionner les series" - même pattern
// que selectedVolumeIds (fiche série): un Set d'id, partagé par les 3 vues (affiches/
// aperçu/tableau) plutôt qu'un mécanisme par vue - voir toggleSeriesSelection/
// updateSeriesBulkActionsBar plus bas.
let selectedSeriesIds = new Set();
// Filtres actifs (multi-sélection): clés comme 'missing', 'oneshot', 'ebdz-unmatched',
// 'genre:Histoire', 'author:Alcante', 'year:2020'. Plusieurs filtres d'une même catégorie
// se combinent en OU (ex: genre:Histoire OU genre:Guerre), les catégories entre elles en ET
let activeFilters = new Set();
let seriesSortMode = 'alpha'; // 'alpha', 'volumes'
let seriesViewMode = localStorage.getItem('seriesViewMode') || 'poster'; // 'poster', 'overview', 'table'
// Tri par en-tête cliquable de la vue tableau (remplace le menu "Ordonner" retiré de la
// toolbar) - appliqué en plus de/par-dessus seriesSortMode, uniquement pour cette vue
let seriesTableSort = { column: null, direction: 'asc' };
// Vue de la liste des tomes sur la fiche série (pas la même clé/état que seriesViewMode
// ci-dessus, qui concerne la liste des SÉRIES sur la page bibliothèque) - 'overview'
// (cartes, comportement historique) ou 'table' (nom/format/taille/emplacement)
let volumesViewMode = localStorage.getItem('volumesViewMode') || 'overview';
// Tri courant de la vue tableau des tomes (clic sur un en-tête de colonne) - non
// persisté (contrairement à volumesViewMode): un tri par nom sur une série n'a pas de
// raison de s'appliquer par défaut à la suivante consultée.
let volumesTableSort = { column: null, direction: 'asc' };
let currentSeriesTitle = '';
// Dernières données complètes chargées par renderSeriesDetail (page de détail d'une
// série), utilisé pour retrouver la couverture/le résumé locaux (ComicInfo.xml) même
// quand seriesData (liste de la bibliothèque) n'est pas chargée sur cette page
let currentSeriesDetail = null;

// Série précédente/suivante de la bibliothèque courante (voir renderSeriesDetail), gardée
// à ce niveau pour que le raccourci clavier ←/→ (voir listener plus bas) puisse naviguer
// sans dépendre du rendu du bandeau ⬅️/➡️ qui, lui, calcule la même chose localement
let adjacentSeriesNav = { prev: null, next: null };

// Durée de vie d'une entrée préchargée avant d'être considérée périmée (voir
// prefetchAdjacentSeries/renderSeriesDetail) - assez court pour ne jamais montrer une
// série visiblement obsolète (ex: métadonnées MAJ entre-temps dans un autre onglet),
// assez long pour couvrir le temps de lecture d'une fiche avant de cliquer ⬅️/➡️
const SERIES_PREFETCH_TTL_MS = 60000;

function _seriesPrefetchKey(id) {
    return `series-prefetch-${id}`;
}

// Précharge la page ET les données de la série précédente/suivante dès qu'on sait
// lesquelles c'est (voir renderSeriesDetail), pour qu'un clic ⬅️/➡️ (ou raccourci ←/→)
// arrive quasi instantané au lieu d'attendre un aller-retour réseau complet. Cette page
// navigue en rechargement complet (viewSeries fait un window.location.href, pas un
// fetch en place), donc un cache JS en mémoire serait perdu à chaque clic - deux
// mécanismes survivent, eux, au rechargement:
// - <link rel="prefetch"> pour la page HTML elle-même (le navigateur la récupère et la
//   garde en cache disque, prête à servir instantanément à la navigation réelle)
// - le JSON de l'API (/api/series/<id>, la partie la plus coûteuse côté serveur) stocké
//   explicitement dans sessionStorage, PAS un simple fetch() "à blanc" compté sur le
//   cache HTTP du navigateur: Flask n'envoie aucun en-tête Cache-Control/ETag sur cette
//   route, donc un fetch() de préchauffage seul ne servait jamais réellement de cache -
//   la navigation réelle refaisait systématiquement une requête fraîche (et son
//   spinner), le préchargement de page ci-dessus ne changeant rien à ça puisque c'est le
//   script de la page qui refetch les données au chargement, pas le HTML statique qui
//   les contient déjà.
function prefetchAdjacentSeries() {
    [adjacentSeriesNav.prev, adjacentSeriesNav.next].forEach(s => {
        if (!s) return;
        const pageHref = `/series/${s.id}`;
        if (!document.querySelector(`link[rel="prefetch"][href="${pageHref}"]`)) {
            const link = document.createElement('link');
            link.rel = 'prefetch';
            link.href = pageHref;
            document.head.appendChild(link);
        }
        fetch(`/api/series/${s.id}`)
            .then(r => r.ok ? r.json() : null)
            .then(data => {
                if (!data) return;
                try {
                    sessionStorage.setItem(_seriesPrefetchKey(s.id), JSON.stringify({ data, ts: Date.now() }));
                } catch (e) {
                    // Quota sessionStorage dépassé ou navigation privée: pas grave, on
                    // retombera simplement sur un fetch normal à l'arrivée
                }
            })
            .catch(() => {});
    });
}

// Lit une entrée préchargée si elle existe et n'est pas périmée (voir
// SERIES_PREFETCH_TTL_MS) - consommée une seule fois (retirée du storage) pour ne jamais
// resservir une donnée devenue obsolète après une action qui modifie la série.
function _consumeSeriesPrefetch(seriesId) {
    const key = _seriesPrefetchKey(seriesId);
    try {
        const raw = sessionStorage.getItem(key);
        if (!raw) return null;
        sessionStorage.removeItem(key);
        const { data, ts } = JSON.parse(raw);
        if (Date.now() - ts > SERIES_PREFETCH_TTL_MS) return null;
        return data;
    } catch (e) {
        return null;
    }
}

// Indique si une série est marquée one-shot, en cherchant dans seriesData ou les
// données de la page de détail actuellement affichée (même logique que ci-dessus)
function isSeriesOneshot(seriesId) {
    const fromList = seriesData.find(s => s.id === seriesId);
    if (fromList) return !!fromList.is_oneshot;
    if (currentSeriesDetail && currentSeriesDetail.id === seriesId) return !!currentSeriesDetail.is_oneshot;
    return false;
}

async function loadLibraryInfo() {
    try {
        const titleEl = document.getElementById('library-title');
        const pathEl = document.getElementById('library-path');

        // Ne charger que si on est sur la page library.html
        if (!titleEl || !pathEl) {
            return;
        }

        const response = await fetch(`/api/libraries/${libraryId}`);
        if (!response.ok) {
            titleEl.innerHTML = '<span class="header-title-icon">📚</span> Bibliothèque introuvable';
            pathEl.textContent = `Aucune bibliothèque avec l'id ${libraryId}`;
            return;
        }
        const library = await response.json();

        // emoji enveloppé dans header-title-icon (masqué en mobile, voir style.css) -
        // même traitement que le h1 statique des autres pages (voir templates), sauf ici
        // où le titre est injecté dynamiquement plutôt qu'écrit en dur dans le HTML.
        titleEl.innerHTML = `<span class="header-title-icon">📚</span> ${escapeHtml(library.name)}`;
        pathEl.textContent = library.path;
    } catch (error) {
        console.error('Erreur chargement bibliothèque:', error);
    }
}

async function renameCurrentLibrary() {
    const titleEl = document.getElementById('library-title');
    const currentName = titleEl.textContent.replace(/^📚\s*/, '');
    const newName = prompt('Nouveau nom de la bibliothèque:', currentName);

    if (!newName || !newName.trim() || newName.trim() === currentName) return;

    try {
        const response = await fetch(`/api/libraries/${libraryId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName.trim() })
        });
        const data = await response.json();

        if (data.success) {
            titleEl.textContent = `📚 ${data.name}`;
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}


// Cache local de la bibliothèque: la dernière liste connue est affichée immédiatement,
// puis revalidée en arrière-plan. Les imports/scans restent détectés par le marqueur
// /last-updated et remplacent le cache dès qu'une modification est constatée.
const LIBRARY_CACHE_TTL_MS = 10 * 60 * 1000;

function _libraryCacheKey() {
    return `library-cache-${libraryId}`;
}

function _readLibraryCache() {
    try {
        const cached = JSON.parse(localStorage.getItem(_libraryCacheKey()) || 'null');
        if (!cached || !Array.isArray(cached.series) || !cached.stats) return null;
        cached.stale = Date.now() - (cached.ts || 0) > LIBRARY_CACHE_TTL_MS;
        return cached;
    } catch (e) {
        return null;
    }
}

function _writeLibraryCache(series, stats) {
    try {
        localStorage.setItem(_libraryCacheKey(), JSON.stringify({ series, stats, ts: Date.now() }));
    } catch (e) {
        // Quota dépassé/navigation privée: le cache est facultatif.
    }
}

let _libraryRenderedSignature = null;
let _libraryLoadPromise = null;

function _libraryDataSignature(series, stats) {
    return JSON.stringify([series, stats], (key, value) => key === '_searchHaystack' ? undefined : value);
}

function _applyLibraryData(series, stats) {
    seriesData = series;
    // Pré-calcule une fois par série le texte de recherche normalisé (titre + auteur +
    // genre + tags), pour ne pas re-normaliser ces champs à chaque frappe dans la barre
    // de recherche (coûteux sur une bibliothèque de centaines/milliers de séries)
    // s.year: déjà résolu côté serveur (manual > bedetheque > local, un seul COALESCE
    // SQL - voir get_library_series, routes.py) et toujours une chaîne (CAST AS TEXT) -
    // "i want one single table in the database with the information. i don't want
    // overcomplicated calculation": pas de repli à recalculer ici.
    seriesData.forEach(s => { s._searchHaystack = buildSearchHaystack(s); });
    updateStats(stats);
    populateDynamicFilterOptions();
    updateActiveFilterBadges();
    // Ré-applique la recherche/les filtres/le tri actifs (plutôt que d'afficher la
    // liste brute) pour qu'un rechargement manuel ou après scan ne fasse pas
    // silencieusement disparaître le filtrage en cours (cf. filterSeries)
    filterSeries();
}

async function loadLibraryData() {
    if (_libraryLoadPromise) return _libraryLoadPromise;
    _libraryLoadPromise = _loadLibraryData();
    try {
        return await _libraryLoadPromise;
    } finally {
        _libraryLoadPromise = null;
    }
}

async function _loadLibraryData() {
    const grid = document.getElementById('series-grid');

    // Ne charger les données que si on est sur la page library.html
    if (!grid) {
        return;
    }

    const cached = _readLibraryCache();
    const renderedFromCache = !!cached;
    if (cached) {
        const cachedSignature = _libraryDataSignature(cached.series, cached.stats);
        if (cachedSignature !== _libraryRenderedSignature) {
            _applyLibraryData(cached.series, cached.stats);
            _libraryRenderedSignature = cachedSignature;
        }
        grid.setAttribute('data-cache-refreshing', 'true');
    } else {
        grid.innerHTML = '<div class="loading"><div class="spinner"></div><p>Chargement des données...</p></div>';
    }

    try {
        const [seriesResponse, statsResponse] = await Promise.all([
            fetch(`/api/library/${libraryId}/series`),
            fetch(`/api/library/${libraryId}/stats`)
        ]);

        const series = await seriesResponse.json();
        const stats = await statsResponse.json();

        _writeLibraryCache(series, stats);
        const freshSignature = _libraryDataSignature(series, stats);
        if (freshSignature !== _libraryRenderedSignature) {
            _applyLibraryData(series, stats);
            _libraryRenderedSignature = freshSignature;
        }
        grid.removeAttribute('data-cache-refreshing');
    } catch (error) {
        console.error('Erreur dans loadLibraryData:', error);
        grid.removeAttribute('data-cache-refreshing');
        if (!renderedFromCache) {
            grid.innerHTML = `<div class="no-data"><h3>Erreur de chargement</h3><p>${escapeHtml(String(error.message))}</p></div>`;
        }
    }
}

// "aussi peut faire quelque chose pour que le tableau soit a jour en permanence et
// eviter d'avoir à le charger tout le temps" - un sondage périodique d'un marqueur bon
// marché (GET /api/library/<id>/last-updated, un simple agrégat SQL) plutôt qu'un
// rechargement complet à intervalle fixe: le fetch complet de la liste des séries (avec
// tri/filtre à ré-appliquer) ne se déclenche que si ce marqueur a changé depuis le
// dernier sondage. Capte les changements faits N'IMPORTE OÙ (import manuel, scheduler
// auto-import, script de backfill en arrière-plan...), pas seulement ceux déclenchés
// depuis cet onglet.
let _libraryFreshnessMarker = null;
let _libraryPollTimer = null;

async function pollLibraryFreshness() {
    // "met dans les periodes ou il ne passe rien ca fera du polling pour rien" - onglet
    // en arrière-plan (changé d'onglet, fenêtre minimisée...): personne ne regarde,
    // inutile de sonder. Rattrapé immédiatement par le listener visibilitychange plus
    // bas dès que l'onglet redevient visible, pas seulement au prochain tick régulier.
    if (document.hidden) return;
    try {
        const response = await fetch(`/api/library/${libraryId}/last-updated`);
        const data = await response.json();
        if (data.error) return;
        const marker = `${data.marker}|${data.series_count}|${data.volumes_count}`;
        // null au tout premier sondage (juste après le chargement initial): sert
        // uniquement à mémoriser l'état de référence, jamais à déclencher un rechargement
        // qui viendrait de se terminer une seconde plus tôt.
        if (_libraryFreshnessMarker !== null && marker !== _libraryFreshnessMarker) {
            loadLibraryData();
        }
        _libraryFreshnessMarker = marker;
    } catch (e) {
        // Sondage best-effort - une erreur ponctuelle n'a pas besoin d'interrompre quoi
        // que ce soit, le prochain passage réessaiera de lui-même.
    }
}

function startLibraryFreshnessPolling() {
    if (_libraryPollTimer) return;
    pollLibraryFreshness();
    _libraryPollTimer = setInterval(pollLibraryFreshness, 20000);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) pollLibraryFreshness();
    });
}

function updateStats(stats) {
    // Vérifier que les éléments de stats existent (ils n'existent que sur library.html)
    const seriesCountEl = document.getElementById('series-count');
    const volumesCountEl = document.getElementById('volumes-count');
    const totalSizeEl = document.getElementById('total-size');
    const avgPagesEl = document.getElementById('avg-pages');
    
    if (seriesCountEl) seriesCountEl.textContent = stats.total_series;
    if (volumesCountEl) volumesCountEl.textContent = stats.total_volumes;
    if (totalSizeEl) totalSizeEl.textContent = formatBytes(stats.total_size);
    if (avgPagesEl) avgPagesEl.textContent = stats.avg_pages;
}

// Recherche cette série via la fenêtre modale (search-ed2k-modal, voir
// searchMissingVolume) plutôt que de naviguer vers la page /search séparée - toutes les
// recherches passent maintenant par cette même modale, quel que soit le point d'entrée.
function searchSeriesInSearchTab(seriesId) {
    // Cherche d'abord dans seriesData (liste de bibliothèque), sinon dans les données de
    // la page de détail actuellement affichée (seriesData y est vide)
    let title;
    const fromList = seriesData.find(item => item.id === seriesId);
    if (fromList) {
        title = fromList.title;
    } else if (currentSeriesDetail && currentSeriesDetail.id === seriesId) {
        title = currentSeriesDetail.title;
    }
    if (!title) return;

    // Pas de numéro de tome: recherche "série entière" (voir searchMissingVolume,
    // hasNumber=false) - seriesId suffit à restreindre au bon thread EBDZ côté serveur
    // (search_volume résout ebdz_thread_id depuis series_id) sans avoir à le recalculer ici
    searchMissingVolume(title, null, { seriesId });
}

// "ajouter aussi recherche automatique dans la page de la série et dans la molette des
// volumes" - alternative à searchMissingVolume/searchSeriesInSearchTab ci-dessus: pas de
// résultats à revoir un par un, cherche et télécharge directement chaque tome trouvé avec
// confiance. runSeriesAutoAcquire (search-results-table.js) est le point d'entrée UNIQUE,
// partagé avec Découvrir (discover.js) - "recherche et recherche automatique de discover
// et de la page album doit être la même... pas de code en double pour rien", voir son
// commentaire pour le détail de ce qu'il remplaçait ici (runAutoAcquireNowForSeries/
// ForVolume/ForOneshot, 3 fonctions quasi-identiques).

// Dégradés utilisés comme affiche de substitution quand une série n'a pas de couverture
// (Komga ou Bédéthèque). Choisi de façon stable à partir du titre pour ne pas "sauter"
// d'une couleur à l'autre entre deux rendus de la même série.
const POSTER_PLACEHOLDER_GRADIENTS = [
    'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
    'linear-gradient(135deg, #06b6d4 0%, #0e7490 100%)',
    'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
    'linear-gradient(135deg, #10b981 0%, #059669 100%)',
    'linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)',
    'linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%)'
];

function posterPlaceholderGradient(title) {
    let hash = 0;
    for (let i = 0; i < title.length; i++) {
        hash = (hash * 31 + title.charCodeAt(i)) >>> 0;
    }
    return POSTER_PLACEHOLDER_GRADIENTS[hash % POSTER_PLACEHOLDER_GRADIENTS.length];
}

// Couverture à afficher sur l'affiche: priorité à la vignette extraite localement
// (première page d'un volume, via ComicInfo.xml/scan) puis Bédéthèque, sinon Komga
function pickPosterCoverPath(s) {
    return s.local_cover_path || s.bedetheque_cover_path || s.komga_cover_path || null;
}

// Construit le HTML d'une couverture (image réelle ou placeholder coloré) à une taille
// donnée, réutilisé par les 3 vues (affiches/aperçu/tableau)
function buildCoverHtml(s, imgClass, placeholderClass) {
    const coverPath = pickPosterCoverPath(s);
    if (coverPath) {
        const coverUrl = `/${coverPath}`;
        // Les couvertures de la grille, de l'aperçu et du tableau restent des zones de
        // navigation vers la fiche série (le clic remonte à .poster-card/.overview-row).
        return `<img class="${imgClass}" src="${escapeHtml(coverUrl)}" alt="${escapeHtml(s.title)}" loading="lazy">`;
    }
    return `<div class="${placeholderClass}" style="background: ${posterPlaceholderGradient(s.title)}">📚</div>`;
}

// Affiche une couverture en grand sans quitter la vue Bibliothèque. Le contenu est
// renseigné via textContent/src plutôt que concaténé dans le HTML de la modale, afin
// que les titres provenant de la base restent inoffensifs même s'ils contiennent des
// caractères spéciaux.
function openCoverModal(imageUrl, title) {
    const modal = document.getElementById('cover-image-modal');
    const image = document.getElementById('cover-image-modal-image');
    const caption = document.getElementById('cover-image-modal-caption');
    if (!modal || !image) return;
    image.src = imageUrl;
    image.alt = title || 'Couverture';
    if (caption) caption.textContent = title || '';
    modal.classList.add('active');
}

function closeCoverModal() {
    const modal = document.getElementById('cover-image-modal');
    const image = document.getElementById('cover-image-modal-image');
    if (!modal) return;
    modal.classList.remove('active');
    if (image) image.removeAttribute('src');
}

// Boutons d'action rapide (Chercher/Éditer), réutilisés par les vues aperçu et tableau -
// Supprimer n'est plus une icône séparée ici, c'est une tuile du menu "⚙️ Éditer" (voir
// openSeriesEditModal) comme le reste des actions de gestion d'une série. Le bouton EBDZ
// qui existait avant Éditer a été retiré pour la même raison que le bouton Komga avant
// lui: son résultat s'écrivait dans un conteneur toujours display:none (voir
// buildHiddenStatusAnchorsHtml), jamais affiché nulle part dans la grille (le matching en
// masse reste possible via "EBDZ (tout)"/"Matcher toutes les séries avec Komga" ailleurs).
function buildQuickActionsHtml(s, btnClass) {
    return `
        <button class="${btnClass}" onclick="event.stopPropagation(); searchSeriesInSearchTab(${s.id})"
                title="Rechercher cette série dans l'onglet Recherche">${svgIcon('search')}</button>
        <button class="${btnClass}" onclick="event.stopPropagation(); runSeriesAutoAcquire(${s.id}, {seriesTitle: '${escapeForAttribute(s.title)}', buttonEl: this})"
                title="Rechercher automatiquement et télécharger les albums manquants">${svgIcon('radar')}</button>
        <button class="${btnClass}" onclick="event.stopPropagation(); openSeriesEditModal(${s.id})"
                title="Éditer cette série">${svgIcon('settings')}</button>
    `;
}

// Ancres cachées ciblées par checkEbdzVolumes/checkKomgaMetadata (context 'card'):
// on ne montre plus le détail complet dans la grille (uniquement dans la page de
// détail), mais ces fonctions ont besoin d'un conteneur existant pour s'exécuter
function buildHiddenStatusAnchorsHtml(s) {
    return `
        <div id="ebdz-status-card-${s.id}" style="display: none;"></div>
        <div id="komga-status-card-${s.id}" style="display: none;"></div>
    `;
}

// Détail des albums possédés d'une série, par catégorie (tomes, intégrales, HS,
// épisodes et spéciaux), à partir des compteurs renvoyés par l'API bibliothèque.
function buildVolumeCountLabel(s) {
    const parts = [];
    const add = (count, singular, plural = singular + 's') => {
        if (Number(count) > 0) parts.push(`${count} ${Number(count) > 1 ? plural : singular}`);
    };
    add(s.owned_tomes, 'tome');
    add(s.owned_integrals, 'intégrale');
    add(s.owned_hs, 'hors-série');
    add(s.owned_episodes, 'épisode');
    add(s.owned_specials, 'spécial');
    return parts.length ? parts.join(' · ') : '0 album';
}

// Construit l'affiche (poster) d'une série: couverture, pastilles de statut,
// actions au survol, et titre/nb de tomes en surimpression en bas
function buildPosterCardHtml(s) {
    const imageHtml = buildCoverHtml(s, 'poster-image', 'poster-placeholder');
    const volumeLabel = buildVolumeCountLabel(s);

    return `
    <div class="poster-card" data-series-id="${s.id}" onclick="viewSeries(${s.id})">
        <div class="poster-image-wrapper">
            ${imageHtml}
            <input type="checkbox" class="series-select-checkbox" data-series-id="${s.id}" ${selectedSeriesIds.has(s.id) ? 'checked' : ''} onclick="event.stopPropagation()" onchange="toggleSeriesSelection(${s.id}, this.checked)" style="position:absolute; top:6px; left:6px; width:18px; height:18px; cursor:pointer; z-index:2;" title="Sélectionner">
            <div class="poster-actions">
                <button class="poster-action-btn" onclick="event.stopPropagation(); scanSeries(${s.id})"
                        title="Actualiser: scan rapide de cette série">${svgIcon('refresh-cw')}</button>
                <button class="poster-action-btn" onclick="event.stopPropagation(); searchSeriesInSearchTab(${s.id})"
                        title="Rechercher cette série dans l'onglet Recherche">${svgIcon('search')}</button>
                <button class="poster-action-btn" onclick="event.stopPropagation(); openSeriesEditModal(${s.id})"
                        title="Éditer: renommer, matcher EBDZ, matcher Komga">${svgIcon('settings')}</button>
            </div>
            <div class="poster-overlay">
                <div class="poster-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</div>
                <div class="poster-subtitle">${volumeLabel}</div>
            </div>
        </div>
        ${buildHiddenStatusAnchorsHtml(s)}
    </div>
    `;
}

// Construit une ligne "aperçu" (façon vue Overview de Radarr): couverture moyenne
// + titre/badges/infos à côté + actions à droite
function buildOverviewRowHtml(s) {
    // Statut en texte simple sans couleur (même classification que le badge coloré de la
    // vue affiches et le texte de la vue tableau, voir _seriesBadgeInfo) - demandé
    // explicitement: garder Terminé/En cours/Incomplet/Manquant mais sans le fond coloré.
    // Matching EBDZ/Komga et Dernier scan retirés de cette vue (demandé explicitement).
    const statusInfo = _seriesBadgeInfo(s);
    const imageHtml = buildCoverHtml(s, 'cover-thumb-overview', 'cover-thumb-overview poster-placeholder-mini');
    const volumeLabel = buildVolumeCountLabel(s);
    const summary = s.local_summary || '';

    return `
    <div class="overview-row" data-series-id="${s.id}" onclick="viewSeries(${s.id})">
        <input type="checkbox" class="series-select-checkbox" data-series-id="${s.id}" ${selectedSeriesIds.has(s.id) ? 'checked' : ''} onclick="event.stopPropagation()" onchange="toggleSeriesSelection(${s.id}, this.checked)" style="width:18px; height:18px; cursor:pointer; flex-shrink:0;" title="Sélectionner">
        ${imageHtml}
        <div class="overview-info">
            <div class="overview-title">${escapeHtml(s.title)}</div>
            ${summary ? `<p class="overview-summary">${escapeHtml(summary)}</p>` : ''}
            <div class="overview-tags">
                ${statusInfo ? `<span class="overview-tag">${svgIcon(statusInfo.icon)} ${escapeHtml(statusInfo.label)}</span>` : ''}
                <span class="overview-tag">📖 ${volumeLabel}</span>
            </div>
        </div>
        <div class="overview-actions">${buildQuickActionsHtml(s, 'poster-action-btn poster-action-btn-light')}</div>
        ${buildHiddenStatusAnchorsHtml(s)}
    </div>
    `;
}

// Colonnes optionnelles de la vue tableau, désactivées par défaut (masquées tant que
// l'utilisateur ne les active pas via le bouton "⚙️ Colonnes"), persistées dans
// localStorage comme seriesViewMode - render(s) produit le contenu d'une <td>, à partir
// d'une série de seriesData. filterType: 'select' (par défaut, voir _tableFilterColumns)
// propose la liste des valeurs distinctes réellement présentes plutôt qu'un texte libre -
// 'text' pour les colonnes à forte cardinalité (chemin, résumé, nombre de tomes manquants)
// où une liste déroulante serait juste aussi longue qu'inutilisable qu'un champ texte.
//
// Année/Genre ne sont plus ici: colonnes par défaut désormais (voir buildTableRowHtml/
// buildSeriesTableHtml). Matching/Résumé/Dernier scan étaient par défaut avant et sont
// passées ici en optionnelles pour alléger la vue par défaut (Titre/Tomes/Statut/Année/
// Genre/Actions demandés explicitement) - l'ancienne colonne "Matching" combinée
// (icônes EBDZ+Komga, buildPosterMiniBadgesHtml) est remplacée par les deux colonnes
// dédiées ebdzMatch/komgaMatch déjà existantes plutôt que dupliquée ici: son rendu HTML
// (pas du texte brut) ne peut pas passer par escapeHtml comme les autres colonnes
// optionnelles.
// Colonnes "Matching EBDZ/Komga" ("l'emoticon avec le oui et non. trop coloré trouve
// quelque chose de plus moderne") - remplace les émoji ✅/❌ (glyphes couleur pleine sur
// la plupart des rendus) par ✓/✗, dont le rendu reste monochrome (hérite la couleur du
// texte). Texte brut volontairement (pas de <span>/icône Lucide ici): ce render() passe
// par le même pipeline que toutes les colonnes optionnelles (buildTableRowHtml,
// _seriesTableSortValue...), qui applique escapeHtml() sur la valeur retournée - du HTML
// y ressortirait échappé tel quel, affiché comme du texte littéral au lieu d'être rendu.
function _matchStatusHtml(matched) {
    // "#5 retire oui / non garde juste l'icone" - l'icône seule suffit (colonne dédiée,
    // déjà nommée "EBDZ"/"Komga" dans l'en-tête).
    return matched ? '✓' : '✗';
}

const TABLE_OPTIONAL_COLUMNS = [
    { key: 'summary', label: 'Résumé', icon: '📝', header: 'Résumé', filterType: 'text', render: s => s.local_summary || '—' },
    { key: 'scanDate', label: 'Dernier scan', icon: '🕓', header: 'Dernier scan', filterType: 'text',
      render: s => parseDbUtcDate(s.last_scanned)?.toLocaleDateString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ }) ?? '—' },
    // multi: true - "Humour, Jeunesse" est 2 valeurs (ComicInfo.xml multi-auteurs/genres),
    // pas une seule chaîne combinée (voir _tableFilterColumns/splitMultiValue).
    { key: 'author', label: 'Auteur', icon: '✍️', header: 'Auteur', render: s => s.local_author || '—', multi: true },
    { key: 'oneshot', label: 'One-Shot', icon: '🔸', header: 'One-Shot', render: s => s.is_oneshot ? 'Oui' : 'Non' },
    { key: 'publisher', label: 'Éditeur', icon: '🏢', header: 'Éditeur', render: s => s.bedetheque_editeurs || '—' },
    { key: 'universe', label: 'Univers', icon: '🌐', header: 'Univers', render: s => s.universe_name || '—' },
    { key: 'missing', label: 'Tomes manquants', icon: '❗', header: 'Manquants', filterType: 'text',
      render: s => (s.missing_volumes && s.missing_volumes.length) ? String(s.missing_volumes.length) : '0' },
    { key: 'ebdzMatch', label: 'Matching EBDZ', icon: '<img class="table-header-source-logo" src="/static/img/ebdz-logo.png" alt="EBDZ">', header: '<img class="table-header-source-logo" src="/static/img/ebdz-logo.png" alt="EBDZ"> EBDZ', render: s => _matchStatusHtml(s.ebdz_match_status === 'matched') },
    { key: 'komgaMatch', label: 'Matching Komga', icon: '<img class="table-header-source-logo" src="/static/img/komga-logo.svg" alt="Komga">', header: '<img class="table-header-source-logo" src="/static/img/komga-logo.svg" alt="Komga"> Komga', render: s => _matchStatusHtml(s.komga_match_status === 'matched') },
    { key: 'path', label: 'Chemin', icon: '📁', header: 'Chemin', filterType: 'text', render: s => s.path || '—' }
];

const TABLE_FIXED_COLUMNS = [
    { key: 'title', label: 'Titre', icon: '🔤' },
    { key: 'volumes', label: 'Tomes', icon: '📚' },
    { key: 'status', label: 'Statut', icon: '📌' },
    { key: 'year', label: 'Année', icon: '📅' },
    { key: 'genre', label: 'Genre', icon: '🏷️' },
];

// "make sure that if komga or ebdz is not configured they dont show up in the table" -
// une colonne "Matching EBDZ"/"Matching Komga" affichant "✗" sur CHAQUE ligne (jamais
// actionnable, l'intégration n'étant même pas configurée) n'a rien d'un vrai signal.
// Filtrées ici, au seul endroit qui lit TABLE_OPTIONAL_COLUMNS, plutôt que dans chaque
// appelant séparément - enabledIntegrations vient de nav.js (refreshEnabledIntegrations),
// seul script chargé sur toutes les pages ayant besoin de cette info.
function _availableTableOptionalColumns() {
    return TABLE_OPTIONAL_COLUMNS.filter(col => {
        if (col.key === 'ebdzMatch') return enabledIntegrations.ebdz;
        if (col.key === 'komgaMatch') return enabledIntegrations.komga;
        return true;
    });
}

let visibleTableColumns = new Set(JSON.parse(localStorage.getItem('tableVisibleColumns') || '[]'));
let hiddenTableColumns = new Set(JSON.parse(localStorage.getItem('tableHiddenColumns') || '[]'));

function _isTableColumnVisible(key) {
    // Le titre et les actions structurent toujours la ligne; seules les autres
    // colonnes de données sont masquables depuis le menu.
    if (key === 'title') return true;
    return TABLE_FIXED_COLUMNS.some(col => col.key === key)
        ? !hiddenTableColumns.has(key)
        : visibleTableColumns.has(key);
}

function toggleTableColumn(key) {
    if (key === 'title') return;
    if (TABLE_FIXED_COLUMNS.some(col => col.key === key)) {
        if (hiddenTableColumns.has(key)) hiddenTableColumns.delete(key);
        else hiddenTableColumns.add(key);
        localStorage.setItem('tableHiddenColumns', JSON.stringify([...hiddenTableColumns]));
    } else {
        if (visibleTableColumns.has(key)) visibleTableColumns.delete(key);
        else visibleTableColumns.add(key);
        localStorage.setItem('tableVisibleColumns', JSON.stringify([...visibleTableColumns]));
    }
    renderTableColumnsMenu();
    filterSeries();
}

// Reconstruit le contenu du menu "⚙️ Colonnes" - pas de classe autoclose puisqu'on veut
// pouvoir cocher/décocher plusieurs colonnes sans que le menu se referme à chaque clic
// (même logique que les menus de filtre Genre/Auteur/Année)
function renderTableColumnsMenu() {
    const menu = document.getElementById('table-columns-menu');
    if (!menu) return;
    const columns = [...TABLE_FIXED_COLUMNS, ..._availableTableOptionalColumns()]
        .filter(col => col.key !== 'title');
    menu.innerHTML = columns.map(col => `
        <button class="toolbar-dropdown-item${_isTableColumnVisible(col.key) ? ' active' : ''}"
                onclick="toggleTableColumn('${col.key}')">${col.icon} ${col.label}</button>
    `).join('');
}

// Construit une ligne de la vue "tableau" (façon vue Table de Radarr). Colonnes par
// défaut: Titre/Tomes/Statut/Année/Genre/Actions - Statut en texte simple (pas d'icône,
// pas de couleur par statut, voir _seriesStatusLabel) plutôt que le badge coloré de la
// vue grille (calculateSeriesBadge), demandé explicitement pour rester sobre dans un
// tableau dense.
function buildTableRowHtml(s) {
    const statusLabel = _seriesStatusLabel(s);
    const volumeLabel = buildVolumeCountLabel(s);

    // Colonnes optionnelles bornées en largeur (voir .series-table-optional-cell): sans
    // ça, un genre/auteur un peu long forçait le tableau entier à s'élargir bien au-delà
    // du conteneur, avec un scroll horizontal peu lisible dès 2-3 colonnes activées à la
    // fois - la troncature ellipsis (+ title="" pour voir la valeur complète au survol)
    // garde le tableau contenu quel que soit le nombre de colonnes cochées
    // data-label: ignoré en desktop, utilisé uniquement par le CSS mobile (::before,
    // voir @media max-width:768px dans style-library-search.css) pour afficher "Label :
    // valeur" sur les colonnes optionnelles - leur valeur seule (ex: "Drugstore") ne dit
    // pas de quelle colonne elle vient une fois l'en-tête masqué sur mobile.
    // data-tooltip (pas title): "quand c'est trop [long] il faudrait afficher un
    // tooltip instantanément" - un title="" natif se déclenche avec le délai de survol
    // de l'OS/navigateur (~1s), contrairement au système d'infobulle instantané déjà en
    // place partout ailleurs dans l'app (voir showJsTooltip, nav.js).
    const optionalCellsHtml = _availableTableOptionalColumns()
        .filter(col => _isTableColumnVisible(col.key))
        .map(col => {
            const value = String(col.render(s));
            return `<td class="series-table-optional-cell" data-label="${escapeHtml(col.header)}" data-tooltip="${escapeHtml(value)}">${escapeHtml(value)}</td>`;
        }).join('');

    return `
    <tr class="series-table-row" data-series-id="${s.id}" onclick="viewSeries(${s.id})">
        <td class="volume-table-select-cell" onclick="event.stopPropagation()"><input type="checkbox" class="series-select-checkbox series-table-row-checkbox" data-series-id="${s.id}" ${selectedSeriesIds.has(s.id) ? 'checked' : ''} onchange="toggleSeriesSelection(${s.id}, this.checked)"></td>
        ${_isTableColumnVisible('title') ? `<td class="series-table-title" data-tooltip="${escapeHtml(s.title)}">${escapeHtml(s.title)}</td>` : ''}
        ${_isTableColumnVisible('volumes') ? `<td class="series-table-pill">${volumeLabel}</td>` : ''}
        ${_isTableColumnVisible('status') ? `<td class="series-table-pill">${escapeHtml(statusLabel)}</td>` : ''}
        ${_isTableColumnVisible('year') ? `<td class="series-table-pill">${escapeHtml(s.year || '—')}</td>` : ''}
        ${_isTableColumnVisible('genre') ? `<td class="series-table-pill">${escapeHtml(s.local_genre || '—')}</td>` : ''}
        ${optionalCellsHtml}
        <td class="series-table-actions" onclick="event.stopPropagation()"><div class="series-table-actions-inner">${buildQuickActionsHtml(s, 'poster-action-btn poster-action-btn-light')}</div></td>
        ${buildHiddenStatusAnchorsHtml(s)}
    </tr>
    `;
}

// Libellé simple ("Manquant"/"Terminé"/"Incomplet"/"En cours") pour filtrer/lister les
// valeurs distinctes de la colonne Statut (voir _tableFilterColumns), à partir de la
// même classification que calculateSeriesBadge/calculateSeriesBadgePlain.
function _seriesStatusLabel(s) {
    const info = _seriesBadgeInfo(s);
    return info ? info.label : '—';
}

// Une entrée par colonne filtrable de la vue tableau, dans le même ordre que les <td>
// produits par buildTableRowHtml (colonne Actions exclue, ce n'est pas une donnée) - sert
// à la fois à générer la ligne de filtres et à savoir sur quel <td> (par index) appliquer
// chaque filtre dans filterSeriesTableRows, sans dépendre du texte de l'en-tête.
//
// type 'select' (par défaut): liste déroulante des valeurs réellement présentes dans
// `series` pour cette colonne (getValue), plutôt qu'un champ texte libre - demandé
// explicitement, un champ libre oblige à deviner l'orthographe exacte d'un genre/auteur.
// type 'text': recherche par sous-chaîne, réservé aux colonnes à forte cardinalité
// (résumé, chemin...) où une liste déroulante serait aussi longue qu'inutilisable.
// "keep the scrolldown for all the entry in the table. for titre, genre, auteur, editeur
// I want the filter not the scrolldown" - TOUTES les colonnes gardent un filtre (aucune
// n'en est privée) - seul le TYPE de filtre change pour ces 4 colonnes précises: texte
// libre ("filter") plutôt que liste déroulante ("scrolldown") - à forte cardinalité
// (des dizaines/centaines de titres, auteurs ou éditeurs distincts), une liste déroulante
// y est bien moins pratique qu'un champ où taper directement. Comparé par label (voir
// header ci-dessous) plutôt que par clé de colonne: les 5 colonnes par défaut n'ont pas
// de clé propre dans ce fichier, seulement un libellé.
const _TEXT_FILTER_TABLE_COLUMN_LABELS = new Set(['Titre', 'Genre', 'Auteur', 'Éditeur']);

function _tableFilterColumns(series) {
    const fixed = TABLE_FIXED_COLUMNS.filter(col => _isTableColumnVisible(col.key));
    const optional = _availableTableOptionalColumns().filter(col => _isTableColumnVisible(col.key));
    const fixedFilters = {
        title: { label: 'Titre', type: 'text' },
        volumes: { label: 'Tomes', type: 'text' },
        status: { label: 'Statut', type: 'select', getValue: _seriesStatusLabel },
        year: { label: 'Année', type: 'select', getValue: s => s.year || '—' },
        genre: { label: 'Genre', type: 'select', getValue: s => s.local_genre || '—', multi: true },
    };
    const cols = [
        ...fixed.map(col => fixedFilters[col.key]),
        // multi: true - "certains sont avec des virgules et donc sont definis comme
        // humour, jeunesse. ca devrait etre 2 genres differents" - local_genre (comme
        // local_author) peut combiner plusieurs valeurs ComicInfo.xml dans une seule
        // chaîne ("Humour, Jeunesse"), même format multi-valeur que la section de filtre
        // Genre du menu principal (voir splitMultiValue/DYNAMIC_FILTER_SECTIONS). Passé en
        // filtre texte ci-dessous (voir _TEXT_FILTER_TABLE_COLUMN_LABELS): une recherche en
        // sous-chaîne retrouve quand même "Jeunesse" dans "Humour, Jeunesse" sans avoir
        // besoin de la liste déroulante ni de ce découpage.
        ...optional.map(col => ({
            label: col.header,
            type: col.filterType || 'select',
            getValue: col.render,
            multi: !!col.multi,
        })),
    ];
    for (const col of cols) {
        if (_TEXT_FILTER_TABLE_COLUMN_LABELS.has(col.label)) {
            col.type = 'text';
        } else if (col.type === 'select') {
            const rawValues = series.map(col.getValue);
            col.values = [...new Set(col.multi ? rawValues.flatMap(splitMultiValue) : rawValues)]
                .sort((a, b) => a.localeCompare(b, 'fr'));
        }
    }
    return cols;
}

// Filtre la vue tableau colonne par colonne (en plus des filtres globaux de la barre
// d'outils, qui reconstruisent seriesData/le tableau entier) - un simple masquage de
// lignes déjà rendues: correspondance exacte pour un filtre "select" (la valeur choisie
// est déjà exactement le texte affiché dans la cellule) - sauf colonne multi-valeur
// (Genre/Auteur, voir _tableFilterColumns/splitMultiValue): la cellule affiche encore le
// texte combiné ("Humour, Jeunesse"), la valeur choisie ("Humour") n'y correspond alors
// jamais mot pour mot, il faut la chercher parmi les valeurs découpées. Sous-chaîne
// insensible à la casse pour un filtre "text". Pas besoin de retoucher filterSeries()/
// activeFilters pour ça, la vue tableau est la seule à avoir un filtre par colonne.
function filterSeriesTableRows() {
    const controls = document.querySelectorAll('.series-table-filter-input, .series-table-filter-select');
    const active = [...controls]
        .map(el => ({
            colIndex: parseInt(el.dataset.colIndex),
            isSelect: el.tagName === 'SELECT',
            isMulti: el.dataset.multi === '1',
            value: el.value.trim(),
        }))
        .filter(f => f.value);

    document.querySelectorAll('.series-table-row').forEach(row => {
        const matches = active.every(f => {
            const cell = row.children[f.colIndex];
            if (!cell) return false;
            const text = cell.textContent.trim();
            // "si j'ai un accent de type é ca trouve pas. vire les accents" - comparaison
            // insensible aux accents (voir normalizeForSearch), pas juste à la casse.
            if (!f.isSelect) return normalizeForSearch(text).includes(normalizeForSearch(f.value));
            return f.isMulti ? splitMultiValue(text).includes(f.value) : text === f.value;
        });
        row.style.display = matches ? '' : 'none';
    });
}

// Tri par en-tête cliquable (remplace le menu "Ordonner" de la toolbar, retiré) - même
// principe que _compareVolumesForSort pour la vue tableau des tomes: valeur comparable
// par colonne, null/'' toujours en fin quel que soit le sens du tri. Couvre aussi bien
// les 5 colonnes par défaut que les colonnes optionnelles (TABLE_OPTIONAL_COLUMNS,
// via leur render(s) déjà utilisé pour le filtre - voir _tableFilterColumns) : toutes
// les colonnes sont cliquables pour trier, pas seulement celles affichées par défaut.
function _seriesTableSortValue(s, column) {
    switch (column) {
        case 'title': return (s.title || '').toLowerCase();
        // One-shot n'a pas vraiment "un nombre de tomes" comparable à une série numérotée
        // - toujours avant un tome "1" (pas la même chose, demandé explicitement): -1
        // trie avant tout total_volumes réel (toujours >= 1) en ordre croissant.
        case 'volumes': return s.is_oneshot ? -1 : (s.total_volumes || 0);
        case 'status': return _seriesStatusLabel(s);
        case 'year': return s.year || null;
        case 'genre': return (s.local_genre || '').toLowerCase() || null;
        default: {
            const col = _availableTableOptionalColumns().find(c => c.key === column);
            return col ? col.render(s) : null;
        }
    }
}

function _compareSeriesForSort(a, b, column, direction) {
    const va = _seriesTableSortValue(a, column);
    const vb = _seriesTableSortValue(b, column);
    const aEmpty = va === null || va === undefined || va === '' || va === '—';
    const bEmpty = vb === null || vb === undefined || vb === '' || vb === '—';
    if (aEmpty && bEmpty) return 0;
    if (aEmpty) return 1;
    if (bEmpty) return -1;

    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    return direction === 'asc' ? cmp : -cmp;
}

// Clic sur un en-tête: même colonne recliquée -> inverse le sens, nouvelle colonne ->
// ascendant. Re-filtre/re-trie/re-affiche depuis seriesData plutôt qu'un simple
// ré-agencement du DOM, pour rester cohérent avec une recherche/un filtre déjà actif.
function setSeriesTableSort(column) {
    if (seriesTableSort.column === column) {
        seriesTableSort.direction = seriesTableSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        seriesTableSort.column = column;
        seriesTableSort.direction = 'asc';
    }
    filterSeries();
}

// filterCol/colIndex optionnels: fusionne le contrôle de filtre (voir _tableFilterColumns)
// directement dans l'en-tête triable, sur la même ligne que le libellé plutôt qu'une
// seconde <tr> dédiée ou empilé dessous ("les filtres tu peux les mettre dans le header
// pour eviter d'avoir une ligne en plus", puis "change toutes les filtre des tableaux
// avec le filter sur la meme ligne style hoover") - replié sur une icône loupe, déplié
// au survol/focus (voir .th-filterable-* dans style-library-search.css). Le filtre a son
// propre onclick stopPropagation pour ne pas déclencher le tri au clic/à la sélection.
function _seriesTableHeaderHtml(column, label, filterCol, colIndex) {
    const active = seriesTableSort.column === column;
    const arrow = active ? (seriesTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    const filterHtml = filterCol ? `
        <span class="th-filterable-filter" onclick="event.stopPropagation()">
            <span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>
            ${filterCol.type === 'select'
                ? `<select id="series-table-filter-${column}" aria-label="Filtrer par ${escapeHtml(label)}" class="series-table-filter-select th-filterable-control" data-col-index="${colIndex}" data-multi="${filterCol.multi ? '1' : ''}" onchange="_syncFilterControlActive(this); filterSeriesTableRows()">
                       <option value="">Tous</option>
                       ${filterCol.values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('')}
                   </select>`
                : `<input type="text" id="series-table-filter-${column}" aria-label="Filtrer par ${escapeHtml(label)}" class="series-table-filter-input th-filterable-control" data-col-index="${colIndex}"
                           placeholder="Filtrer..." oninput="_syncFilterControlActive(this); filterSeriesTableRows()">`}
        </span>
    ` : '';
    return `
        <th class="volume-table-sortable${active ? ' volume-table-sort-active' : ''}" onclick="setSeriesTableSort('${column}')">
            <div class="th-filterable-row">
                <span class="th-filterable-label">${label}${arrow}</span>
                ${filterHtml}
            </div>
        </th>
    `;
}

function buildSeriesTableHtml(series) {
    const filterCols = _tableFilterColumns(series);
    const fixedColumns = TABLE_FIXED_COLUMNS
        .filter(col => _isTableColumnVisible(col.key))
        .map(col => ({ column: col.key, label: col.label }));
    const optionalColumns = _availableTableOptionalColumns()
        .filter(col => _isTableColumnVisible(col.key))
        .map(col => ({ column: col.key, label: col.header }));

    // colIndex décalé de +1 (i + 1): row.children[0] est désormais la case à cocher
    // (voir buildTableRowHtml/toggleSeriesSelection), pas la première colonne de données -
    // sans ce décalage, filterSeriesTableRows() lisait le contenu (vide) de la case à
    // cocher au lieu du texte de la colonne "Titre" et ne matchait donc plus jamais rien
    // ("si je tappe pouvoir. il y a rien qui s'affiche").
    const headersHtml = [...fixedColumns, ...optionalColumns]
        .map((col, i) => _seriesTableHeaderHtml(col.column, col.label, filterCols[i], i + 1))
        .join('');

    const sortedSeries = seriesTableSort.column
        ? [...series].sort((a, b) => _compareSeriesForSort(a, b, seriesTableSort.column, seriesTableSort.direction))
        : series;

    return `
        <table class="series-table">
            <thead>
                <tr>
                    <th class="volume-table-select-cell"><input type="checkbox" id="series-table-select-all" onchange="toggleAllSeriesTableSelection(this.checked)" title="Tout sélectionner (séries visibles)"></th>
                    ${headersHtml}
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${sortedSeries.map(buildTableRowHtml).join('')}
            </tbody>
        </table>
    `;
}

// Construit le HTML d'une série pour le mode de vue actuellement sélectionné
function buildSeriesItemHtml(s) {
    if (seriesViewMode === 'table') return buildTableRowHtml(s);
    if (seriesViewMode === 'overview') return buildOverviewRowHtml(s);
    return buildPosterCardHtml(s);
}

// Reconstruit entièrement l'élément d'une série déjà affichée (carte, ligne d'aperçu
// ou ligne de tableau selon le mode de vue actif), à partir du cache seriesData.
// Appelé après un enrichissement/matching/unmatching individuel ou en masse, pour que
// la couverture et les pastilles reflètent tout de suite le nouveau statut.
function refreshSeriesItem(seriesId) {
    const el = document.querySelector(`[data-series-id="${seriesId}"]`);
    if (!el) return;
    const s = seriesData.find(item => item.id === seriesId);
    if (!s) return;
    el.outerHTML = buildSeriesItemHtml(s);
}

function setSeriesViewMode(mode) {
    seriesViewMode = mode;
    localStorage.setItem('seriesViewMode', mode);
    updateViewSwitcherButtons();
    filterSeries();
}

function updateViewSwitcherButtons() {
    document.querySelectorAll('.view-switch-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.view === seriesViewMode);
    });
    const columnsSection = document.getElementById('table-columns-section');
    if (columnsSection) columnsSection.style.display = seriesViewMode === 'table' ? '' : 'none';
}

function displaySeries(series) {
    const grid = document.getElementById('series-grid');

    if (series.length === 0) {
        grid.className = 'series-grid';
        grid.innerHTML = '<div class="no-data"><span class="no-data-icon">📚</span><h3>Aucune série trouvée</h3><p>Scannez votre bibliothèque pour commencer</p></div>';
        return;
    }

    if (seriesViewMode === 'table') {
        grid.className = 'series-list series-table-wrapper';
        grid.innerHTML = buildSeriesTableHtml(series);
    } else if (seriesViewMode === 'overview') {
        grid.className = 'series-list overview-list';
        grid.innerHTML = series.map(buildOverviewRowHtml).join('');
    } else {
        grid.className = 'series-grid poster-grid';
        grid.innerHTML = series.map(buildPosterCardHtml).join('');
    }

    initClearableSearchInputs(grid);
    updateSeriesBulkActionsBar();
}

// ===== SÉLECTION MULTIPLE / ACTIONS GROUPÉES - LISTE DES SÉRIES =====
// "la page /library/2 il n'y a pas de bouton pour selectionner les series" - même case à
// cocher/Set/barre d'actions que la sélection de tomes sur la fiche série
// (selectedVolumeIds), partagée par les 3 vues (affiches/aperçu/tableau) au lieu d'un
// mécanisme par vue.
function toggleSeriesSelection(seriesId, checked) {
    if (checked) selectedSeriesIds.add(seriesId); else selectedSeriesIds.delete(seriesId);
    updateSeriesBulkActionsBar();
}

// "Tout sélectionner" de la vue tableau - seulement les lignes VISIBLES (respecte un
// filtre de colonne déjà actif), même raisonnement que toggleAllVolumeTableSelection.
function toggleAllSeriesTableSelection(checked) {
    document.querySelectorAll('.series-table-row').forEach(row => {
        if (row.style.display === 'none') return;
        const checkbox = row.querySelector('.series-table-row-checkbox');
        if (!checkbox) return;
        checkbox.checked = checked;
        const id = parseInt(checkbox.dataset.seriesId, 10);
        if (checked) selectedSeriesIds.add(id); else selectedSeriesIds.delete(id);
    });
    updateSeriesBulkActionsBar();
}

function clearSeriesSelection() {
    selectedSeriesIds.clear();
    document.querySelectorAll('.series-select-checkbox').forEach(cb => { cb.checked = false; });
    const selectAll = document.getElementById('series-table-select-all');
    if (selectAll) selectAll.checked = false;
    updateSeriesBulkActionsBar();
}

function updateSeriesBulkActionsBar() {
    const bar = document.getElementById('series-bulk-actions-bar');
    if (!bar) return;
    const count = selectedSeriesIds.size;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="volume-bulk-actions-count">${count} ${pluralize(count, 'série')} ${pluralize(count, 'sélectionnée')}</span>
        <button class="btn-neutral-sm" onclick="bulkScanSelectedSeries()">${svgIcon('refresh-cw')} Actualiser</button>
        <button class="btn-neutral-sm" onclick="bulkUpdateMetadataSelectedSeries()">${svgIcon('tag')} MAJ métadonnées</button>
        <button class="btn-neutral-sm" onclick="bulkAutoAcquireSelectedSeries()">${svgIcon('radar')} Recherche automatique</button>
        <button class="btn-danger-sm" onclick="bulkDeleteSelectedSeries()">${svgIcon('trash-2')} Supprimer</button>
        <button class="btn-neutral-sm" onclick="clearSeriesSelection()">Annuler la sélection</button>
    `;
}

// Résout les ids sélectionnés en objets série complets depuis seriesData (déjà en
// mémoire) - même raisonnement que _selectedVolumesData.
function _selectedSeriesData() {
    return [...selectedSeriesIds].map(id => seriesData.find(s => s.id === id)).filter(Boolean);
}

// Même pattern "sélection groupée séquentielle + un seul toast" que _runBulkVolumeAction
// (fiche série) - scanSeries/deleteSeries (usage individuel, boutons ⚙️/carte) ont chacun
// leurs propres alert()/redirection, impraticables en boucle ; on reprend directement leurs
// appels réseau ici.
async function _runBulkSeriesAction(seriesList, toastId, verbFn, actionFn) {
    const failures = [];
    for (let i = 0; i < seriesList.length; i++) {
        const s = seriesList[i];
        showToast(toastId, verbFn(i + 1, seriesList.length));
        try {
            const response = await actionFn(s);
            const data = await response.json();
            if (!data.success) failures.push(`${s.title}: ${data.error || 'erreur inconnue'}`);
        } catch (error) {
            failures.push(`${s.title}: ${error.message}`);
        }
    }
    showToast(toastId, failures.length ? `Terminé avec ${failures.length} ${pluralize(failures.length, 'erreur')}` : 'Terminé',
        { icon: failures.length ? 'triangle-alert' : 'check', autoHideMs: 4000 });
    if (failures.length) alert('❌ Échecs:\n' + failures.join('\n'));
    clearSeriesSelection();
    if (typeof loadLibraryData === 'function') loadLibraryData();
}

async function bulkScanSelectedSeries() {
    const series = _selectedSeriesData();
    if (series.length === 0) return;

    await _runBulkSeriesAction(
        series, 'bulk-scan-series',
        (i, n) => `Actualisation... (${i}/${n})`,
        s => fetch(`/api/scan/series/${s.id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' } })
    );
}

async function bulkUpdateMetadataSelectedSeries() {
    const series = _selectedSeriesData();
    if (series.length === 0) return;
    if (!confirm(`Mettre à jour les métadonnées Bédéthèque (série + tomes) de ${series.length} ${pluralize(series.length, 'série')} ?`)) return;

    await _runBulkSeriesAction(
        series, 'bulk-update-metadata-series',
        (i, n) => `Mise à jour des métadonnées... (${i}/${n})`,
        s => fetch(`/api/bedetheque/update-metadata/series/${s.id}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope: 'all' })
        })
    );
}

async function bulkAutoAcquireSelectedSeries() {
    const series = _selectedSeriesData();
    if (series.length === 0) return;
    showToast('bulk-auto-acquire-series', `Recherche automatique lancée pour ${series.length} ${pluralize(series.length, 'série')}…`, { icon: 'radar', autoHideMs: 5000 });
    const failures = [];
    let started = 0;
    for (const s of series) {
        try {
            const response = await fetch('/api/bedetheque/auto-acquire/run', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ series_id: s.id })
            });
            const data = await response.json();
            if (!data.success) failures.push(`${s.title}: ${data.error || 'erreur inconnue'}`);
            else if (data.started) started++;
        } catch (error) {
            failures.push(`${s.title}: ${error.message}`);
        }
    }
    showToast('bulk-auto-acquire-series', failures.length
        ? `${started} recherche(s) lancée(s), ${failures.length} erreur(s).`
        : `${started} recherche(s) automatique(s) lancée(s) pour les séries sélectionnées.`,
        { icon: failures.length ? 'triangle-alert' : 'check', autoHideMs: 6000 });
    if (failures.length) alert('❌ Échecs:\n' + failures.join('\n'));
    clearSeriesSelection();
}

async function bulkDeleteSelectedSeries() {
    const series = _selectedSeriesData();
    if (series.length === 0) return;
    if (!confirm(`⚠️ Action irréversible : supprime définitivement ${series.length} ${pluralize(series.length, 'série')} ET tous leurs fichiers du disque.\n\n${series.map(s => '- ' + s.title).join('\n')}\n\nConfirmer la suppression ?`)) {
        return;
    }

    await _runBulkSeriesAction(
        series, 'bulk-delete-series',
        (i, n) => `Suppression... (${i}/${n})`,
        s => fetch(`/api/series/${s.id}`, { method: 'DELETE' })
    );
}

// Détermine le statut d'une série (icône/libellé/raison), sans se soucier du rendu -
// calculateSeriesBadge (coloré, vue grille) et _seriesStatusLabel (texte simple sans
// icône ni couleur, vue tableau) partagent cette même classification.
// "Terminé" reprend le vocabulaire de Bédéthèque (bedetheque_status y est déjà
// "Terminée") plutôt que "Finie", qui n'existe nulle part ailleurs dans l'appli.
function _seriesBadgeInfo(series) {
    // Déclaration manuelle ("je voudrais ameliorer si un album est complet ou non...
    // ajoute une option dans la molette pour déclarer cet album complet") - prime sur
    // TOUT calcul automatique ci-dessous, y compris le one-shot: c'est un choix explicite
    // de l'utilisateur, jamais reconsidéré (voir toggle_series_complete_override, routes.py).
    if (series.manual_complete_override) {
        return { key: 'complete', icon: 'check', label: 'Terminé', reason: 'Déclarée complète manuellement' };
    }

    // "ajoute un statut vide ou j'ai la serie en bibliotheque mais pas de volume
    // telecharger" - une série ajoutée (ex: depuis Découvrir, voir add_series_from_bedetheque)
    // mais dont aucun fichier n'a encore été importé n'a par définition ni tome possédé ni
    // trou à signaler - avant le check one-shot: un one-shot pas encore téléchargé est
    // "vide" lui aussi, pas "Terminé" (qui laisserait croire qu'il est déjà possédé).
    if (!series.total_volumes) {
        return { key: 'empty', icon: '📭', label: 'Vide', reason: 'Série ajoutée à la bibliothèque, aucun volume téléchargé pour l\'instant' };
    }

    // Un one-shot n'a par nature pas d'autre tome à venir - toujours "Terminé", sans
    // passer par le texte de bedetheque_status (souvent juste "One shot", qui ne dit rien
    // de l'état "fini"/"en cours" et ferait sinon retomber ce cas sur "Incomplet").
    if (series.is_oneshot) {
        return { key: 'complete', icon: 'check', label: 'Terminé', reason: 'One-shot' };
    }

    const hasBedethequeInfo = series.bedetheque_total_volumes;
    const hasMissingVolumes = series.missing_volumes && series.missing_volumes.length > 0;
    // Le statut réel renvoyé par Bédéthèque pour une série finie est "Série finie" (vérifié
    // en base: 106 séries avec exactement ce texte) - "termin*" ne matchait jamais rien,
    // donc "Terminé" ne pouvait tout simplement jamais s'afficher, ces séries retombant
    // à tort sur "Incomplet"/"En cours". Le mot-clé réel est "fini".
    const isBedethequeComplete = series.bedetheque_status && (
        series.bedetheque_status.toLowerCase().includes('terminé') ||
        series.bedetheque_status.toLowerCase().includes('termin') ||
        series.bedetheque_status.toLowerCase().includes('fini')
    );
    const isBedethequeOngoing = series.bedetheque_status &&
        series.bedetheque_status.toLowerCase().includes('en cours');
    // "now we can validate if a serie is complete or no... check we have the same number
    // of volume than the parus => if egal it is complete / if not check if we have INT
    // => if yes complete / if not check we have all the INT1, INT2... => if yes complete
    // / if not then it is not complete" - series.bedetheque_complete (calculé côté
    // serveur par update_series_stats, voir scanner.py) applique exactement ces 3 étapes
    // maintenant que INT/HS/spéciaux sont fiablement classifiés (voir
    // _parse_int_hs_prefix, scraper.py) - remplace l'ancien signal "!hasMissingVolumes"
    // (best-effort, dépendait d'un parsing de plage dans le TITRE de l'intégrale, voir
    // _parse_integral_tome_range) comme référence principale. null (série pas encore
    // matchée à Bédéthèque, pas de parus à comparer) retombe sur l'ancien signal.
    const isFullyOwned = series.bedetheque_complete !== null && series.bedetheque_complete !== undefined
        ? series.bedetheque_complete
        : (hasBedethequeInfo && !hasMissingVolumes);
    const volumesDontMatch = hasBedethequeInfo && series.total_volumes !== series.bedetheque_total_volumes;

    // IMPORTANT: Checker "Manquant" AVANT "Terminé" pour donner la priorité aux volumes manquants
    // 2. "Manquant" : série terminée sur Bédéthèque ET volumes manquants
    if (isBedethequeComplete && hasMissingVolumes) {
        return {
            key: 'missing', icon: '📚', label: 'Manquant',
            reason: `Terminée sur Bédéthèque (${series.bedetheque_status}) mais il manque les volumes ${series.missing_volumes.join(', ')} dans votre collection`
        };
    }

    // 1. "Terminé" : rien ne manque dans la séquence possédée ET série terminée sur Bédéthèque
    if (isFullyOwned && !hasMissingVolumes) {
        return {
            key: 'complete', icon: 'check', label: 'Terminé',
            reason: `Série terminée (${series.bedetheque_status}) - ${series.bedetheque_complete_reason || `${series.total_volumes} ${pluralize(series.total_volumes, 'volume')} ${pluralize(series.total_volumes, 'possédé')} sur ${series.bedetheque_total_volumes}`}`
        };
    }

    // 3. "Incomplet" : volumes manquants ET série pas terminée sur Bédéthèque
    if (hasMissingVolumes && !isBedethequeComplete) {
        const statusPart = series.bedetheque_status ? ` (statut Bédéthèque: ${series.bedetheque_status})` : ' (pas de statut Bédéthèque)';
        return {
            key: 'incomplete', icon: 'triangle-alert', label: 'Incomplet',
            reason: `Il manque les volumes ${series.missing_volumes.join(', ')} dans la séquence possédée${statusPart}`
        };
    }

    // 4. "En cours" : (volumes ne correspondent pas) OU (série en cours sur Bédéthèque)
    if (volumesDontMatch || isBedethequeOngoing) {
        const reasonParts = [];
        if (volumesDontMatch) {
            reasonParts.push(`${series.total_volumes} ${pluralize(series.total_volumes, 'volume')} ${pluralize(series.total_volumes, 'possédé')} contre ${series.bedetheque_total_volumes} ${pluralize(series.bedetheque_total_volumes, 'annoncé')} sur Bédéthèque`);
        }
        if (isBedethequeOngoing) {
            reasonParts.push(`série en cours sur Bédéthèque (${series.bedetheque_status})`);
        }
        return { key: 'ongoing', icon: 'refresh-cw', label: 'En cours', reason: reasonParts.join(' — ') };
    }

    return null;
}

// Normalise un texte pour une recherche insensible aux accents/casse (ex: "ecole"
// retrouve "École"), même logique que normalize_search_text côté serveur (EBDZ)
function normalizeForSearch(text) {
    return String(text)
        .replace(/œ/g, 'oe').replace(/Œ/g, 'OE')
        .replace(/æ/g, 'ae').replace(/Æ/g, 'AE')
        .normalize('NFKD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLowerCase();
}

// Construit le texte de recherche normalisé d'une série (titre + auteur + genre + tags),
// mis en cache dans s._searchHaystack au chargement pour que la recherche en direct dans
// la barre de recherche n'ait plus qu'une comparaison de sous-chaîne à faire par série,
// au lieu de re-normaliser plusieurs champs à chaque frappe pour chaque série
function buildSearchHaystack(s) {
    const parts = [s.title, s.local_author, s.local_genre, ...(s.tags || [])].filter(Boolean);
    return normalizeForSearch(parts.join(' '));
}

// Catégorie d'une clé de filtre, utilisée pour grouper la logique OU/ET: toutes les
// clés d'une même catégorie (ex: deux genres) se combinent en OU, les catégories
// différentes (ex: genre + année) se combinent en ET
function getFilterCategory(key) {
    if (key === 'missing' || key === 'oneshot' || key === 'ebdz-unmatched' || key === 'missing-metadata') return key;
    const idx = key.indexOf(':');
    return idx === -1 ? key : key.slice(0, idx);
}

function seriesMatchesFilterKey(s, key) {
    if (key === 'missing') return s.missing_volumes.length > 0;
    if (key === 'oneshot') return s.is_oneshot;
    if (key === 'ebdz-unmatched') return s.ebdz_match_status === 'unmatched';
    if (key === 'missing-metadata') return s.volumes_without_metadata > 0;
    if (key.startsWith('genre:')) return splitMultiValue(s.local_genre).includes(key.slice('genre:'.length));
    if (key.startsWith('author:')) return splitMultiValue(s.local_author).includes(key.slice('author:'.length));
    if (key.startsWith('year:')) return (s.year || '') === key.slice('year:'.length);
    return true;
}

function filterSeries() {
    // "retire rechercher une série field dans bibliothèque" - le champ de recherche en
    // page (#search) a été retiré (redondant avec la recherche globale du header, voir
    // initHeaderSearch dans nav.js), mais filterSeries() reste le point d'entrée central
    // du filtrage/tri de la bibliothèque (appelé par les filtres de catégorie, le tri...
    // pas seulement par ce champ) - getElementById renvoie donc null ici désormais, d'où
    // ce garde plutôt que de casser tout le filtrage.
    const searchInput = document.getElementById('search');
    const searchTerm = normalizeForSearch(searchInput ? searchInput.value : '');
    // Recherche sur titre + auteur + genre + tags (texte pré-normalisé et mis en cache
    // dans _searchHaystack par loadLibraryData/buildSearchHaystack), pas seulement le titre
    let filtered = seriesData.filter(s =>
        (s._searchHaystack || buildSearchHaystack(s)).includes(searchTerm)
    );

    // "affiche maintenant les serie vide" - une série ajoutée à la bibliothèque (ex:
    // depuis Découvrir) mais dont aucun fichier n'a encore été importé n'était jusqu'ici
    // jamais montrée du tout - elle a désormais son propre statut "Vide" (voir
    // _seriesBadgeInfo) plutôt que de disparaître silencieusement de la liste.

    if (activeFilters.size > 0) {
        const byCategory = new Map();
        activeFilters.forEach(key => {
            const cat = getFilterCategory(key);
            if (!byCategory.has(cat)) byCategory.set(cat, []);
            byCategory.get(cat).push(key);
        });

        filtered = filtered.filter(s => {
            for (const keys of byCategory.values()) {
                if (!keys.some(k => seriesMatchesFilterKey(s, k))) return false;
            }
            return true;
        });
    }

    filtered = sortSeriesList(filtered);

    displaySeries(filtered);
}

// Trie une liste de séries selon seriesSortMode ('alpha', 'volumes' ou 'added'), sans
// muter le tableau d'origine (seriesData doit rester dans son ordre alphabétique de référence)
function sortSeriesList(list) {
    const sorted = [...list];
    if (seriesSortMode === 'volumes') {
        sorted.sort((a, b) => (b.total_volumes || 0) - (a.total_volumes || 0));
    } else if (seriesSortMode === 'added') {
        // Pas de colonne "date d'ajout" dédiée: l'id (auto-incrémenté, jamais réutilisé)
        // reflète déjà fidèlement l'ordre d'ajout, y compris pour les séries existantes
        sorted.sort((a, b) => (b.id || 0) - (a.id || 0));
    } else {
        sorted.sort((a, b) => a.title.localeCompare(b.title, 'fr'));
    }
    return sorted;
}

// Retire les menus déroulants "détachés" (voir toggleToolbarDropdown, anchorEl) restés
// enfants directs de <body> - à appeler avant tout re-rendu qui reconstruit une zone
// contenant un bouton ⚙️ molette (renderSeriesDetail, changement de vue/tri/colonnes des
// tomes...). Sans ce nettoyage, un menu déjà ouvert au moins une fois se retrouve
// dupliqué en id après le re-rendu (l'ancien exemplaire, orphelin, traîne encore dans
// <body> avec son dernier état - ex: le bouton "MAJ de ce tome" resté bloqué sur son ✅ de
// succès au lieu du texte normal) - constaté: "je reclique sur la molette, MAJ metadata
// devient icone validation alors que ca devrait rester MAJ metadata".
function cleanupDetachedDropdownMenus() {
    document.querySelectorAll('body > .toolbar-dropdown-menu').forEach(el => el.remove());
}

function toggleToolbarDropdown(menuId, anchorEl) {
    document.querySelectorAll('.toolbar-dropdown-menu').forEach(el => {
        if (el.id !== menuId) el.style.display = 'none';
    });
    const menu = document.getElementById(menuId);
    if (!menu) return;

    const opening = menu.style.display !== 'block';
    if (opening && anchorEl) {
        document.body.appendChild(menu);
        const rect = anchorEl.getBoundingClientRect();
        menu.style.position = 'fixed';

        // Mesure la taille réelle du menu (masqué mais affiché, donc mesurable) pour
        // savoir s'il tient en dessous du bouton et à droite avant de choisir où
        // l'ouvrir - sans ça, un bouton proche du bord (ex: colonne Actions tout à
        // droite d'un tableau large) ouvrait quand même le menu ancré à gauche du
        // bouton et le poussait hors de l'écran, invisible et inaccessible
        menu.style.visibility = 'hidden';
        menu.style.display = 'block';
        menu.style.left = '0';
        menu.style.right = 'auto';
        const menuWidth = menu.offsetWidth;
        const menuHeight = menu.offsetHeight;

        const spaceBelow = window.innerHeight - rect.bottom;
        const opensUpward = spaceBelow < menuHeight + 6 && rect.top > menuHeight + 6;
        menu.style.top = opensUpward ? `${rect.top - menuHeight - 6}px` : `${rect.bottom + 6}px`;

        const opensLeftward = rect.left + menuWidth > window.innerWidth && rect.right - menuWidth > 0;
        if (opensLeftward) {
            menu.style.left = 'auto';
            menu.style.right = `${window.innerWidth - rect.right}px`;
        } else {
            menu.style.left = `${rect.left}px`;
            menu.style.right = 'auto';
        }

        menu.style.visibility = 'visible';
        return;
    }
    menu.style.display = opening ? 'block' : 'none';
}

document.addEventListener('click', (e) => {
    // Menus "action" (⚙️ Actions série/tome, voir toolbar-dropdown-menu-autoclose):
    // chaque entrée déclenche une action ponctuelle (ouvre un modal, lance un fetch...),
    // jamais une sélection multiple - donc on referme systématiquement après un clic sur
    // une entrée, sinon le menu (détaché en position:fixed, très haut z-index) restait
    // ouvert par-dessus le modal que l'action venait d'ouvrir. Les dropdowns de filtre
    // (Genre/Auteur/Statut, sélection multiple) n'ont pas cette classe et gardent leur
    // comportement habituel (restent ouverts pour cocher plusieurs options).
    if (e.target.closest('.toolbar-dropdown-menu-autoclose button')) {
        document.querySelectorAll('.toolbar-dropdown-menu').forEach(el => el.style.display = 'none');
        return;
    }
    if (e.target.closest('.toolbar-dropdown') || e.target.closest('.toolbar-dropdown-menu')) return;
    document.querySelectorAll('.toolbar-dropdown-menu').forEach(el => el.style.display = 'none');
});

// Un menu détaché en position:fixed (voir toggleToolbarDropdown) reste plaqué à sa
// position d'ouverture si la page scrolle sous lui: plutôt que de le faire suivre (coût
// de recalcul à chaque scroll pour un menu qu'on referme de toute façon en général avant
// de scroller), on le referme simplement au premier scroll
document.querySelector('.content')?.addEventListener('scroll', () => {
    document.querySelectorAll('.toolbar-dropdown-menu').forEach(el => el.style.display = 'none');
}, { passive: true });

// Le menu "⚙️ Actions" se referme dès qu'on clique une entrée (voir plus haut), donc le
// "En cours..."/⏳ affiché sur l'entrée elle-même (bouton du menu) n'est jamais visible.
// Ces deux fonctions affichent plutôt ce statut sur l'icône ⚙️ elle-même, qui elle reste
// visible pendant toute la durée de l'action.
function setSeriesActionsGearBusy(seriesId, busy) {
    const gear = document.getElementById(`series-actions-gear-${seriesId}`);
    if (!gear) return;
    if (busy) {
        gear.dataset.originalIcon = gear.innerHTML;
        gear.innerHTML = '<span class="toolbar-btn-icon">⏳</span><span class="toolbar-btn-label">Actions</span>';
    } else if (gear.dataset.originalIcon !== undefined) {
        gear.innerHTML = gear.dataset.originalIcon;
        delete gear.dataset.originalIcon;
    }
}

function setVolumeActionsGearBusy(volumeId, busy) {
    const gear = document.getElementById(`volume-actions-gear-${volumeId}`);
    if (!gear) return;
    if (busy) {
        gear.dataset.originalIcon = gear.innerHTML;
        gear.innerHTML = '⏳';
    } else if (gear.dataset.originalIcon !== undefined) {
        gear.innerHTML = gear.dataset.originalIcon;
        delete gear.dataset.originalIcon;
    }
}

// Découpe une valeur multi-valuée d'une série (ex: genre "Histoire, Guerre" ou auteur
// "Alcante, Bollée" issus du ComicInfo.xml) en une liste de valeurs individuelles
function splitMultiValue(value) {
    if (!value) return [];
    return value.split(',').map(v => v.trim()).filter(Boolean);
}

// Description de chaque section de filtres dynamiques: id du conteneur dans le menu
// déroulant, champ de seriesData à lire, préfixe de mode utilisé par filterSeries(),
// icône affichée devant chaque option, si la valeur doit être découpée (multi-valeur)
// ou utilisée telle quelle (valeur unique, ex: année)
const DYNAMIC_FILTER_SECTIONS = [
    { containerId: 'genre-filter-list', field: 'local_genre', prefix: 'genre:', icon: '🏷️', multi: true },
    { containerId: 'author-filter-list', field: 'local_author', prefix: 'author:', icon: '✍️', multi: true },
    { containerId: 'year-filter-list', field: 'year', prefix: 'year:', icon: '📅', multi: false }
];

// Reconstruit les sous-listes du menu Filtrer (genre/auteur/année) à partir des valeurs
// locales (ComicInfo.xml) présentes dans seriesData, puisqu'elles dépendent du contenu de
// chaque bibliothèque et ne peuvent pas être codées en dur comme les autres options
function populateDynamicFilterOptions() {
    DYNAMIC_FILTER_SECTIONS.forEach(section => {
        const container = document.getElementById(section.containerId);
        const sectionEl = document.getElementById(section.containerId.replace('-list', '-section'));
        if (!container) return;

        const values = new Set();
        seriesData.forEach(s => {
            const raw = s[section.field];
            if (section.multi) {
                splitMultiValue(raw).forEach(v => values.add(v));
            } else if (raw) {
                values.add(String(raw));
            }
        });

        const sorted = section.field === 'year'
            ? [...values].sort((a, b) => b.localeCompare(a))
            : [...values].sort((a, b) => a.localeCompare(b, 'fr'));

        if (sorted.length === 0) {
            container.innerHTML = '';
            if (sectionEl) sectionEl.style.display = 'none';
            return;
        }

        container.innerHTML = sorted.map(v => `
            <button class="toolbar-dropdown-item${activeFilters.has(section.prefix + v) ? ' active' : ''}"
                    data-filter="${section.prefix}${escapeHtml(v)}" onclick="toggleSeriesFilter(this.dataset.filter)">${section.icon} ${escapeHtml(v)}</button>
        `).join('');
        if (sectionEl) sectionEl.style.display = '';
    });
}

// Libellé lisible d'une clé de filtre, utilisé pour les badges dans la toolbar
function getFilterKeyLabel(key) {
    if (key === 'missing') return '⚠️ Volumes manquants';
    if (key === 'oneshot') return '🔸 One-Shot';
    if (key === 'ebdz-unmatched') return '🎯 Matching EBDZ';
    if (key === 'missing-metadata') return '📄 Sans métadonnées';
    for (const section of DYNAMIC_FILTER_SECTIONS) {
        if (key.startsWith(section.prefix)) {
            return `${section.icon} ${key.slice(section.prefix.length)}`;
        }
    }
    return key;
}

// Affiche les filtres actifs sous forme de badges dans la toolbar, juste à côté du
// bouton Filtrer (un badge par filtre, chacun retirable individuellement, plus un
// bouton "Tout effacer" dès qu'il y en a plusieurs)
function updateActiveFilterBadges() {
    const container = document.getElementById('active-filter-badges');
    if (!container) return;

    if (activeFilters.size === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }

    container.style.display = 'inline-flex';
    const pills = [...activeFilters].map(key => `
        <span class="active-filter-badge" data-filter-key="${escapeHtml(key)}">
            <span>${escapeHtml(getFilterKeyLabel(key))}</span>
            <button type="button" class="active-filter-badge-clear" onclick="toggleSeriesFilter(this.parentElement.dataset.filterKey)" aria-label="Retirer ce filtre">${svgIcon('x')}</button>
        </span>
    `).join('');
    const clearAll = activeFilters.size > 1
        ? `<button type="button" class="active-filter-clear-all" onclick="clearSeriesFilters()">Tout effacer</button>`
        : '';
    container.innerHTML = pills + clearAll;
}

// Persistance des filtres actifs et du mode de tri d'une bibliothèque dans le
// localStorage (comme seriesViewMode déjà), pour qu'ils survivent à une navigation vers
// une autre page puis un retour, ou à un simple rechargement de la page (F5). Scopé par
// libraryId puisque les filtres dynamiques (genre/auteur/année) sont propres à chaque
// bibliothèque
function getFilterStorageKey(suffix) {
    return `libraryFilters_${libraryId}_${suffix}`;
}

function saveFilterState() {
    try {
        localStorage.setItem(getFilterStorageKey('active'), JSON.stringify([...activeFilters]));
        localStorage.setItem(getFilterStorageKey('sort'), seriesSortMode);
    } catch (e) {
        // localStorage indisponible (navigation privée, quota...): pas bloquant, on
        // continue simplement sans persistance
        console.warn('Impossible de sauvegarder l\'état des filtres:', e);
    }
}

// Restaure les filtres/tri sauvegardés pour cette bibliothèque, appelé une fois au
// chargement de la page, avant le premier appel à loadLibraryData()
function restoreFilterState() {
    try {
        const savedFilters = JSON.parse(localStorage.getItem(getFilterStorageKey('active')) || '[]');
        activeFilters = new Set(Array.isArray(savedFilters) ? savedFilters : []);
        const savedSort = localStorage.getItem(getFilterStorageKey('sort'));
        if (savedSort) seriesSortMode = savedSort;
    } catch (e) {
        activeFilters = new Set();
    }
    updateSortDropdownActiveButton();
}

// Ajoute/retire une clé de filtre de la sélection multiple active
function toggleSeriesFilter(key) {
    if (activeFilters.has(key)) {
        activeFilters.delete(key);
    } else {
        activeFilters.add(key);
    }
    refreshFilterDropdownUI();
    saveFilterState();
    filterSeries();
}

function clearSeriesFilters() {
    activeFilters.clear();
    refreshFilterDropdownUI();
    saveFilterState();
    filterSeries();
}

// Boutons de la toolbar déclenchant chacun un dropdown de filtre dédié (Statut/Genre/
// Auteur/Année, cf. templates/library.html): utilisé pour mettre en surbrillance le
// bouton lui-même dès qu'un filtre de sa catégorie est actif, même dropdown fermé
const FILTER_TOOLBAR_BUTTONS = [
    { id: 'status-filter-btn', match: key => key === 'missing' || key === 'oneshot' || key === 'ebdz-unmatched' || key === 'missing-metadata' },
    { id: 'genre-filter-btn', match: key => key.startsWith('genre:') },
    { id: 'author-filter-btn', match: key => key.startsWith('author:') },
    { id: 'year-filter-btn', match: key => key.startsWith('year:') }
];

function refreshFilterDropdownUI() {
    // Les items de filtre (data-filter) sont maintenant répartis sur plusieurs menus
    // (un par catégorie) au lieu d'un seul menu géant: on les cible tous globalement,
    // le sélecteur reste sans ambiguïté puisque seuls ces items portent data-filter.
    document.querySelectorAll('.toolbar-dropdown-item[data-filter]').forEach(btn => {
        btn.classList.toggle('active', activeFilters.has(btn.dataset.filter));
    });
    FILTER_TOOLBAR_BUTTONS.forEach(({ id, match }) => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.toggle('toolbar-btn-filter-active', [...activeFilters].some(match));
    });
    updateActiveFilterBadges();
}

// Met à jour l'état visuel (classe active) des options du menu Ordonner d'après
// seriesSortMode, sans ouvrir/fermer le menu (utilisé à la fois par setSeriesSortMode et
// par restoreFilterState au chargement de la page)
function updateSortDropdownActiveButton() {
    document.querySelectorAll('#sort-dropdown-menu .toolbar-dropdown-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.sort === seriesSortMode);
    });
}

function setSeriesSortMode(mode) {
    seriesSortMode = mode;
    updateSortDropdownActiveButton();
    toggleToolbarDropdown('sort-dropdown-menu');
    saveFilterState();
    filterSeries();
}

// "le bouton recharger c'est hyper lent. ca fait un scan en cours. alors que ca devrait
// juste recharger la bibliotheque" - un rechargement doit juste rejouer loadLibraryData
// (relit la BD, déjà tenue à jour par pollLibraryFreshness/le scheduler d'import
// automatique) plutôt que scanLibrary/POST /api/scan/<id> (relit CHAQUE fichier sur
// disque - page_count/ComicInfo/couverture - lent sur une grosse bibliothèque). Un vrai
// scan disque reste accessible depuis /?all=1 (bouton "Scanner" de index.js) pour le cas
// où un fichier a été déposé manuellement en dehors du pipeline d'import.
async function reloadLibraryData(buttonEl) {
    if (buttonEl) buttonEl.disabled = true;
    try {
        await loadLibraryData();
    } finally {
        if (buttonEl) buttonEl.disabled = false;
    }
}

async function scanLibrary(passedLibraryId, forceFull, buttonEl) {
    // Support deux modes d'appel:
    // 1. Depuis library.html: sans libraryId, utilise le libraryId global; buttonEl est
    //    passé explicitement par le bouton (via "this") plutôt que de dépendre de
    //    l'event global implicite, qui peut ne plus être fiable une fois qu'on est dans
    //    une fonction async (ex: après un confirm() imbriqué) et laisser le bouton
    //    bloqué sur "Scan en cours..." si jamais il ne pointe plus vers le bon élément
    // 2. Depuis index.html: avec libraryId en paramètre, pas de bouton à mettre à jour
    // forceFull: scan complet (ré-extrait page_count/ComicInfo.xml/couverture de tous les
    // fichiers, même inchangés) au lieu du scan rapide par défaut (fichiers nouveaux/modifiés uniquement)

    let libId = passedLibraryId || libraryId;

    if (!libId) {
        alert('❌ Erreur: Aucune bibliothèque sélectionnée');
        return;
    }

    const scanUrl = `/api/scan/${libId}${forceFull ? '?force=1' : ''}`;

    // Vérifier si on est sur library.html en regardant si l'élément series-grid existe
    const isLibraryPage = document.getElementById('series-grid') !== null;

    if (isLibraryPage && buttonEl) {
        // Mode library.html: bouton "Scanner"/"Scan complet" de la toolbar
        const button = buttonEl;
        const originalHtml = button.innerHTML;
        button.disabled = true;
        button.innerHTML = '<span class="toolbar-btn-icon">⏳</span><span class="toolbar-btn-label">Scan en cours...</span>';

        try {
            const response = await fetch(scanUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            const data = await response.json();

            if (data.success) {
                await loadLibraryData();
            } else {
                alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            }
        } catch (error) {
            alert('❌ Erreur de connexion: ' + error.message);
        } finally {
            button.disabled = false;
            button.innerHTML = originalHtml;
        }
    } else {
        // Mode index.html: le bouton "Scanner" sur la liste des bibliothèques
        if (!confirm('Voulez-vous scanner cette bibliothèque ? Cela peut prendre du temps.')) {
            return;
        }

        try {
            const response = await fetch(scanUrl);
            const data = await response.json();

            if (data.success) {
                alert(`✅ Scan terminé ! ${data.series_count} séries trouvées.`);
                location.reload();
            } else {
                alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            }
        } catch (error) {
            alert('❌ Erreur: ' + error.message);
        }
    }
}

async function scanSeries(seriesId, forceFull) {
    // forceFull: scan complet (ré-extrait page_count/ComicInfo.xml/couverture de tous
    // les volumes, même inchangés) au lieu du scan rapide par défaut (ne retraite que
    // les fichiers nouveaux/modifiés depuis le dernier scan)
    setSeriesActionsGearBusy(seriesId, true);
    try {
        const url = `/api/scan/series/${seriesId}${forceFull ? '?force=1' : ''}`;
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.success && data.deleted) {
            // Le répertoire n'existe plus: la série a été supprimée de la base. On ne
            // peut plus afficher sa page de détail, retour à la bibliothèque
            alert('🗑️ ' + data.message);
            const libId = (currentSeriesDetail && currentSeriesDetail.library && currentSeriesDetail.library.id) || libraryId;
            window.location.href = libId ? `/library/${libId}` : '/';
        } else if (data.success) {
            // Recharger la vue de détail en place pour voir les changements - seulement
            // si on est bien sur la page de détail (#modal-body n'existe pas quand ce
            // bouton est déclenché depuis l'overlay au survol d'une affiche de la grille)
            if (document.getElementById('modal-body')) {
                await renderSeriesDetail(seriesId);
            } else if (typeof loadLibraryData === 'function') {
                loadLibraryData();
            }
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            setSeriesActionsGearBusy(seriesId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        setSeriesActionsGearBusy(seriesId, false);
    }
}

// Upload direct d'un fichier de tome depuis la fiche série ("ajoute une option upload
// fichier pour mettre à jour une série") - alternative au dépôt dans un répertoire
// surveillé (aMule/torrents) suivi d'une assignation manuelle sur /import, pour un ajout
// ponctuel sans attendre un téléchargement externe. Un seul <input type="file"> caché est
// créé/réutilisé à la demande (id fixe) plutôt qu'un par série: la fiche série n'a qu'une
// seule instance affichée à la fois.
// forcedVolumeOverride: connu d'avance quand l'envoi part du menu ⚙️ d'UN tome précis
// (voir buildVolumeActionsGearHtml) - pas besoin de redemander à l'utilisateur quel tome
// c'est (_confirmUploadVolumeSlot) puisque c'est justement celui dont il vient de cliquer
// le menu. undefined depuis le header de la série (one-shot): là, le tome cible n'est pas
// encore connu, la confirmation reste nécessaire.
function triggerSeriesFileUpload(seriesId, forcedVolumeOverride) {
    let input = document.getElementById('series-file-upload-input');
    if (!input) {
        input = document.createElement('input');
        input.type = 'file';
        input.id = 'series-file-upload-input';
        input.accept = '.cbz,.cbr,.zip,.rar,.pdf';
        input.style.display = 'none';
        document.body.appendChild(input);
    }
    input.value = '';
    input.onchange = () => {
        if (input.files && input.files[0]) {
            uploadSeriesFile(seriesId, input.files[0], forcedVolumeOverride);
        }
    };
    input.click();
}

// Un fichier envoyé sans numéro de tome reconnu ("pour une série avec volume ajouter un
// fichier ça va où?") demande confirmation plutôt que de s'insérer à l'aveugle comme
// édition sans numéro - même liste de tomes connus de la série (possédés ou emplacements
// réservés Bédéthèque) que le sélecteur équivalent de la page /import (voir
// updateVolumeOverrideVisibility/buildVolumeOverride dans import.js), reconstruite ici en
// modale légère créée à la demande. Retourne un objet volume_override (vide si "sans
// numéro" délibérément choisi), ou null si l'utilisateur annule.
// Résolveur de la promesse en cours (voir cleanup ci-dessous) - permis à
// closeUploadVolumeConfirmModal (fermeture par ✕/Échap/clic sur le fond, voir
// MODAL_CLOSE_FUNCTIONS dans nav.js) de résoudre proprement en "annulé" au lieu de
// laisser _confirmUploadVolumeSlot en attente indéfiniment (uploadSeriesFile resterait
// alors bloqué en plein milieu, toast/état "busy" jamais nettoyés)
let _uploadVolumeConfirmResolve = null;

function closeUploadVolumeConfirmModal() {
    const modal = document.getElementById('upload-volume-confirm-modal');
    if (modal) modal.classList.remove('active');
    if (_uploadVolumeConfirmResolve) {
        const resolve = _uploadVolumeConfirmResolve;
        _uploadVolumeConfirmResolve = null;
        resolve(null);
    }
}

async function _confirmUploadVolumeSlot(seriesId, fileData) {
    const parsed = fileData.parsed;
    const isAmbiguous = parsed.volume == null && !parsed.is_integral && !parsed.is_hs && !parsed.is_episode;
    if (!isAmbiguous) return {};

    let volumes = [];
    try {
        const resp = await fetch(`/api/series/${seriesId}/volumes`);
        volumes = await resp.json();
    } catch (e) {
        volumes = [];
    }

    return new Promise(resolve => {
        let modal = document.getElementById('upload-volume-confirm-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'upload-volume-confirm-modal';
            modal.className = 'modal';
            document.body.appendChild(modal);
        }

        // Libellé partagé, voir buildVolumeOptionLabel (nav.js) - dernière des 4 copies de
        // cette construction à être consolidée (voir _bdVolumeOptionLabel/
        // _manualEditVolumeOptionLabel dans ce même fichier).
        const optionsHtml = volumes.map((v, index) =>
            `<option value="${index}">${escapeHtml(buildVolumeOptionLabel(v, { showOwned: true }))}</option>`
        ).join('');

        modal.innerHTML = `
            <div class="modal-content" style="max-width:480px;">
                <span class="close-modal" id="upload-volume-confirm-x">×</span>
                <h2 class="modal-title">Quel tome est-ce ?</h2>
                <p class="modal-subtitle">« ${escapeHtml(fileData.filename)} » ne précise pas de numéro de tome reconnu.</p>
                <div class="form-group">
                    <select id="upload-volume-confirm-slot">
                        ${optionsHtml}
                        <option value="manual">Autre (préciser un numéro)</option>
                        <option value="none">Sans numéro (édition unique)</option>
                    </select>
                    <div id="upload-volume-confirm-manual" style="display:none; margin-top:8px;">
                        <select id="upload-volume-confirm-type">
                            <option value="volume">Tome numéroté</option>
                            <option value="integral">Intégrale</option>
                            <option value="hs">Hors-série</option>
                            <option value="episode">Épisode</option>
                        </select>
                        <input type="number" id="upload-volume-confirm-number" placeholder="Numéro" style="margin-top:6px;">
                    </div>
                </div>
                <div class="form-actions">
                    <button class="btn btn-neutral-sm" id="upload-volume-confirm-cancel">Annuler l'ajout</button>
                    <button class="btn btn-success" id="upload-volume-confirm-ok">Valider</button>
                </div>
            </div>
        `;
        modal.classList.add('active');

        document.getElementById('upload-volume-confirm-slot').onchange = function() {
            document.getElementById('upload-volume-confirm-manual').style.display = this.value === 'manual' ? 'block' : 'none';
        };

        _uploadVolumeConfirmResolve = resolve;
        const cleanup = (result) => {
            modal.classList.remove('active');
            _uploadVolumeConfirmResolve = null;
            resolve(result);
        };
        document.getElementById('upload-volume-confirm-x').onclick = () => cleanup(null);
        document.getElementById('upload-volume-confirm-cancel').onclick = () => cleanup(null);
        document.getElementById('upload-volume-confirm-ok').onclick = () => {
            const slot = document.getElementById('upload-volume-confirm-slot').value;
            if (slot === 'none') { cleanup({}); return; }
            if (slot === 'manual') {
                const type = document.getElementById('upload-volume-confirm-type').value;
                const raw = document.getElementById('upload-volume-confirm-number').value.trim();
                const number = raw === '' ? null : parseInt(raw, 10);
                if (type === 'volume') cleanup({ volume: number, is_integral: false, integral_number: null, is_hs: false, hs_number: null, is_episode: false, episode_number: null });
                else if (type === 'integral') cleanup({ volume: null, is_integral: true, integral_number: number, is_hs: false, hs_number: null, is_episode: false, episode_number: null });
                else if (type === 'hs') cleanup({ volume: null, is_integral: false, integral_number: null, is_hs: true, hs_number: number, is_episode: false, episode_number: null });
                else cleanup({ volume: null, is_integral: false, integral_number: null, is_hs: false, hs_number: null, is_episode: true, episode_number: number });
                return;
            }
            const v = volumes[parseInt(slot, 10)];
            cleanup({
                volume: v.volume_number != null ? v.volume_number : null,
                is_integral: !!v.is_integral,
                integral_number: v.integral_number != null ? v.integral_number : null,
                is_hs: !!v.is_hs,
                hs_number: v.hs_number != null ? v.hs_number : null,
                is_episode: !!v.is_episode,
                episode_number: v.episode_number != null ? v.episode_number : null
            });
        };
    });
}

async function uploadSeriesFile(seriesId, file, forcedVolumeOverride) {
    setSeriesActionsGearBusy(seriesId, true);
    showToast('series-upload', `Envoi de « ${file.name} »...`, { icon: 'upload' });

    try {
        const formData = new FormData();
        formData.append('file', file);

        const uploadResponse = await fetch(`/api/series/${seriesId}/upload-file`, {
            method: 'POST',
            body: formData
        });
        const uploadData = await uploadResponse.json();

        if (!uploadData.success) {
            alert('❌ Erreur: ' + (uploadData.error || "Échec de l'envoi"));
            return;
        }

        const volumeOverride = forcedVolumeOverride || await _confirmUploadVolumeSlot(seriesId, uploadData.file);
        if (volumeOverride === null) {
            // Annulé: le fichier envoyé reste orphelin dans _uploads/<series_id>/ sans ce
            // nettoyage explicite (jamais réintégré au scan normal, voir upload_series_file)
            await fetch('/api/import/file', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    import_root: uploadData.file.import_root,
                    relative_path: uploadData.file.relative_path
                })
            }).catch(() => {});
            showToast('series-upload', 'Ajout annulé', { icon: 'info', autoHideMs: 3000 });
            return;
        }
        if (Object.keys(volumeOverride).length > 0) {
            uploadData.file.destination.volume_override = volumeOverride;
        }

        // Le fichier envoyé est déjà rattaché à cette série (destination pré-remplie par
        // le serveur, voir upload_series_file) - réutilise tel quel le pipeline d'import
        // existant (dédoublonnage par format, conversion cbr->cbz, ComicInfo, scan Komga)
        // au lieu d'en dupliquer la logique ici.
        showToast('series-upload', `Import de « ${file.name} »...`, { icon: 'download' });
        const importResponse = await fetch('/api/import/execute', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files: [uploadData.file] })
        });
        const importData = await importResponse.json();

        if (!importData.success) {
            alert('❌ Erreur: ' + (importData.error || "Échec de l'import"));
            return;
        }
        if (importData.failed_count > 0) {
            const failure = (importData.failures && importData.failures[0]) || {};
            alert(`❌ Échec de l'import: ${failure.error || 'erreur inconnue'}`);
            return;
        }

        showToast('series-upload', '✅ Fichier ajouté', { icon: 'check', autoHideMs: 4000 });
        await renderSeriesDetail(seriesId);
    } catch (error) {
        alert(`❌ Erreur de connexion: ${error.message}`);
    } finally {
        dismissToast('series-upload');
        setSeriesActionsGearBusy(seriesId, false);
    }
}

async function toggleOneshot(seriesId) {
    setSeriesActionsGearBusy(seriesId, true);
    try {
        const response = await fetch(`/api/series/${seriesId}/toggle-oneshot`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.success) {
            // Recharger la vue de détail pour afficher le nouvel état
            renderSeriesDetail(seriesId);

            // Recharger aussi la liste pour mettre à jour les badges (si on est dans library.html)
            setTimeout(() => {
                if (typeof loadLibraryData === 'function') {
                    loadLibraryData();
                }
            }, 500);
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            setSeriesActionsGearBusy(seriesId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        setSeriesActionsGearBusy(seriesId, false);
    }
}

// Bascule la déclaration manuelle "série complète" ("ajoute une option dans la molette
// pour déclarer cet album complet") - prime sur le calcul automatique du badge
// (_seriesBadgeInfo) quel qu'il soit, voir toggle_series_complete_override côté Flask.
async function toggleSeriesCompleteOverride(seriesId) {
    setSeriesActionsGearBusy(seriesId, true);
    try {
        const response = await fetch(`/api/series/${seriesId}/toggle-complete-override`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        const data = await response.json();

        if (data.success) {
            renderSeriesDetail(seriesId);
            setTimeout(() => {
                if (typeof loadLibraryData === 'function') {
                    loadLibraryData();
                }
            }, 500);
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            setSeriesActionsGearBusy(seriesId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        setSeriesActionsGearBusy(seriesId, false);
    }
}

// Bascule la surveillance "façon Sonarr" d'une série (table missing_volume_monitor,
// voir MissingVolumeDetector.create_monitor_entry côté Flask) - réutilise l'endpoint
// existant de /missing-monitor plutôt que d'en créer un dédié à la fiche série.
// auto_download_enabled n'est jamais activé depuis ce bouton: le téléchargement reste
// toujours une action manuelle (bouton par résultat de recherche), voir searchMissingVolume.
async function toggleSeriesMonitor(seriesId, currentlyMonitored) {
    const btn = document.getElementById(`monitor-btn-${seriesId}`);
    if (btn) btn.disabled = true;
    try {
        const response = await fetch(`/api/missing-monitor/series/${seriesId}/monitor`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !currentlyMonitored })
        });
        const data = await response.json();
        if (data.success) {
            // Mutation locale uniquement : icône/label/tooltip du bouton, sans
            // recharger ni rerendre toute la fiche série (voir demande utilisateur).
            const monitoredNow = !!data.monitored;
            if (btn) {
                btn.setAttribute('onclick', `toggleSeriesMonitor(${seriesId}, ${monitoredNow ? 'true' : 'false'})`);
                btn.setAttribute('data-tooltip', monitoredNow
                    ? 'Série surveillée: recherche EBDZ/Prowlarr des tomes manquants active'
                    : 'Activer la surveillance de cette série (recherche EBDZ/Prowlarr des tomes manquants)');
                const iconEl = btn.querySelector('.toolbar-btn-icon');
                const labelEl = btn.querySelector('.toolbar-btn-label');
                if (iconEl) iconEl.innerHTML = svgIcon(monitoredNow ? 'eye' : 'eye-off');
                if (labelEl) labelEl.textContent = monitoredNow ? 'Surveillé' : 'Surveiller';
                if (typeof currentSeriesDetail !== 'undefined' && currentSeriesDetail && currentSeriesDetail.id === seriesId) {
                    currentSeriesDetail.monitored = monitoredNow;
                }
                btn.disabled = false;
            }
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            if (btn) btn.disabled = false;
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        if (btn) btn.disabled = false;
    }
}

async function deleteSeries(seriesId) {
    // currentSeriesDetail est déjà chargé pour la série affichée (renderSeriesDetail),
    // on l'utilise pour retrouver le titre exact et la bibliothèque d'origine sans
    // requête supplémentaire. Repli sur seriesData (liste de la page bibliothèque, voir
    // buildQuickActionsHtml) quand l'appel vient de la grille/du tableau plutôt que de la
    // fiche série - sans lui le dialogue de confirmation affichait juste "#123" au lieu
    // du vrai titre.
    const series = (currentSeriesDetail && currentSeriesDetail.id === seriesId)
        ? currentSeriesDetail
        : (typeof seriesData !== 'undefined' ? seriesData.find(s => s.id === seriesId) : null);
    const title = series ? series.title : `#${seriesId}`;

    // Action irréversible (supprime aussi les fichiers du disque): simple confirm(),
    // sans exiger de retaper le titre
    if (!confirm(`⚠️ Action irréversible : supprime définitivement "${title}" ET tous ses fichiers du disque.\n\nConfirmer la suppression ?`)) {
        return;
    }

    try {
        const response = await fetch(`/api/series/${seriesId}`, { method: 'DELETE' });
        const data = await response.json();

        if (data.success) {
            alert(`🗑️ "${title}" supprimée définitivement.`);
            const libId = data.library_id || (series && series.library && series.library.id) || libraryId;
            window.location.href = libId ? `/library/${libId}` : '/';
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Ouvre la page dédiée au détail d'une série (au lieu d'une fenêtre modale)
function viewSeries(seriesId) {
    window.location.href = `/series/${seriesId}`;
}

// Navigation ⬅️/➡️ (et raccourci clavier ←/→) SANS rechargement complet de page -
// contrairement à viewSeries ci-dessus, utilisé partout ailleurs (bibliothèque,
// recherche, découvrir) pour ouvrir une fiche série la première fois, où un vrai
// changement de page reste approprié (contexte/scripts différents). Ici on reste sur la
// même page: renderSeriesDetail refait déjà tout le travail utile (fetch + reconstruction
// du DOM, y compris la lecture du cache sessionStorage préchargé) - il ne manquait que la
// gestion d'URL/historique pour que ⬅️/➡️ n'aient plus besoin d'un aller-retour navigateur
// complet (chargement HTML/CSS/JS) pour un simple changement de série.
async function navigateToSeriesInPage(seriesId) {
    // Un rechargement complet remettait ces deux choses à zéro gratuitement - à refaire
    // explicitement ici: un menu ⚙️ resté ouvert de la fiche précédente n'a plus de sens
    // une fois son tome disparu du DOM, et rester scrollé en bas de l'ancienne fiche en
    // arrivant sur une nouvelle donne l'impression que rien n'a changé
    document.querySelectorAll('.toolbar-dropdown-menu').forEach(el => el.style.display = 'none');
    const contentEl = document.querySelector('.content');
    if (contentEl) contentEl.scrollTop = 0;

    await renderSeriesDetail(seriesId);
    // pushState APRÈS le rendu: si renderSeriesDetail devait échouer, l'URL ne change
    // pas et on ne pousse pas d'entrée d'historique vers une page qui n'a pas pu charger
    history.pushState({ seriesId }, '', `/series/${seriesId}`);
    if (currentSeriesTitle) document.title = currentSeriesTitle;
}

// Bouton retour/avant du navigateur après une navigation ⬅️/➡️ en AJAX (voir
// navigateToSeriesInPage): popstate se déclenche SANS recharger la page quand on a
// utilisé pushState, donc il faut explicitement refaire le rendu ici - sans ce listener,
// le bouton retour changerait l'URL affichée mais laisserait la fiche précédente à l'écran.
window.addEventListener('popstate', (e) => {
    const seriesId = e.state && e.state.seriesId;
    // history.state est vide sur l'entrée d'origine (chargement serveur classique, pas de
    // pushState) - dans ce cas seul un vrai rechargement peut retrouver ce state initial
    if (!seriesId) {
        window.location.reload();
        return;
    }
    renderSeriesDetail(seriesId).then(() => {
        if (currentSeriesTitle) document.title = currentSeriesTitle;
    });
});

// Construit les badges de métadonnées (tags/auteur/artistes/éditeur/résolution) d'un
// volume à partir de son ComicInfo.xml, avec repli sur les infos extraites du nom de
// fichier. Partagé par la carte volume (buildVolumeItemHtml) et le header d'un
// one-shot (celui-ci n'affiche pas de carte volume, voir renderSeriesDetail)
function buildVolumeMetaBadgesHtml(v) {
    const ci = v.comicinfo || {};
    const hasNoMetadata = Object.keys(ci).length === 0;
    const artists = [ci.penciller, ci.inker, ci.colorist].filter(Boolean);
    const uniqueArtists = [...new Set(artists)];

    return `
        ${hasNoMetadata ? `<span class="badge badge-warning" title="Pas de ComicInfo.xml exploitable dans ce fichier (absent, illisible, ou sans aucun champ rempli)">⚠️ Pas de métadonnées</span>` : ''}
        ${v.is_integral ? `<span class="badge badge-integral" title="Regroupe plusieurs tomes en un seul livre, pas de tome dédié nécessaire">📦 Intégrale${v.integral_number ? ' ' + v.integral_number : ''}</span>` : ''}
        ${v.is_hs ? `<span class="badge badge-hs" title="Hors-série: en dehors de la numérotation normale de la série">✨ Hors-série${v.hs_number ? ' ' + v.hs_number : ''}</span>` : ''}
        ${v.is_episode ? `<span class="badge badge-episode" title="Épisode: publication séparée des tomes qui la compilent ensuite">🎬 Épisode${v.episode_number ? ' ' + v.episode_number : ''}</span>` : ''}
        ${ci.writer ? `<span class="badge">✍️ ${escapeHtml(ci.writer)}</span>` : (v.author ? `<span class="badge">👤 ${escapeHtml(v.author)}</span>` : '')}
        ${uniqueArtists.length ? `<span class="badge">🎨 ${escapeHtml(uniqueArtists.join(', '))}</span>` : ''}
        ${ci.publisher ? `<span class="badge">🏢 ${escapeHtml(ci.publisher)}</span>` : ''}
        ${ci.genre ? `<span class="badge">🏷️ ${escapeHtml(ci.genre)}</span>` : ''}
        ${v.resolution ? `<span class="badge" title="Qualité/résolution du scan">🖼️ ${escapeHtml(String(v.resolution))}</span>` : ''}
        ${v.release_group ? `<span class="badge" title="Releaser">📀 ${escapeHtml(String(v.release_group))}</span>` : ''}
    `;
}

// Construit la ligne "année • pages • taille • format" d'un volume. Partagée par la
// carte volume (buildVolumeItemHtml) et le header d'un one-shot (voir buildVolumeMetaBadgesHtml).
// v.format est toujours renseigné pour un tome réellement possédé (celui-ci n'est appelé
// que dans ce cas, voir buildVolumeItemHtml - un tome "placeholder" sans fichier a sa
// propre ligne de détail, pas celle-ci)
function buildVolumeFileDetailHtml(v) {
    const ci = v.comicinfo || {};
    // Note Bédéthèque: existait déjà en colonne optionnelle de la vue tableau (voir
    // VOLUME_TABLE_OPTIONAL_COLUMNS) mais un one-shot n'affiche jamais cette table (pas
    // de carte volume classique, voir renderSeriesDetail) - sans l'ajouter ici aussi, sa
    // note restait invisible nulle part. buildVolumeFileDetailHtml est partagé avec la
    // carte tome classique (buildVolumeItemHtml), qui en profite donc aussi.
    // Cliquable -> modale des avis de lecteurs Bédéthèque (voir openBedethequeReviewsModal):
    // la note seule ne dit rien du POURQUOI, l'utilisateur a demandé à pouvoir lire le
    // texte des avis sans quitter la fiche série
    // "avis est incorrect, c'est le nombre de votes" - communityratingcount (Bédéthèque)
    // compte les gens qui ont NOTÉ l'album, pas ceux qui ont écrit un texte d'avis (les
    // deux peuvent diverger) - "votes" est le mot exact, "avis" laissait croire à tort
    // qu'il y avait forcément ce nombre de textes à lire dans la modale.
    const ratingHtml = ci.communityrating
        ? ` • <span class="bd-rating-link" data-tooltip="Voir les avis Bédéthèque" onclick="openBedethequeReviewsModal(${v.id})">⭐ ${escapeHtml(String(ci.communityrating))}/5${ci.communityratingcount ? ` (${escapeHtml(String(ci.communityratingcount))} votes)` : ''}</span>`
        : '';
    return `${(ci.year || v.year) ? `📅 ${escapeHtml(String(ci.year || v.year))} • ` : ''}📄 ${v.page_count} pages • 💾 ${formatBytes(v.file_size)} • ${(v.format || '').toUpperCase()}${ratingHtml}`;
}

// Construit les actions propres à un tome précis (MAJ métadonnées, conversion
// cbr->cbz, renommage): partagé par la carte volume (buildVolumeItemHtml) et le header
// d'un one-shot/intégrale (celui-ci n'affiche pas de carte volume, voir
// renderSeriesDetail) - sans ce partage, ces actions étaient inaccessibles pour ces
// séries puisqu'aucune carte volume n'est rendue pour elles
//
// Liens directs Komga/Bédéthèque + icône molette (actions): les liens ci-dessous pointent
// vers CE tome précis (livre Komga individuel / page d'album Bédéthèque), différents de
// ceux du bloc "🔗 Liens" du header qui pointent vers la SÉRIE - donc pas de doublon,
// chacun mène à une page différente
// Liens directs vers ce tome précis (Komga: livre matché pour ce volume; Bédéthèque: URL
// du tag <Web> de ComicInfo.xml), regroupés derrière une icône 🔗. Extrait de
// buildVolumeActionIconsHtml pour être réutilisable seul par la carte "placeholder" (tome
// non possédé, voir plus bas) - celle-ci n'a pas de fichier donc pas de menu ⚙️ Actions
// (MAJ métadonnées/conversion/renommage n'ont pas de sens sans fichier), mais le lien
// Bédéthèque de l'album, lui, est déjà connu dès la création du placeholder et doit rester
// consultable.
function buildVolumeLinksIconHtml(v) {
    const ci = v.comicinfo || {};

    const komgaLinkHtml = v.komga_book_url
        ? `<a href="${escapeHtml(v.komga_book_url)}" target="_blank" rel="noopener" class="volume-link-icon" title="Ouvrir ce tome sur Komga"><img src="/static/img/komga-logo.svg" alt="Komga"></a>`
        : '';
    const bedethequeUrl = (ci.web && /bedetheque\.com/i.test(ci.web)) ? ci.web : null;
    const bedethequeLinkHtml = bedethequeUrl
        ? `<a href="${escapeHtml(bedethequeUrl)}" target="_blank" rel="noopener" class="volume-link-icon" title="Ouvrir ce tome sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque"></a>`
        : '';
    if (!komgaLinkHtml && !bedethequeLinkHtml) return '';

    const linksMenuId = `volume-links-menu-${v.id}`;
    return `
        <div class="toolbar-dropdown">
            <button type="button" class="volume-link-icon" onclick="toggleToolbarDropdown('${linksMenuId}', this)" title="Liens externes pour ce tome">${svgIcon('link')}</button>
            <div class="toolbar-dropdown-menu series-detail-links-menu" id="${linksMenuId}" style="display: none;">
                <div class="series-detail-links-icons">
                    ${komgaLinkHtml}
                    ${bedethequeLinkHtml}
                </div>
            </div>
        </div>
    `;
}

// Menu ⚙️ Actions pour un tome manquant/placeholder (pas de fichier) - "les fichiers
// manquants aussi doivent avoir la molette avec les options MAJ, rechercher, ajouter un
// volume", pendant du menu ⚙️ d'un tome possédé (buildVolumeActionsGearHtml) mais réduit
// aux trois actions qui ont un sens sans fichier: rafraîchir les infos Bédéthèque déjà
// connues du placeholder, chercher une source, ou envoyer directement un fichier trouvé
// à la main. Avant, la seule façon de lancer la recherche pour ce tome était le badge
// "Non possédé" affiché dans la colonne Emplacement/le détail de la carte ("il y a pour
// remplacer l'existant mais pas ceux que je n'ai pas") - le badge reste cliquable en plus
// (pas retiré), ce menu ajoute juste un chemin cohérent avec les tomes possédés.
function buildVolumeMissingActionsGearHtml(v) {
    const volNum = v.volume_number ?? v.integral_number ?? v.hs_number ?? v.episode_number ?? 'null';
    // Même garde que le menu d'un tome possédé: n'a de sens que si la série est déjà
    // matchée sur Bédéthèque (voir _ensure_bedetheque_match, jamais de recherche
    // automatique sans confirmation).
    const updateMetadataItemHtml = (currentSeriesDetail && currentSeriesDetail.bedetheque && currentSeriesDetail.bedetheque.url)
        ? `<button type="button" class="toolbar-dropdown-item" onclick="updateVolumeMetadataFromBedetheque(${v.id}, event)">${svgIcon('tag')} MAJ de ce tome</button>`
        : '';
    const searchItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="searchMissingVolume(currentSeriesDetail.title, ${volNum}, {seriesId: currentSeriesDetail.id, isIntegral: ${!!v.is_integral}, isHs: ${!!v.is_hs}, isEpisode: ${!!v.is_episode}})">${svgIcon('radar')} Rechercher</button>`;
    // "ajouter aussi recherche automatique... dans la molette des volumes" - alternative
    // à "Rechercher" ci-dessus (voir runSeriesAutoAcquire, search-results-table.js).
    // "Recherche automatique est explicitement désactivé pour ces types dans l'UI. oui
    // ajoute cela" - couvrait auparavant seulement les tomes numérotés classiques
    // (is_integral/is_hs/is_episode excluaient le bouton), route Flask corrigée pour
    // transmettre le bon label ("Intégrale N"/"HS N"/"Épisode N") à
    // _confirms_requested_volume plutôt que de laisser un numéro seul se faire rejeter à
    // tort comme "ce n'est pas le tome N demandé".
    const autoAcquireItemHtml = (volNum !== 'null')
        ? `<button type="button" class="toolbar-dropdown-item" onclick="runSeriesAutoAcquire(currentSeriesDetail.id, {volumeNumber: ${volNum}, isIntegral: ${!!v.is_integral}, isHs: ${!!v.is_hs}, isEpisode: ${!!v.is_episode}, seriesTitle: currentSeriesDetail.title, buttonEl: this})">${svgIcon('radar')} Recherche automatique</button>`
        : '';
    // Numéro déjà connu du placeholder (créé depuis Bédéthèque) transmis d'avance à
    // triggerSeriesFileUpload, comme pour un tome possédé - "Ajouter un fichier" plutôt
    // que "Remplacer" puisqu'il n'y a justement rien à remplacer.
    const uploadFileItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="triggerSeriesFileUpload(currentSeriesDetail.id, {volume: ${v.volume_number ?? 'null'}, is_integral: ${!!v.is_integral}, integral_number: ${v.integral_number ?? 'null'}, is_hs: ${!!v.is_hs}, hs_number: ${v.hs_number ?? 'null'}, is_episode: ${!!v.is_episode}, episode_number: ${v.episode_number ?? 'null'}})">${svgIcon('upload')} Ajouter un fichier</button>`;
    // "depuis les volumes d'une serie ajoute dans action supprimer le volume" - retire ce
    // tome placeholder de la liste de suivi (rien à effacer sur le disque, voir
    // delete_volume côté Flask qui gère déjà ce cas filepath NULL), même action que celle
    // déjà utilisée par la modale d'édition manuelle (deleteVolume).
    const missingVolumeLabel = `Tome ${v.volume_number != null ? v.volume_number : '?'}`;
    const deleteItemHtml = `<button type="button" class="toolbar-dropdown-item toolbar-dropdown-item-danger" onclick="deleteVolume(${v.id}, currentSeriesDetail.id, '${escapeForAttribute(missingVolumeLabel)}', true)">${svgIcon('trash-2')} Retirer ce tome</button>`;
    const menuId = `volume-missing-actions-menu-${v.id}`;
    return `
        <div class="toolbar-dropdown">
            <button type="button" class="volume-link-icon" id="volume-missing-actions-gear-${v.id}" onclick="toggleToolbarDropdown('${menuId}', this)" title="Actions sur ce tome">${svgIcon('settings')}</button>
            <div class="toolbar-dropdown-menu toolbar-dropdown-menu-autoclose" id="${menuId}" style="display: none;">
                ${updateMetadataItemHtml}
                ${searchItemHtml}
                ${autoAcquireItemHtml}
                ${uploadFileItemHtml}
                ${deleteItemHtml}
            </div>
        </div>
    `;
}

// Menu ⚙️ Actions seul (MAJ métadonnées/conversion/renommage/édition/recherche), sans le
// bouton 🔗 Liens - extrait pour la vue Tableau des tomes (voir buildVolumeTableRowHtml)
// dont la colonne "Actions" ne doit contenir que de vraies actions, pas un lien de
// consultation externe (voir buildVolumeActionIconsHtml pour la version combinée liens+
// actions utilisée par la carte).
function buildVolumeActionsGearHtml(v) {
    const ci = v.comicinfo || {};

    const updateMetadataItemHtml = (currentSeriesDetail && currentSeriesDetail.bedetheque && currentSeriesDetail.bedetheque.url)
        ? `<button type="button" class="toolbar-dropdown-item" onclick="updateVolumeMetadataFromBedetheque(${v.id}, event)">${svgIcon('tag')} MAJ de ce tome</button>`
        : '';
    // Convertisseur ->cbz, affiché sur les tomes cbr/pdf/zip nu (la mise à jour des
    // métadonnées ci-dessus ne fonctionne que sur du cbz) - action indépendante, pas
    // mélangée à la mise à jour des métadonnées. "regarde s'il y a une option dans la
    // molette des volumes pour convertir vers cbz" - n'existait jusqu'ici que pour le
    // cbr (voir convert-cbr côté Flask), étendu à pdf/zip (convert-pdf/convert-zip).
    const _convertibleFormat = (v.format || '').toLowerCase();
    const convertCbrItemHtml = ['cbr', 'pdf', 'zip'].includes(_convertibleFormat)
        ? `<button type="button" class="toolbar-dropdown-item" onclick="convertVolumeToCbz(${v.id}, event, '${_convertibleFormat}')">${svgIcon('package')} Convertir en cbz</button>`
        : '';
    // Point d'entrée direct vers l'édition manuelle de CE tome (voir
    // openManualEditModal): sans ça, une série non matchée sur Bédéthèque (donc sans
    // "MAJ de ce tome", masqué ci-dessus) et un tome cbz (donc sans "Convertir en cbz")
    // se retrouvait avec un menu ⚙️ vide - aucun chemin direct vers l'édition d'un tome
    // précis sans passer par le menu "Actions" de la série puis chercher la bonne ligne
    // dans la longue liste de tomes de la modale.
    const editManuallyItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="openManualEditModal(currentSeriesDetail.id, ${v.id})">${svgIcon('pencil')} Éditer ce tome</button>`;
    const searchAgainNumber = v.volume_number ?? v.integral_number ?? v.hs_number ?? v.episode_number ?? 'null';
    const searchAgainItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="searchMissingVolume(currentSeriesDetail.title, ${searchAgainNumber}, {seriesId: currentSeriesDetail.id, isIntegral: ${!!v.is_integral}, isHs: ${!!v.is_hs}, isEpisode: ${!!v.is_episode}, currentVolumeId: ${v.id}})">${svgIcon('radar')} Rechercher un remplacement</button>`;
    // "ajouter aussi recherche automatique... dans la molette des volumes" - même
    // correctif que buildVolumeMissingActionsGearHtml (voir son commentaire): plus limité
    // aux tomes numérotés classiques.
    const autoAcquireAgainItemHtml = (searchAgainNumber !== 'null')
        ? `<button type="button" class="toolbar-dropdown-item" onclick="runSeriesAutoAcquire(currentSeriesDetail.id, {volumeNumber: ${searchAgainNumber}, isIntegral: ${!!v.is_integral}, isHs: ${!!v.is_hs}, isEpisode: ${!!v.is_episode}, seriesTitle: currentSeriesDetail.title, buttonEl: this})">${svgIcon('radar')} Recherche automatique</button>`
        : '';
    // Envoi manuel d'un fichier directement pour CE tome, DÉJÀ possédé ("pour ajouter un
    // fichier c'est sur un volume manquant. pour un volume existant renomme a remplacer
    // le fichier") - le tome cible étant déjà connu (celui dont on ouvre le menu), le
    // numéro est transmis d'avance à triggerSeriesFileUpload pour sauter la confirmation
    // de tome qu'un envoi depuis le header (one-shot) doit sinon demander. Remplace le
    // fichier existant si le nouveau est meilleur (même comparaison qualité qu'un import
    // normal, is_better_volume) - utile pour glisser une meilleure source trouvée
    // manuellement sans repasser par le pipeline d'import classique. Pendant homologue
    // pour un tome MANQUANT (pas encore de fichier): buildVolumeMissingActionsGearHtml,
    // libellé "Ajouter un fichier" là où il n'y a rien à remplacer.
    const uploadFileItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="triggerSeriesFileUpload(currentSeriesDetail.id, {volume: ${v.volume_number ?? 'null'}, is_integral: ${!!v.is_integral}, integral_number: ${v.integral_number ?? 'null'}, is_hs: ${!!v.is_hs}, hs_number: ${v.hs_number ?? 'null'}})">${svgIcon('upload')} Remplacer le fichier</button>`;
    // "seuls #16 indique size 1B. ajoute un bouton actualiser" - relit taille/nombre de
    // pages depuis le fichier réel sur disque pour CE tome seul (voir refreshVolumeFromDisk),
    // sans passer par un scan complet de la série: une DB désynchronisée d'un fichier
    // corrigé après coup (re-téléchargé, remplacé manuellement...) n'a sinon aucun moyen
    // simple de se remettre à jour sans déclencher un scan pesant sur toute la série.
    const refreshItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="refreshVolumeFromDisk(${v.id}, event)">${svgIcon('refresh-cw')} Actualiser</button>`;
    // "ajoute une option dans la molette pour telecharger le fichier (pour le volume)" -
    // simple navigation vers la route de téléchargement (voir download_volume côté
    // Flask, Content-Disposition: attachment), pas un appel fetch: le navigateur gère
    // le téléchargement nativement sans quitter la page.
    const downloadItemHtml = `<button type="button" class="toolbar-dropdown-item" onclick="window.location.href='/api/volumes/${v.id}/download'">${svgIcon('download')} Télécharger le fichier</button>`;
    // "depuis les volumes d'une serie ajoute dans action supprimer le volume" - jusqu'ici
    // seule la modale d'édition manuelle permettait de supprimer un tome (voir
    // deleteVolume, réutilisée telle quelle ici) - il fallait l'ouvrir juste pour ça.
    const deleteVolumeLabel = v.filename || `Tome ${v.volume_number != null ? v.volume_number : '?'}`;
    const deleteItemHtml = `<button type="button" class="toolbar-dropdown-item toolbar-dropdown-item-danger" onclick="deleteVolume(${v.id}, currentSeriesDetail.id, '${escapeForAttribute(deleteVolumeLabel)}')">${svgIcon('trash-2')} Supprimer le fichier</button>`;
    // Actions regroupées derrière une icône molette (au lieu d'icônes séparées par tome):
    // même pattern que le menu "⚙️ Actions" de la toolbar série, avec positionnement
    // fixe ancré au bouton (voir toggleToolbarDropdown) pour ne pas être coupé par le
    // scroll de .content
    const menuId = `volume-actions-menu-${v.id}`;
    return `
        <div class="toolbar-dropdown">
            <button type="button" class="volume-link-icon" id="volume-actions-gear-${v.id}" onclick="toggleToolbarDropdown('${menuId}', this)" title="Actions sur ce tome">${svgIcon('settings')}</button>
            <div class="toolbar-dropdown-menu toolbar-dropdown-menu-autoclose" id="${menuId}" style="display: none;">
                ${updateMetadataItemHtml}
                ${convertCbrItemHtml}
                ${editManuallyItemHtml}
                ${searchAgainItemHtml}
                ${autoAcquireAgainItemHtml}
                ${uploadFileItemHtml}
                ${downloadItemHtml}
                ${refreshItemHtml}
                ${deleteItemHtml}
            </div>
        </div>
    `;
}

// Version combinée liens (🔗) + actions (⚙️), utilisée par la carte volume (vue Aperçu) -
// voir buildVolumeActionsGearHtml pour le détail des actions et buildVolumeLinksIconHtml
// pour le détail des liens.
function buildVolumeActionIconsHtml(v) {
    return `${buildVolumeLinksIconHtml(v)}${buildVolumeActionsGearHtml(v)}`;
}

function _bedethequeCodePrefix(v) {
    const ci = v.comicinfo || {};
    const title = ci.title || '';
    const m = title.match(/^([^.]{1,20}?)\.\s/);
    return m ? m[1].trim() : null;
}

function volumeNumberLabel(v, isOneshot) {
    // "au lieu de mettre ? dans les albums met just -" - même convention "—" que partout
    // ailleurs dans l'app pour une valeur inconnue/absente, plutôt qu'un "?" qui suggère
    // une erreur alors que c'est simplement un numéro pas encore trouvé.
    // "in valerian still tome 0 show -" - `v.volume_number || '—'` traitait un vrai tome 0
    // (existe sur Bédéthèque, ex. Valérian) exactement comme une valeur absente (0 est
    // falsy en JS) - `!= null` distingue enfin "vraiment absent" de "vaut zéro". Même
    // correctif pour integral_number/hs_number/episode_number ci-dessous, exposés au même
    // risque si l'un de ces types est un jour numéroté à partir de 0.
    let numberLabel = isOneshot ? 'OS' : (v.volume_number != null ? v.volume_number : '—');
    if (v.is_bis && v.volume_number != null) {
        // "13 bis is not special it is part of the album" - reste dans le groupe
        // "Volumes" (is_special=0, voir scanner.py/routes.py), badge affiché avec le
        // même numéro que le tome de base + le suffixe exact Bédéthèque ("13 Bis",
        // jamais renuméroté - "meme notations que dans bedetheque").
        numberLabel = `${v.volume_number} ${v.bis_suffix || ''}`.trim();
    } else if (v.is_integral) {
        numberLabel = _bedethequeCodePrefix(v) || (v.integral_number != null ? `INT ${v.integral_number}` : 'INT');
    } else if (v.is_hs) {
        numberLabel = _bedethequeCodePrefix(v) || (v.hs_number != null ? `HS ${v.hs_number}` : 'HS');
    } else if (v.is_episode) {
        numberLabel = v.episode_number != null ? `ÉP ${v.episode_number}` : 'ÉP';
    } else if (v.is_special && !isOneshot) {
        numberLabel = v.special_label || '—';
    }
    return numberLabel;
}

function _displayVolumeTitle(v) {
    const ci = v.comicinfo || {};
    let title = ci.title || '';
    if (title && (v.is_integral || v.is_hs || v.is_special)) {
        title = title.replace(/^[^.]{1,20}?\.\s*/, '');
    }
    return title;
}

// "not this should explain what COF is according to the list I mentioned before" - codes
// DOCUMENTÉS (éditions/tirages/provenance/collections particulières de la nomenclature
// BEL/Bédéthèque fournie par l'utilisateur), pas un dictionnaire de TOUS les codes
// rencontrés en pratique (135 recensés sur cette bibliothèque, la plupart des codes
// promotionnels ponctuels propres à un seul éditeur - "MBD05", "Quick1", "Lidl"... aucun
// vocabulaire officiel à traduire pour ceux-là, voir _parse_special_prefix, scraper.py).
// Clé = code SANS le suffixe numérique éventuel (comparé après avoir retiré les chiffres
// de fin, voir ci-dessous - "PIR1"/"PIR2"/"PIR3"/"PIR4" partagent tous "Collection
// Pirate", juste avec un numéro différent).
const SPECIAL_CODE_LABELS = {
    // Tirages
    TT: 'Tirage de Tête', TL: 'Tirage de Luxe', TS: 'Tirage Spécial', HC: 'Hors Commerce',
    NUM: 'Numéroté', SIGN: 'Signé',
    // "0 = tome 0 (préquel, pilote, album introductif...) HC = édition Hors Commerce de
    // ce tome" - clé composée (comme 'COF INT1' plus bas) plutôt que le seul "HC": "0 HC"
    // ne matche pas le pattern lettres+chiffres-finaux de _volumeNumberBadgeTooltip (un
    // chiffre EN TÊTE, pas à la fin), sans quoi HC seul ne serait jamais trouvé pour ce
    // code précis.
    '0 HC': 'Tome 0 (préquel/pilote/album introductif) - Hors Commerce',
    // Formats
    POC: 'Poche', POCHE: 'Poche', FAC: 'Fac-similé', COL: 'Couleur', MINI: 'Petit format', MAXI: 'Grand format',
    // Provenance
    FL: 'France Loisirs', COF: 'Coffret', PIR: 'Collection Pirate', CLUB: 'Édition Club', CANAL: 'Édition Canal BD',
    // Éditions spéciales
    ES: 'Édition spéciale', ANNIV: 'Anniversaire', VAR: 'Couverture variante', ALT: 'Couverture alternative',
    COLL: 'Collector', LUXE: 'Luxe',
    // Collections particulières
    SP: 'Service Presse', EO: 'Édition Originale', TLHC: 'Tirage Luxe Hors Commerce', TTHC: 'Tirage de Tête Hors Commerce',
    // Prépublication/formats de parution (codes réels observés sur cette bibliothèque,
    // fournis par l'utilisateur)
    PRE: 'Prépublication', HCOURTE: 'Histoire courte',
    // Partenariats magazine/enseigne, compilations, novelisations (codes réels observés
    // sur cette bibliothèque - "je sépare ce qui est certain, très probable et à
    // vérifier", niveaux de confiance élevés uniquement, le reste laissé sans traduction
    // plutôt que deviner - "je préfère ne pas inventer")
    MBD: 'Le Monde de la BD', QUICK: 'Édition promotionnelle Quick', QUICKBO: 'Bonus Quick',
    LIDL: 'Édition Lidl', LECLERC: 'Édition E.Leclerc', PUB: 'Édition publicitaire',
    'PUB QUICK': 'Publicitaire Quick', 'PUB CITR': 'Publicité Citroën',
    BO: 'Bonus', BOBD: 'Bonus BD', COMPIL: 'Compilation', BESTOF: 'Best Of',
    TOTAL: 'Édition "Total" (station-service)', ROMAN: 'Roman / Novélisation',
    CAT: 'Catalogue', JDR: 'Jeu de rôle', JEU: 'Jeu',
    'COF INT1': "Coffret contenant l'intégrale 1",
    TIMBRE: 'Timbre', TIMBRE1TL: 'Album avec timbre + tirage luxe', SUPP: 'Supplément',
    DP: 'Dossier de presse',
    '3D': 'Album 3D', ART: 'Artbook', CC: 'Coffret Collector', CDV: 'Cahier de vacances',
    FLD: 'Feuillet / Flyer de libraire (ou Fascicule Ludique Dérivé)', LIV: 'Livre',
    MR: 'Mini-récit', POR: 'Portfolio', SCO: 'Scolaire',
    TH: 'Tirage spécial / édition festival', FOL: 'Folio', GEO: 'Édition GEO',
    GP: 'Grands Personnages / Grand Public (selon la collection)', PARO: 'Parodie', PF: 'Petit Format',
};

function _volumeNumberBadgeTooltip(v) {
    if (!v.is_special) return '';
    const ci = v.comicinfo || {};
    const rawTitle = ci.title || '';
    const label = v.special_label || '';
    // "N&B" garde son "&" (pas un chiffre à retirer) - seul un suffixe numérique final
    // ("PIR1" -> "PIR" + "1") est isolé pour la recherche dans le dictionnaire.
    const m = label.match(/^([A-Za-zÀ-ÿ&]+)(\d*)$/);
    const codeBase = m ? m[1].toUpperCase() : label.toUpperCase();
    const codeNum = m ? m[2] : '';
    const known = SPECIAL_CODE_LABELS[codeBase] || (codeBase === 'R' && codeNum ? 'Recueil' : null);
    if (known) {
        return `${known}${codeNum ? ' ' + codeNum : ''} — ${rawTitle}`;
    }
    // Code non documenté: pas de traduction possible, le titre complet reste le seul
    // contexte disponible (déjà mieux que le code brut seul).
    return rawTitle;
}

function _volumeTitleSuffix(title, seriesTitle) {
    if (!title) return '';
    if (seriesTitle && normalizeForSearch(title.trim()) === normalizeForSearch(seriesTitle.trim())) return '';
    return ` - ${title}`;
}

// Label "Tome N - Titre" (titre omis s'il n'est pas encore connu localement) pour le
// toast de MAJ métadonnées d'un tome seul - même format "Tome N - Titre" que celui déjà
// affiché par le toast de MAJ "série + tomes" (voir pollMetadataWriteProgress), pour que
// les deux affichages restent cohérents entre eux.
function volumeProgressLabel(v, isOneshot) {
    if (!v) return null;
    const ci = v.comicinfo || {};
    const label = `Tome ${volumeNumberLabel(v, isOneshot)}`;
    return ci.title ? `${label} - ${ci.title}` : label;
}

// Construit une carte volume, en privilégiant les métadonnées ComicInfo.xml (embarquées
// dans le cbz/cbr par Komga ou un autre outil de tag) quand elles sont disponibles,
// avec repli sur les infos extraites du nom de fichier.
//
// Un tome "placeholder" (ajouté depuis Bédéthèque, sans fichier - v.filepath NULL, voir
// add_series_from_bedetheque côté Flask) a sa propre carte, plus simple: couverture/titre
// Bédéthèque déjà en base, pas de menu d'actions fichier (rien à convertir/renommer/
// éditer), un bouton "Chercher une source" à la place - fusionné directement dans cette
// liste plutôt que dans une grille "volumes manquants" séparée, pour que tous les tomes
// (possédés ou non) apparaissent triés ensemble au même endroit.
function buildVolumeItemHtml(v, isOneshot, seriesTitle, seriesId) {
    const ci = v.comicinfo || {};
    const coverHtml = v.cover_path
        ? `<img class="volume-cover" src="/${v.cover_path}" alt="${escapeHtml(ci.title || v.filename || '')}" loading="lazy">`
        : `<div class="volume-cover volume-cover-placeholder">📖</div>`;

    const numberLabel = volumeNumberLabel(v, isOneshot);

    if (!v.filepath) {
        return `
            <div class="volume-item volume-item-placeholder">
                <div class="volume-number${v.is_integral ? ' volume-number-integral' : ''}${v.is_hs ? ' volume-number-hs' : ''}${v.is_episode ? ' volume-number-episode' : ''}${v.is_special ? ' volume-number-special' : ''}"${v.is_special ? ` data-tooltip="${escapeHtml(_volumeNumberBadgeTooltip(v))}"` : ''}>${numberLabel}</div>
                <div class="volume-cover-wrapper">
                    ${coverHtml}
                </div>
                <div class="volume-details">
                    <div class="volume-filename-row">
                        <div class="volume-filename">${escapeHtml(_displayVolumeTitle(v) || `Tome ${numberLabel}`)}</div>
                        ${buildVolumeLinksIconHtml(v)}${buildVolumeMissingActionsGearHtml(v)}
                    </div>
                    <div class="volume-file-detail">
                        <span class="badge badge-missing volume-missing-badge-clickable"
                              data-series-id="${seriesId || ''}"
                              data-series-title="${encodeURIComponent(seriesTitle || '')}"
                              data-volume-number="${v.volume_number != null ? v.volume_number : (v.integral_number != null ? v.integral_number : (v.hs_number != null ? v.hs_number : (v.episode_number != null ? v.episode_number : '')))}"
                              data-is-integral="${v.is_integral ? '1' : ''}"
                              data-is-hs="${v.is_hs ? '1' : ''}"
                              data-is-episode="${v.is_episode ? '1' : ''}"
                              title="Rechercher ce tome sur EBDZ/Prowlarr">${svgIcon('search')} Non possédé</span>
                        ${ci.year ? ` 📅 ${escapeHtml(String(ci.year))}` : ''}
                    </div>
                    <div class="volume-meta">
                        ${ci.writer ? `<span class="badge">✍️ ${escapeHtml(ci.writer)}</span>` : ''}
                        ${ci.publisher ? `<span class="badge">🏢 ${escapeHtml(ci.publisher)}</span>` : ''}
                        ${ci.genre ? `<span class="badge">🏷️ ${escapeHtml(ci.genre)}</span>` : ''}
                    </div>
                    ${buildTruncatedHtml(ci.summary, 200, 'volume-summary')}
                </div>
            </div>
        `;
    }

    // "dans la bibliothèque il n'y a pas moyen de sélectionner plusieurs volumes pour les
    // actualiser / supprimer etc" - la sélection multiple + barre d'actions groupées
    // n'existait jusqu'ici qu'en vue TABLEAU (selectedVolumeIds/toggleVolumeTableRowSelection/
    // updateVolumeBulkActionsBar, voir plus bas) ; réutilisée telle quelle ici plutôt qu'un
    // second mécanisme de sélection - la case en surimpression du badge numéro (position
    // relative posée en inline, la grille CSS de .volume-item ne prévoit pas de 4e colonne)
    // coche/décoche le même id dans le même Set, la barre d'actions groupées répond donc
    // déjà quelle que soit la vue active.
    const cardCheckboxHtml = `<input type="checkbox" class="volume-table-row-checkbox" data-volume-id="${v.id}" ${selectedVolumeIds.has(v.id) ? 'checked' : ''} onchange="toggleVolumeTableRowSelection(${v.id}, this.checked)" style="position:absolute; top:-8px; left:-8px; width:16px; height:16px; cursor:pointer; margin:0;" title="Sélectionner" onclick="event.stopPropagation()">`;

    return `
        <div class="volume-item">
            <div class="volume-number${v.is_integral ? ' volume-number-integral' : ''}${v.is_hs ? ' volume-number-hs' : ''}${v.is_episode ? ' volume-number-episode' : ''}${v.is_special ? ' volume-number-special' : ''}" style="position:relative;"${v.is_special ? ` data-tooltip="${escapeHtml(_volumeNumberBadgeTooltip(v))}"` : ''}>
                ${cardCheckboxHtml}
                ${numberLabel}
            </div>
            <div class="volume-cover-wrapper">
                ${coverHtml}
            </div>
            <div class="volume-details">
                <div class="volume-filename-row">
                    <div class="volume-filename">${escapeHtml(_displayVolumeTitle(v) || v.filename)}</div>
                    ${buildVolumeActionIconsHtml(v)}
                </div>
                ${ci.title ? `<div class="volume-file-detail">${escapeHtml(v.filename)}</div>` : ''}
                <div class="volume-file-detail">
                    ${buildVolumeFileDetailHtml(v)}
                </div>
                <div class="volume-meta">
                    ${buildVolumeMetaBadgesHtml(v)}
                </div>
                ${buildTruncatedHtml(ci.summary, 200, 'volume-summary')}
            </div>
        </div>
    `;
}

function setVolumesViewMode(mode) {
    volumesViewMode = mode;
    localStorage.setItem('volumesViewMode', mode);
    // Re-rendu local depuis les données déjà en mémoire (currentSeriesDetail) - pas de
    // fetch réseau ni de reconstruction du header/toolbar (voir buildVolumesSectionHtml):
    // avant, basculer de vue rappelait renderSeriesDetail au complet, perceptible comme
    // un rechargement de page (spinner, position de scroll perdue...) pour un simple
    // changement d'affichage local.
    const section = document.getElementById('series-volumes-section');
    if (currentSeriesDetail && section) {
        cleanupDetachedDropdownMenus();
        section.innerHTML = buildVolumesSectionHtml(currentSeriesDetail);
        renderVolumeTableColumnsMenu();
        initClearableSearchInputs(section);
    }
}

// Tri de la vue tableau au clic sur un en-tête de colonne (voir buildVolumesTableHtml):
// même colonne recliquée -> inverse le sens, nouvelle colonne -> tri ascendant. Re-rendu
// local depuis currentSeriesDetail, même principe que setVolumesViewMode ci-dessus.
function setVolumesTableSort(column) {
    if (volumesTableSort.column === column) {
        volumesTableSort.direction = volumesTableSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        volumesTableSort.column = column;
        volumesTableSort.direction = 'asc';
    }
    const section = document.getElementById('series-volumes-section');
    if (currentSeriesDetail && section) {
        cleanupDetachedDropdownMenus();
        section.innerHTML = buildVolumesSectionHtml(currentSeriesDetail);
        renderVolumeTableColumnsMenu();
        initClearableSearchInputs(section);
    }
}

// Colonnes optionnelles de la vue tableau des tomes, désactivées par défaut - même
// principe que TABLE_OPTIONAL_COLUMNS (liste des séries, voir son commentaire) via un
// bouton "⚙️ Colonnes" ("je veux l'icône molette pour ajouter des entrées"), persistées
// dans localStorage. render(v) lit le ComicInfo du tome, pas de champ dédié en base pour
// ces valeurs (elles vivent uniquement dans le JSON comicinfo, voir CLAUDE.md).
// Date (année de publication) est un des 7 colonnes par défaut demandées explicitement
// ("numero, nom, date, format, taille, emplacement et actions") - pas ici.
const VOLUME_TABLE_OPTIONAL_COLUMNS = [
    { key: 'author', label: 'Auteur', icon: '✍️', header: 'Auteur', render: v => (v.comicinfo || {}).writer || v.author || '—' },
    { key: 'publisher', label: 'Éditeur', icon: '🏢', header: 'Éditeur', render: v => (v.comicinfo || {}).publisher || '—' },
    { key: 'genre', label: 'Genre', icon: '🏷️', header: 'Genre', render: v => (v.comicinfo || {}).genre || '—' },
    { key: 'pages', label: 'Pages', icon: '📄', header: 'Pages', filterType: 'text', render: v => v.page_count || '—' },
    { key: 'summary', label: 'Résumé', icon: '📝', header: 'Résumé', filterType: 'text', render: v => (v.comicinfo || {}).summary || '—' },
    // "parse les notes des volumes bedetheque pour recuperer la note / nb review. a
    // ajouter dans les metadatas et dans le tableau volume" - communityrating/
    // communityratingcount viennent du ComicInfo (CommunityRating/CommunityRatingCount,
    // voir build_comicinfo_fields côté comicinfo_writer.py), clés en minuscules comme
    // tout champ comicinfo (LibraryScanner.read_comicinfo). Texte brut volontairement
    // (pas de ⭐ ici): ce render() passe par le même pipeline que les autres colonnes
    // optionnelles, qui échappe la valeur retournée (voir le bug corrigé sur les
    // colonnes Matching EBDZ/Komga, CLAUDE.md) - un caractère plein comme "★" reste un
    // caractère de texte normal, contrairement à une balise HTML.
    { key: 'rating', label: 'Note Bédéthèque', icon: '⭐', header: 'Note', render: v => {
        const ci = v.comicinfo || {};
        if (!ci.communityrating) return '—';
        return `${ci.communityrating}/5${ci.communityratingcount ? ` (${ci.communityratingcount} votes)` : ''}`;
    } },
    // "pareil pour le tableau de série. pas de releaser ni qualité" - resolution/
    // release_group viennent directement de la colonne `volumes` (pas du ComicInfo,
    // contrairement aux colonnes ci-dessus), même champs déjà affichés en badge sur la
    // carte tome/le header one-shot (voir buildVolumeMetaBadgesHtml).
    { key: 'quality', label: 'Qualité', icon: '🖼️', header: 'Qualité', filterType: 'text', render: v => v.resolution || '—' },
    { key: 'releaser', label: 'Releaser', icon: '📀', header: 'Releaser', filterType: 'text', render: v => v.release_group || '—' },
];

// Valeur "Date" (année de publication) d'un tome - colonne par défaut de la vue tableau,
// factorisée ici puisque lue à la fois par buildVolumeTableRowHtml (affichage) et
// _volumeTableSortValue (tri).
function _volumeDateLabel(v) {
    return (v.comicinfo || {}).year || v.year || '—';
}

let visibleVolumeTableColumns = new Set(JSON.parse(localStorage.getItem('volumeTableVisibleColumns') || '[]'));

function toggleVolumeTableColumn(key) {
    if (visibleVolumeTableColumns.has(key)) {
        visibleVolumeTableColumns.delete(key);
    } else {
        visibleVolumeTableColumns.add(key);
    }
    localStorage.setItem('volumeTableVisibleColumns', JSON.stringify([...visibleVolumeTableColumns]));
    const section = document.getElementById('series-volumes-section');
    if (currentSeriesDetail && section) {
        cleanupDetachedDropdownMenus();
        section.innerHTML = buildVolumesSectionHtml(currentSeriesDetail);
        renderVolumeTableColumnsMenu();
        initClearableSearchInputs(section);
    }
}

// Reconstruit le contenu du menu "⚙️ Colonnes" - même logique que renderTableColumnsMenu
// (liste des séries): pas de classe autoclose, pour cocher/décocher plusieurs colonnes
// sans que le menu se referme à chaque clic.
function renderVolumeTableColumnsMenu() {
    const menu = document.getElementById('volume-table-columns-menu');
    if (!menu) return;
    menu.innerHTML = VOLUME_TABLE_OPTIONAL_COLUMNS.map(col => `
        <button class="toolbar-dropdown-item${visibleVolumeTableColumns.has(col.key) ? ' active' : ''}"
                onclick="toggleVolumeTableColumn('${col.key}')">${col.icon} ${col.label}</button>
    `).join('');
}

// Valeur comparable d'un tome pour une colonne triable donnée - null/'' pour une donnée
// absente (ex: taille/format d'un tome non possédé), toujours envoyée en fin de liste par
// _compareVolumesForSort quel que soit le sens du tri.
function _volumeTableSortValue(v, column) {
    const ci = v.comicinfo || {};
    switch (column) {
        case 'number':
            return v.volume_number ?? v.integral_number ?? v.hs_number ?? v.episode_number ?? null;
        case 'name':
            return (ci.title || v.filename || '').toLowerCase();
        case 'date': {
            const label = _volumeDateLabel(v);
            return label === '—' ? null : label;
        }
        case 'format':
            return (v.format || '').toLowerCase();
        case 'size':
            return v.file_size ?? null;
        case 'path':
            return (v.filepath || '').toLowerCase();
        default: {
            const col = VOLUME_TABLE_OPTIONAL_COLUMNS.find(c => c.key === column);
            return col ? col.render(v) : null;
        }
    }
}

function _volumeNumberSortTier(v) {
    if (v.volume_number !== null && v.volume_number !== undefined) return 0;
    if (v.is_integral) return 1;
    if (v.is_hs) return 2;
    if (v.is_episode) return 3;
    if (v.is_special) return 4;
    return 5;
}

function _compareVolumesForSort(a, b, column, direction) {
    if (column === 'number') {
        const tierA = _volumeNumberSortTier(a);
        const tierB = _volumeNumberSortTier(b);
        if (tierA !== tierB) return tierA - tierB;

        // "just order by name in each section" - une seule règle au sein d'une même
        // section (Tomes/Intégrales/HS/Épisodes/Spéciaux, voir _volumeNumberSortTier):
        // trier par le libellé affiché dans cette même colonne (volumeNumberLabel, ex.
        // "Tome 5"/"HS 2"/"HS"/"BO1") - `numeric: true` compare déjà les nombres qu'il
        // contient dans le bon ordre ("Tome 2" avant "Tome 10"), pas besoin de
        // distinguer le champ numérique propre à chaque type.
        const la = volumeNumberLabel(a);
        const lb = volumeNumberLabel(b);
        const cmp = String(la).localeCompare(String(lb), 'fr', { numeric: true, sensitivity: 'base' });
        return direction === 'asc' ? cmp : -cmp;
    }

    const va = _volumeTableSortValue(a, column);
    const vb = _volumeTableSortValue(b, column);
    const aEmpty = va === null || va === undefined || va === '';
    const bEmpty = vb === null || vb === undefined || vb === '';
    if (aEmpty && bEmpty) return 0;
    if (aEmpty) return 1;
    if (bEmpty) return -1;

    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    return direction === 'asc' ? cmp : -cmp;
}

// Vue "tableau" de la liste des tomes (nom/format/taille/emplacement), alternative à la
// vue "aperçu" en cartes (buildVolumeItemHtml) - même esprit que le bascule Aperçu/Tableau
// déjà existant pour la liste des séries sur la page bibliothèque (seriesViewMode), mais
// c'est un état/bascule séparé (volumesViewMode): les deux listes n'ont aucun rapport.
// Un tome "placeholder" (voir buildVolumeItemHtml) n'a ni format/taille/emplacement réels
// (rien sur disque) - ligne visuellement distincte (badge "Non possédé"), pas de menu
// ⚙️ Actions (rien à convertir/renommer/éditer sans fichier), juste le lien Bédéthèque
// s'il existe déjà (buildVolumeLinksIconHtml, même logique que la carte). Pour un tome
// possédé, la colonne "Actions" ne contient QUE le menu ⚙️ (buildVolumeActionsGearHtml) -
// "Non possédé" cliquable en vue tableau (recherche EBDZ/Prowlarr directement depuis le
// badge - référence currentSeriesDetail au clic plutôt que d'encoder le titre dans
// l'attribut, pas de risque de casser l'onclick sur un titre avec une apostrophe). Seule
// action de recherche pour un placeholder en vue tableau: l'icône 📡 séparée qui
// existait ici a été retirée, redondante avec ce badge déjà cliquable.
function buildVolumeTableMissingBadgeHtml(v) {
    const volNum = v.volume_number ?? v.integral_number ?? v.hs_number ?? v.episode_number ?? 'null';
    return `<span class="badge badge-missing" style="cursor:pointer;" onclick="event.stopPropagation(); searchMissingVolume(currentSeriesDetail.title, ${volNum}, {seriesId: currentSeriesDetail.id, isIntegral: ${!!v.is_integral}, isHs: ${!!v.is_hs}, isEpisode: ${!!v.is_episode}})" title="Rechercher ce tome sur EBDZ/Prowlarr">${svgIcon('search')} Non possédé</span>`;
}

// pas le bouton 🔗 Liens, qui n'est pas une action mais une consultation externe.
function buildVolumeTableRowHtml(v, isOneshot) {
    const ci = v.comicinfo || {};
    const numberLabel = volumeNumberLabel(v, isOneshot);
    const name = _displayVolumeTitle(v) || v.filename || `Tome ${numberLabel}`;
    const isPlaceholder = !v.filepath;
    const actionsHtml = isPlaceholder
        ? `${buildVolumeLinksIconHtml(v)}${buildVolumeMissingActionsGearHtml(v)}`
        : buildVolumeActionIconsHtml(v);

    // Colonnes optionnelles bornées en largeur (voir .series-table-optional-cell,
    // réutilisée ici) - même raison que pour la liste des séries: un résumé/genre un peu
    // long ne doit pas élargir tout le tableau au-delà de son conteneur.
    const optionalCellsHtml = VOLUME_TABLE_OPTIONAL_COLUMNS
        .filter(col => visibleVolumeTableColumns.has(col.key))
        .map(col => {
            const value = String(col.render(v));
            // Colonne "rating": cellule cliquable -> modale des avis (voir
            // openBedethequeReviewsModal). Le rendu de la colonne reste du texte échappé
            // (voir commentaire de VOLUME_TABLE_OPTIONAL_COLUMNS plus haut, pas de HTML
            // dans col.render) - le onclick est ajouté sur le <td> lui-même, pas dans la
            // valeur, donc aucun contournement de l'échappement.
            const clickable = col.key === 'rating' && value !== '—';
            const clickAttrs = clickable ? ` onclick="openBedethequeReviewsModal(${v.id})" style="cursor: pointer;"` : '';
            return `<td class="series-table-optional-cell" data-label="${escapeHtml(col.header)}" title="${escapeHtml(value)}"${clickAttrs}>${escapeHtml(value)}</td>`;
        }).join('');

    // Case à cocher pour la sélection multiple - absente sur un placeholder (pas encore
    // possédé): aucune des actions groupées (supprimer/convertir/éditer/MAJ métadonnées)
    // n'a de sens sur un tome sans fichier, voir buildVolumeActionsGearHtml/
    // buildVolumeMissingActionsGearHtml qui font déjà cette même distinction.
    const selectCellHtml = isPlaceholder
        ? '<td class="volume-table-select-cell"></td>'
        : `<td class="volume-table-select-cell"><input type="checkbox" class="volume-table-row-checkbox" data-volume-id="${v.id}" ${selectedVolumeIds.has(v.id) ? 'checked' : ''} onchange="toggleVolumeTableRowSelection(${v.id}, this.checked)"></td>`;

    // data-group + visibilité initiale posée directement dans le HTML (pas dans un second
    // passage après insertion): filterVolumesTableRows reste la SEULE fonction qui décide
    // ensuite de row.style.display (filtre de colonne ET groupe replié combinés, voir son
    // commentaire) - poser l'état initial ici évite un flash "tout visible" avant qu'un
    // premier filtre ne tourne.
    const groupKey = _volumeGroupKey(v, isOneshot);
    const groupHiddenStyle = _isVolumeGroupCollapsed(groupKey) ? ' style="display:none;"' : '';

    return `
        <tr class="volume-table-row${isPlaceholder ? ' volume-table-row-placeholder' : ' volume-table-row-owned'}" data-group="${groupKey}"${groupHiddenStyle}>
            ${selectCellHtml}
            <td${v.is_special ? ` data-tooltip="${escapeHtml(_volumeNumberBadgeTooltip(v))}"` : ''}>${numberLabel}</td>
            <td class="volume-table-name" title="${escapeHtml(name)}">${escapeHtml(name)}</td>
            <td style="white-space:nowrap;">${escapeHtml(String(_volumeDateLabel(v)))}</td>
            <td>${isPlaceholder ? '—' : escapeHtml((v.format || '?').toUpperCase())}</td>
            <td style="white-space:nowrap;" data-raw-size="${v.file_size || 0}">${isPlaceholder ? '—' : formatBytes(v.file_size)}</td>
            <td class="volume-table-path" title="${escapeHtml(v.filepath || '')}">${v.filepath ? escapeHtml(v.filepath) : buildVolumeTableMissingBadgeHtml(v)}</td>
            ${optionalCellsHtml}
            <td class="volume-table-actions"><div class="volume-table-actions-inner">${actionsHtml}</div></td>
        </tr>
    `;
}

// En-tête de colonne triable (voir setVolumesTableSort/_compareVolumesForSort): clic pour
// trier, avec flèche indiquant la colonne/le sens actifs. "Actions" n'a volontairement
// pas de tri, ce n'est pas une donnée du tome.
// filterCol/colIndex optionnels: fusionne le contrôle de filtre (voir
// _volumeTableFilterColumns) directement dans l'en-tête triable, sur la même ligne que
// le libellé plutôt qu'une <tr> de filtre séparée - même pattern/mêmes classes
// .th-filterable-* que _seriesTableHeaderHtml (liste des séries), voir son commentaire
// ("change toutes les filtre des tableaux avec le filter sur la meme ligne style hoover").
function _volumeTableHeaderHtml(column, label, filterCol, colIndex) {
    const active = volumesTableSort.column === column;
    const arrow = active ? (volumesTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    const filterHtml = filterCol ? `
        <span class="th-filterable-filter" onclick="event.stopPropagation()">
            <span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>
            ${filterCol.type === 'select'
                ? `<select id="volume-table-filter-${column}" aria-label="Filtrer par ${escapeHtml(label)}" class="volume-table-filter-select th-filterable-control" data-col-index="${colIndex}" onchange="_syncFilterControlActive(this); filterVolumesTableRows()">
                       <option value="">Tous</option>
                       ${filterCol.values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('')}
                   </select>`
                : filterCol.type === 'size-bucket'
                // "le fitre taille change pour une echelle de selection style moins de
                // 100 mb, etc" - une échelle fixe (SEARCH_SIZE_BUCKETS, déjà utilisée par
                // le tableau de résultats de recherche, voir search-results-table.js) au
                // lieu d'un texte libre comparé au libellé formaté ("12,3 Mo") de la
                // cellule, jamais pratique à taper à la main pour trouver une plage.
                ? `<select id="volume-table-filter-${column}" aria-label="Filtrer par ${escapeHtml(label)}" class="volume-table-filter-select th-filterable-control" data-col-index="${colIndex}" data-size-bucket="1" onchange="_syncFilterControlActive(this); filterVolumesTableRows()">
                       ${SEARCH_SIZE_BUCKETS.map(b => `<option value="${b.value}">${escapeHtml(b.label)}</option>`).join('')}
                   </select>`
                : `<input type="text" id="volume-table-filter-${column}" aria-label="Filtrer par ${escapeHtml(label)}" class="volume-table-filter-input th-filterable-control" data-col-index="${colIndex}"
                           placeholder="Filtrer..." oninput="_syncFilterControlActive(this); filterVolumesTableRows()">`}
        </span>
    ` : '';
    return `
        <th class="volume-table-sortable${active ? ' volume-table-sort-active' : ''}" onclick="setVolumesTableSort('${column}')">
            <div class="th-filterable-row">
                <span class="th-filterable-label">${label}${arrow}</span>
                ${filterHtml}
            </div>
        </th>
    `;
}

// Une entrée par colonne filtrable de la vue tableau des tomes, dans le même ordre que
// les <td> produits par buildVolumeTableRowHtml (colonne Actions exclue) - même principe
// que _tableFilterColumns pour la liste des séries (voir son commentaire), demandé
// explicitement pour que la vue tableau des tomes ait les mêmes filtres/options que celle
// de la bibliothèque.
function _volumeTableFilterColumns(volumes) {
    const optional = VOLUME_TABLE_OPTIONAL_COLUMNS.filter(col => visibleVolumeTableColumns.has(col.key));
    const cols = [
        { label: '#', type: 'text' },
        { label: 'Nom', type: 'text' },
        { label: 'Date', type: 'select', getValue: v => String(_volumeDateLabel(v)) },
        { label: 'Format', type: 'select', getValue: v => v.filepath ? (v.format || '?').toUpperCase() : '—' },
        { label: 'Taille', type: 'size-bucket' },
        { label: 'Emplacement', type: 'text' },
        ...optional.map(col => ({
            label: col.header,
            type: col.filterType || 'select',
            getValue: col.render,
        })),
    ];
    for (const col of cols) {
        if (col.type === 'select') {
            col.values = [...new Set(volumes.map(col.getValue))].sort((a, b) => a.localeCompare(b, 'fr'));
        }
    }
    return cols;
}

// Filtre la vue tableau des tomes colonne par colonne - même logique que
// filterSeriesTableRows (liste des séries): correspondance exacte pour un filtre
// "select", sous-chaîne insensible à la casse pour un filtre "text".
function filterVolumesTableRows() {
    const controls = document.querySelectorAll('.volume-table-filter-input, .volume-table-filter-select');
    const active = [...controls]
        .map(el => ({
            colIndex: parseInt(el.dataset.colIndex),
            isSelect: el.tagName === 'SELECT',
            isSizeBucket: el.dataset.sizeBucket === '1',
            value: el.value.trim(),
        }))
        .filter(f => f.value);

    document.querySelectorAll('.volume-table-row').forEach(row => {
        const matches = active.every(f => {
            const cell = row.children[f.colIndex];
            if (!cell) return false;
            // Échelle Taille (voir _volumeTableFilterColumns/_volumeTableHeaderHtml): la
            // valeur brute en octets vit dans data-raw-size, jamais dans le texte formaté
            // ("12,3 Mo") de la cellule, sur lequel _searchResultMatchesSizeBucket ne
            // pourrait rien comparer.
            if (f.isSizeBucket) return _searchResultMatchesSizeBucket({ size: Number(cell.dataset.rawSize || 0) }, f.value);
            const text = cell.textContent.trim();
            // "si j'ai un accent de type é ca trouve pas. vire les accents" - comparaison
            // insensible aux accents (voir normalizeForSearch), pas juste à la casse.
            return f.isSelect ? text === f.value : normalizeForSearch(text).includes(normalizeForSearch(f.value));
        });
        // Section repliable (voir toggleVolumeGroupSection): une ligne reste visible
        // seulement si elle correspond ET que son groupe n'est pas replié - seule fonction
        // qui décide de row.style.display, pour que filtre de colonnes et repli de section
        // composent correctement plutôt que de s'écraser l'un l'autre.
        row.style.display = (matches && !_isVolumeGroupCollapsed(row.dataset.group)) ? '' : 'none';
    });
}

function buildVolumesTableHtml(volumes, isOneshot) {
    // Reconstruction complète du tableau (nouvelle série, changement de filtre/vue/
    // colonnes) - la sélection précédente ne référence plus forcément les mêmes lignes,
    // voir le commentaire sur selectedVolumeIds.
    selectedVolumeIds.clear();

    const sortedVolumes = volumesTableSort.column
        ? [...volumes].sort((a, b) => _compareVolumesForSort(a, b, volumesTableSort.column, volumesTableSort.direction))
        : volumes;

    // Colonnes fixes + optionnelles, dans le même ordre que _volumeTableFilterColumns
    // (zippées par index ci-dessous) pour associer le bon filtre à chaque en-tête.
    // colIndex décalé de +1 (i + 1): row.children[0] est désormais la case à cocher, pas
    // la première colonne de données (voir filterVolumesTableRows, qui indexe row.children
    // par cet index pour retrouver le texte de la bonne cellule à filtrer).
    const fixedColumns = [
        { column: 'number', label: '#' },
        { column: 'name', label: 'Nom' },
        { column: 'date', label: 'Date' },
        { column: 'format', label: 'Format' },
        { column: 'size', label: 'Taille' },
        { column: 'path', label: 'Emplacement' },
    ];
    const optionalColumns = VOLUME_TABLE_OPTIONAL_COLUMNS
        .filter(col => visibleVolumeTableColumns.has(col.key))
        .map(col => ({ column: col.key, label: col.header }));

    const filterCols = _volumeTableFilterColumns(volumes);
    const headersHtml = [...fixedColumns, ...optionalColumns]
        .map((col, i) => _volumeTableHeaderHtml(col.column, col.label, filterCols[i], i + 1))
        .join('');

    // Sections repliables par type (voir _groupVolumesByType/toggleVolumeGroupSection) -
    // une <tr> d'en-tête par groupe (colspan sur toute la largeur), sautée si un seul type
    // est présent (cas courant, rien à distinguer). Le tri par colonne (sortedVolumes)
    // reste appliqué D'ABORD, _groupVolumesByType partitionne ensuite en préservant l'ordre
    // relatif au sein de chaque groupe.
    const totalColCount = 1 + fixedColumns.length + optionalColumns.length + 1;
    const bodyHtml = _groupVolumesByType(sortedVolumes, currentSeriesDetail && currentSeriesDetail.is_oneshot).map(g => {
        const rowsHtml = g.items.map(v => buildVolumeTableRowHtml(v, isOneshot)).join('');
        const collapsed = _isVolumeGroupCollapsed(g.key);
        return `
            <tr class="volume-group-header-row${collapsed ? ' volume-group-collapsed' : ''}" onclick="toggleVolumeGroupSection('${g.key}', this)">
                <td colspan="${totalColCount}">
                    <span class="volume-group-toggle">${svgIcon('chevron-down')}</span>
                    <strong>${escapeHtml(g.label)}</strong>
                    <span class="part-count">${_volumeGroupCountLabel(g.items)}</span>
                </td>
            </tr>
            ${rowsHtml}
        `;
    }).join('');

    return `
        <div class="volumes-table-wrapper">
            <table class="volumes-table">
                <thead>
                    <tr>
                        <th class="volume-table-select-cell"><input type="checkbox" id="volume-table-select-all" onchange="toggleAllVolumeTableSelection(this.checked)" title="Tout sélectionner (tomes visibles)"></th>
                        ${headersHtml}
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${bodyHtml}
                </tbody>
            </table>
        </div>
    `;
}

// Bascule Aperçu/Tableau pour la liste des tomes (voir volumesViewMode/setVolumesViewMode
// ci-dessus) - même habillage visuel que .view-switcher (liste des séries) pour rester
// cohérent, bouton actif en évidence.
// "dans les volumes ajoute un filtre existant / non existant" - pas persisté
// (contrairement à volumesViewMode): ouvrir une autre série avec un filtre "Manquants"
// oublié d'une visite précédente masquerait ses tomes possédés sans qu'on comprenne
// pourquoi, mieux vaut repartir de "Tout" à chaque fiche série.
let volumesOwnershipFilter = 'all'; // 'all' | 'owned' | 'missing'

// Sélection multiple en vue tableau des tomes ("en mode tableau add a checkbox pour
// selectionner plusieurs volumes et ajoute un fonction pour supprimer / convertir / edit
// / MAJ ... pour la selection") - un Set d'ids de tomes RÉELS (jamais un placeholder,
// voir buildVolumeTableRowHtml: aucune des 4 actions groupées n'a de sens sur un tome pas
// encore possédé). Vidé à chaque reconstruction du tableau (nouvelle série, changement de
// filtre/vue) plutôt que persisté, même raisonnement que volumesOwnershipFilter ci-dessus.
let selectedVolumeIds = new Set();

function buildVolumesViewSwitcherHtml() {
    // Bouton "⚙️ Colonnes" (voir VOLUME_TABLE_OPTIONAL_COLUMNS/toggleVolumeTableColumn) -
    // uniquement affiché en vue tableau, rien à configurer pour la vue aperçu en cartes.
    const columnsButtonHtml = volumesViewMode === 'table' ? `
            <div class="toolbar-dropdown" id="volume-table-columns-section">
                <button class="toolbar-btn toolbar-btn-icon-only" id="volume-table-columns-btn" onclick="toggleToolbarDropdown('volume-table-columns-menu', this)" title="Colonnes du tableau">
                    <span class="toolbar-btn-icon">${svgIcon('settings')}</span>
                </button>
                <div class="toolbar-dropdown-menu toolbar-dropdown-menu-right" id="volume-table-columns-menu" style="display: none;"></div>
            </div>` : '';
    return `
        <div class="view-switcher" style="margin-bottom: 12px;">
            <button class="view-switch-btn${volumesViewMode === 'overview' ? ' active' : ''}" onclick="setVolumesViewMode('overview')" title="Vue aperçu">${svgIcon('list')} Aperçu</button>
            <button class="view-switch-btn${volumesViewMode === 'table' ? ' active' : ''}" onclick="setVolumesViewMode('table')" title="Vue tableau">${svgIcon('table')} Tableau</button>
            ${columnsButtonHtml}
            <span style="width:1px; align-self:stretch; background:var(--color-border); margin:0 4px;"></span>
            <button class="view-switch-btn view-switch-btn-icon-only${volumesOwnershipFilter === 'all' ? ' active' : ''}" onclick="setVolumesOwnershipFilter('all')" title="Tous les tomes">${svgIcon('layout-list')}</button>
            <button class="view-switch-btn view-switch-btn-icon-only${volumesOwnershipFilter === 'owned' ? ' active' : ''}" onclick="setVolumesOwnershipFilter('owned')" title="Seulement les tomes déjà sur disque">${svgIcon('check-check')}</button>
            <button class="view-switch-btn view-switch-btn-icon-only${volumesOwnershipFilter === 'missing' ? ' active' : ''}" onclick="setVolumesOwnershipFilter('missing')" title="Seulement les tomes manquants">${svgIcon('x')}</button>
        </div>
    `;
}

function setVolumesOwnershipFilter(filter) {
    volumesOwnershipFilter = filter;
    // Même principe que setVolumesViewMode: re-rendu local depuis currentSeriesDetail,
    // pas de fetch réseau.
    const section = document.getElementById('series-volumes-section');
    if (currentSeriesDetail && section) {
        cleanupDetachedDropdownMenus();
        section.innerHTML = buildVolumesSectionHtml(currentSeriesDetail);
        renderVolumeTableColumnsMenu();
        initClearableSearchInputs(section);
    }
}

// Bascule (switcher) + liste des tomes, dans le mode courant (volumesViewMode) - extrait
// de renderSeriesDetail pour être réutilisable par setVolumesViewMode SANS refaire un
// fetch réseau ni reconstruire header/toolbar: changer juste la vue des tomes n'a besoin
// d'aucune donnée nouvelle, currentSeriesDetail (déjà en mémoire) suffit. Avant cette
// extraction, changer de vue rappelait renderSeriesDetail au complet - un aller-retour
// réseau et un re-rendu de toute la fiche pour changer un simple affichage local.
// "dans les volumes ajoute un filtre existant / non existant" - un tome "existant" a un
// fichier (v.filepath non nul), un "non existant" est un placeholder Bédéthèque sans
// fichier (voir buildVolumeItemHtml). Filtre appliqué ici, en amont de toutes les vues
// (tableau/parties/liste simple), pour qu'elles restent cohérentes entre elles sans
// dupliquer la condition dans chacune.
function _matchesVolumesOwnershipFilter(v) {
    if (volumesOwnershipFilter === 'owned') return !!v.filepath;
    if (volumesOwnershipFilter === 'missing') return !v.filepath;
    return true;
}

// "peux tu mettre un separateur entre volume normal et special / integrales / HS... le
// separateur peut close chaque section... fait la meme chose pour aperçu" - regroupement
// par TYPE de tome (à ne pas confondre avec data.parts, un regroupement par arc narratif
// indépendant), en sections repliables individuellement, appliqué aux deux vues (Aperçu
// ET Tableau). Ordre volontairement "normal d'abord": c'est ce qu'on vient chercher en
// premier sur une fiche série, les à-côtés (HS/intégrales/épisodes) ensuite.
// "il y a un volume COF. ce n'est pas un volume, c'est un spécial... same for everything
// that is not a volume, no all in volumes as there are not" - dernière section, fourre-
// tout pour tout ce qui n'est ni un tome classique ni INT/HS/Épisode (coffrets, tirages,
// rééditions promotionnelles... voir _parse_special_prefix, blueprints/bedetheque/
// scraper.py - pas de vocabulaire fermé possible côté Bédéthèque).
const VOLUME_GROUP_ORDER = ['volume', 'episode', 'integral', 'hs', 'special'];
const VOLUME_GROUP_LABELS = { volume: 'Volumes', integral: 'Intégrales', hs: 'Hors-série', episode: 'Épisodes', special: 'Spéciaux' };
// "interesting to have a tooltip for the speciaux sections to understand what it is" -
// seule "Spéciaux" en a besoin (les 3 autres sont déjà explicites) : coffrets, tirages,
// rééditions promotionnelles... Bédéthèque n'a aucun vocabulaire fermé pour ces codes,
// voir _parse_special_prefix (blueprints/bedetheque/scraper.py).
const VOLUME_GROUP_TOOLTIPS = {
    special: 'Coffrets, tirages spéciaux, rééditions promotionnelles, recueils... tout ce qui n\'est ni un tome classique, ni une intégrale/hors-série/épisode. Le code affiché (ex. "COF", "TT") vient directement de Bédéthèque.'
};

function _volumeGroupKey(v, isOneshot = false) {
    // Un one-shot reste un volume dans l'interface, même si une ancienne ligne
    // de base porte encore is_special=1.
    if (isOneshot && !v.is_integral && !v.is_hs && !v.is_episode) return 'volume';
    if (v.is_integral) return 'integral';
    if (v.is_hs) return 'hs';
    if (v.is_episode) return 'episode';
    if (v.is_special) return 'special';
    // Un album non numéroté mais non marqué spécial reste un volume (notamment un
    // one-shot dont la série a ensuite été repassée en mode normal). La catégorie
    // "Spéciaux" est réservée aux lignes explicitement marquées is_special=1.
    return 'volume';
}

// Un seul tome (série sans HS/intégrale du tout, cas de très loin le plus courant): pas
// la peine d'afficher un unique en-tête "Volumes" qui ne sépare jamais rien - les sections
// ne servent qu'à distinguer plusieurs types coexistants.
function _groupVolumesByType(volumes, isOneshot = false) {
    const buckets = {};
    for (const v of volumes) {
        const key = _volumeGroupKey(v, isOneshot);
        (buckets[key] = buckets[key] || []).push(v);
    }
    // "quand il n'y a que des volumes et pas d'intégrales il n'y a pas de groupes
    // volumes... ajoute le" - l'en-tête "Volumes" (avec son compte possédé/total) reste
    // utile même seul, pas la peine d'être multi-type pour le justifier.
    return VOLUME_GROUP_ORDER.filter(key => buckets[key] && buckets[key].length)
        .map(key => ({ key, label: VOLUME_GROUP_LABELS[key], items: buckets[key] }));
}

// "met le nombre de volumes possédés / total" - le total seul ne dit pas combien
// manquent réellement dans CETTE section (un placeholder Bédéthèque non possédé compte
// pareil qu'un tome réel dans g.items.length).
function _volumeGroupCountLabel(items) {
    const owned = items.filter(v => v.filepath).length;
    return `${owned} / ${items.length}`;
}

// Replié/déplié persisté (localStorage, même mécanique que les groupes de la sidebar -
// voir nav.js) - propre à chaque type, PAS à la série: "Hors-série" replié une fois reste
// replié en ouvrant une autre fiche série, cohérent avec le fait que ce sont les mêmes 4
// types partout dans l'app.
function _isVolumeGroupCollapsed(key) {
    try { return localStorage.getItem('volumeGroupCollapsed_' + key) === 'true'; } catch (e) { return false; }
}

function toggleVolumeGroupSection(key, headerEl) {
    const collapsed = !_isVolumeGroupCollapsed(key);
    try { localStorage.setItem('volumeGroupCollapsed_' + key, String(collapsed)); } catch (e) { /* ignore */ }

    // Aperçu: la classe vit sur le wrapper englobant (.volume-group-section), dont le CSS
    // masque la .volume-list ET fait pivoter le chevron par sélecteur descendant. Tableau:
    // pas de wrapper autour des <tr> (une <tr> d'en-tête ne peut pas en contenir d'autres) -
    // la classe est donc posée directement sur headerEl (la <tr> cliquée elle-même) pour
    // que son propre chevron pivote ; ses lignes de données, elles, sont masquées à part
    // via filterVolumesTableRows ci-dessous (seule fonction qui décide de row.style.display,
    // pour ne jamais laisser ce mécanisme et le filtre de colonnes se marcher dessus).
    const section = headerEl.closest('.volume-group-section');
    (section || headerEl).classList.toggle('volume-group-collapsed', collapsed);

    if (typeof filterVolumesTableRows === 'function' && document.querySelector('.volume-table-row')) {
        filterVolumesTableRows();
    }
}

// Une section repliable (vue Aperçu) - même habillage visuel que .part-section/.part-header
// (regroupement par arc narratif, déjà existant) pour rester cohérent, avec en plus le
// repli/dépli (voir toggleVolumeGroupSection).
function _volumeGroupSectionHtml(g, data) {
    const collapsed = _isVolumeGroupCollapsed(g.key);
    return `
        <div class="volume-group-section part-section${collapsed ? ' volume-group-collapsed' : ''}">
            <div class="part-header volume-group-header" onclick="toggleVolumeGroupSection('${g.key}', this)"${VOLUME_GROUP_TOOLTIPS[g.key] ? ` data-tooltip="${escapeHtml(VOLUME_GROUP_TOOLTIPS[g.key])}"` : ''}>
                <span class="volume-group-title">
                    <span class="volume-group-toggle">${svgIcon('chevron-down')}</span>
                    <h3>${escapeHtml(g.label)}</h3>
                </span>
                <span class="part-count">${_volumeGroupCountLabel(g.items)}</span>
            </div>
            <div class="volume-list">
                ${g.items.map(v => buildVolumeItemHtml(v, data.is_oneshot, data.title, data.id)).join('')}
            </div>
        </div>
    `;
}

function buildVolumesSectionHtml(data) {
    let volumesHtml = '';
    let volumesViewSwitcherHtml = '';

    const filteredVolumes = (data.volumes || []).filter(_matchesVolumesOwnershipFilter);

    // Vue "tableau" (nom/format/taille/emplacement): à plat, ignore volontairement le
    // regroupement par partie (data.parts) - un tableau reste lisible sans sections,
    // contrairement à la vue aperçu où le regroupement aide à s'y retrouver visuellement
    if (!data.is_oneshot && (data.volumes || []).length > 0) {
        volumesViewSwitcherHtml = buildVolumesViewSwitcherHtml();
    }

    // Barre d'actions groupées, repliée (display:none) tant qu'aucun tome n'est
    // sélectionné - voir updateVolumeBulkActionsBar/toggleVolumeTableRowSelection. Existe
    // maintenant dans les deux vues ("dans la bibliothèque il n'y a pas moyen de
    // sélectionner plusieurs volumes... vue aperçu" - la case à cocher en vue cartes,
    // voir buildVolumeItemHtml, alimente le même Set selectedVolumeIds que la vue tableau).
    let volumeBulkActionsBarHtml = (!data.is_oneshot && (data.volumes || []).length > 0)
        ? `<div id="volume-bulk-actions-bar" class="volume-bulk-actions-bar" style="display:none;"></div>`
        : '';

    if (!data.is_oneshot && volumesViewMode === 'table' && (data.volumes || []).length > 0) {
        volumesHtml = buildVolumesTableHtml(filteredVolumes, data.is_oneshot);
    } else if (!data.is_oneshot && data.has_parts && data.parts) {
        const partNumbers = Object.keys(data.parts).sort((a, b) => parseInt(a) - parseInt(b));

        if (partNumbers.length > 0) {
            volumesHtml = partNumbers.map(partNum => {
                const part = data.parts[partNum];
                const partVolumes = part.volumes.filter(_matchesVolumesOwnershipFilter);
                if (partVolumes.length === 0) return '';
                return `
                    <div class="part-section">
                        <div class="part-header">
                            <h3>📖 ${escapeHtml(part.name)}</h3>
                            <span class="part-count">${partVolumes.length} ${pluralize(partVolumes.length, 'volume')}</span>
                        </div>
                        <div class="volume-list">
                            ${partVolumes.map(v => buildVolumeItemHtml(v, data.is_oneshot, data.title, data.id)).join('')}
                        </div>
                    </div>
                `;
            }).join('');
        }
    }

    // Si pas de parties ou pas de volumes (et pas un one-shot), afficher la liste
    // simple. Un one-shot n'affiche pas de carte volume (un seul fichier, déjà
    // représenté par la couverture/le titre en haut): ses tags/auteur/genre sont
    // affichés directement dans le header (voir oneshotMetaHtml plus bas) plutôt
    // que dans une carte qui redirait le nom de fichier/la couverture
    if (!volumesHtml && !data.is_oneshot) {
        volumesHtml = _groupVolumesByType(filteredVolumes, data.is_oneshot).map(g => _volumeGroupSectionHtml(g, data)).join('');
    }

    if (!volumesHtml && !data.is_oneshot && (data.volumes || []).length > 0) {
        volumesHtml = '<p class="help-text" style="padding:12px 0;">Aucun tome ne correspond à ce filtre.</p>';
    }

    return `${volumesViewSwitcherHtml}${volumeBulkActionsBarHtml}${volumesHtml}`;
}

// ===== SÉLECTION MULTIPLE / ACTIONS GROUPÉES - VUE TABLEAU DES TOMES =====
// "en mode tableau add a checkbox pour selectionner plusieurs volumes et ajoute un
// fonction pour supprimer / convertir / edit / MAJ ... pour la selection". "Editer" ne
// fusionne pas les champs de plusieurs tomes différents dans un seul formulaire (la
// modale d'édition manuelle reste volontairement centrée sur un seul tome à la fois,
// voir CLAUDE.md) - plutôt un parcours un par un avec un bouton "Suivant" (voir
// startBulkEditQueue/bulkEditNext, renderManualEditModalSingleVolume).

function toggleVolumeTableRowSelection(volumeId, checked) {
    if (checked) selectedVolumeIds.add(volumeId); else selectedVolumeIds.delete(volumeId);
    updateVolumeBulkActionsBar();
}

// "Tout sélectionner": seulement les lignes actuellement VISIBLES (respecte un filtre de
// colonne déjà actif, voir filterVolumesTableRows) - sélectionner en plus des lignes
// masquées par le filtre courant surprendrait plus qu'autre chose.
function toggleAllVolumeTableSelection(checked) {
    document.querySelectorAll('.volume-table-row').forEach(row => {
        if (row.style.display === 'none') return;
        const checkbox = row.querySelector('.volume-table-row-checkbox');
        if (!checkbox) return;
        checkbox.checked = checked;
        const id = parseInt(checkbox.dataset.volumeId, 10);
        if (checked) selectedVolumeIds.add(id); else selectedVolumeIds.delete(id);
    });
    updateVolumeBulkActionsBar();
}

function clearVolumeTableSelection() {
    selectedVolumeIds.clear();
    document.querySelectorAll('.volume-table-row-checkbox').forEach(cb => { cb.checked = false; });
    const selectAll = document.getElementById('volume-table-select-all');
    if (selectAll) selectAll.checked = false;
    updateVolumeBulkActionsBar();
}

function updateVolumeBulkActionsBar() {
    const bar = document.getElementById('volume-bulk-actions-bar');
    if (!bar) return;
    const count = selectedVolumeIds.size;
    if (count === 0) {
        bar.style.display = 'none';
        bar.innerHTML = '';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = `
        <span class="volume-bulk-actions-count">${count} ${pluralize(count, 'tome')} ${pluralize(count, 'sélectionné')}</span>
        <button class="btn-neutral-sm" onclick="bulkEditSelectedVolumes()">${svgIcon('pencil')} Éditer</button>
        <button class="btn-neutral-sm" onclick="bulkDownloadSelectedVolumes()">${svgIcon('download')} Télécharger</button>
        <button class="btn-neutral-sm" onclick="bulkConvertSelectedVolumes()">${svgIcon('package')} Convertir en cbz</button>
        <button class="btn-neutral-sm" onclick="bulkRefreshSelectedVolumes()">${svgIcon('refresh-cw')} Actualiser</button>
        <button class="btn-neutral-sm" onclick="bulkUpdateMetadataSelectedVolumes()">${svgIcon('tag')} MAJ métadonnées</button>
        <button class="btn-danger-sm" onclick="bulkDeleteSelectedVolumes()">${svgIcon('trash-2')} Supprimer</button>
        <button class="btn-neutral-sm" onclick="clearVolumeTableSelection()">Annuler la sélection</button>
    `;
}

// Résout les ids sélectionnés en objets tome complets depuis currentSeriesDetail (déjà en
// mémoire, pas de fetch) - filtre les ids qui ne s'y retrouvent plus (course avec un
// rafraîchissement entretemps) plutôt que de planter sur un undefined.
function _selectedVolumesData() {
    const all = (currentSeriesDetail && currentSeriesDetail.volumes) || [];
    return [...selectedVolumeIds].map(id => all.find(v => v.id === id)).filter(Boolean);
}

// Pattern "sélection groupée" commun aux 3 actions ci-dessous (voir aussi le même
// pattern sur /verification, CLAUDE.md): traitement SÉQUENTIEL (jamais Promise.all - les
// écritures Bédéthèque sont déjà limitées côté serveur par un délai anti-bot, les
// paralléliser ne les accélérerait pas, juste plus de requêtes en vol à la fois), un seul
// toast mis à jour au fil de la progression, échecs individuels collectés pour une seule
// alerte récapitulative à la fin plutôt qu'une popup par échec.
async function _runBulkVolumeAction(volumes, toastId, verbFn, actionFn) {
    const failures = [];
    for (let i = 0; i < volumes.length; i++) {
        const v = volumes[i];
        showToast(toastId, `${verbFn(i + 1, volumes.length)}`);
        try {
            const response = await actionFn(v);
            const data = await response.json();
            if (!data.success) failures.push(`${v.filename || v.id}: ${data.error || 'erreur inconnue'}`);
        } catch (error) {
            failures.push(`${v.filename || v.id}: ${error.message}`);
        }
    }
    showToast(toastId, failures.length ? `Terminé avec ${failures.length} ${pluralize(failures.length, 'erreur')}` : 'Terminé', { icon: failures.length ? 'triangle-alert' : 'check', autoHideMs: 4000 });
    if (failures.length) alert('❌ Échecs:\n' + failures.join('\n'));
    clearVolumeTableSelection();
    if (currentSeriesDetail) await renderSeriesDetail(currentSeriesDetail.id);
}

async function bulkDeleteSelectedVolumes() {
    const volumes = _selectedVolumesData();
    if (volumes.length === 0) return;
    if (!confirm(`Supprimer le fichier de ${volumes.length} ${pluralize(volumes.length, 'tome')} ?\n\nLes tomes resteront suivis comme manquants (leur fiche n'est pas retirée).`)) return;

    await _runBulkVolumeAction(
        volumes, 'bulk-delete-volumes',
        (i, n) => `Suppression... (${i}/${n})`,
        v => fetch(`/api/volumes/${v.id}`, { method: 'DELETE' })
    );
}

// "toujours erreur pour conversion [...] quand je quitte la page" - contrairement aux 3
// autres actions groupées (delete/refresh/update-metadata, voir _runBulkVolumeAction),
// la conversion ne passe plus par une boucle JS séquentielle (un fetch par tome, le
// suivant attend la réponse du précédent) : fermer l'onglet/naviguer ailleurs
// interrompait cette boucle elle-même, laissant tout tome pas encore lancé jamais
// converti. Un seul appel à /api/bedetheque/convert-volumes-batch (qui lance SON PROPRE
// thread côté serveur pour traiter toute la liste, format par tome relu depuis la base -
// voir sa docstring) remplace ça : la page peut être fermée immédiatement après, la
// conversion continue sans elle.
async function bulkConvertSelectedVolumes() {
    const volumes = _selectedVolumesData();
    const convertible = volumes.filter(v => ['cbr', 'pdf', 'zip'].includes((v.format || '').toLowerCase()));
    const skipped = volumes.length - convertible.length;
    if (convertible.length === 0) {
        alert("Aucun tome sélectionné n'est dans un format convertible (cbr/pdf/zip).");
        return;
    }
    if (!confirm(`Convertir ${convertible.length} ${pluralize(convertible.length, 'tome')} en cbz ?${skipped ? ` (${skipped} ${pluralize(skipped, 'tome')} déjà dans un autre format ${pluralize(skipped, 'sera', 'seront')} ${pluralize(skipped, 'ignoré')})` : ''}`)) return;

    const toastId = 'bulk-convert-volumes';
    try {
        const response = await fetch('/api/bedetheque/convert-volumes-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ volume_ids: convertible.map(v => v.id) })
        });
        const data = await response.json();
        if (!data.success) {
            showToast(toastId, `❌ Erreur: ${data.error || 'erreur inconnue'}`, { icon: 'triangle-alert', autoHideMs: 6000 });
            return;
        }
        showToast(toastId, `Conversion... (${convertible.length} ${pluralize(convertible.length, 'tome')})`, { icon: 'loader-circle', autoHideMs: 4000 });
        clearVolumeTableSelection();
    } catch (error) {
        showToast(toastId, `❌ Erreur de connexion: ${error.message}`, { icon: 'triangle-alert', autoHideMs: 6000 });
    }
}

// "ajouter actualiser aussi à la page série dans les options" - même endpoint que le
// menu ⚙️ d'un tome isolé (voir POST /api/volumes/<id>/refresh, CLAUDE.md: relit juste
// file_size/page_count depuis le disque sans rescanner toute la série, utile quand un
// fichier a été remplacé à la main et que la taille en base est périmée), en sélection
// groupée plutôt qu'un par un.
async function bulkRefreshSelectedVolumes() {
    const volumes = _selectedVolumesData();
    if (volumes.length === 0) return;

    await _runBulkVolumeAction(
        volumes, 'bulk-refresh-volumes',
        (i, n) => `Actualisation... (${i}/${n})`,
        v => fetch(`/api/volumes/${v.id}/refresh`, { method: 'POST' })
    );
}

async function bulkUpdateMetadataSelectedVolumes() {
    const volumes = _selectedVolumesData();
    if (volumes.length === 0) return;
    if (!confirm(`Mettre à jour les métadonnées Bédéthèque de ${volumes.length} ${pluralize(volumes.length, 'tome')} ?`)) return;

    await _runBulkVolumeAction(
        volumes, 'bulk-update-metadata-volumes',
        (i, n) => `Mise à jour des métadonnées... (${i}/${n})`,
        v => fetch(`/api/bedetheque/update-metadata/volume/${v.id}`, { method: 'POST' })
    );
    showToast('komga-scan', 'Scan Komga demandé', { icon: 'radio', autoHideMs: 4000 });
}

function bulkEditSelectedVolumes() {
    const ids = [...selectedVolumeIds];
    if (ids.length === 0 || !currentSeriesDetail) return;
    startBulkEditQueue(currentSeriesDetail.id, ids);
}

// "Télécharger" en sélection groupée: un seul tome sélectionné -> téléchargement direct
// du fichier (même route qu'un tome isolé, voir buildVolumeActionsGearHtml), plusieurs
// tomes -> zip généré à la volée côté serveur (download_volumes_zip, blueprints/library/
// routes.py). fetch + blob comme downloadSeriesZip plus haut - une requête POST (le
// choix de tomes est arbitraire, pas juste un id dans l'URL) ne permet pas la simple
// navigation window.location.href utilisée pour un seul fichier.
async function bulkDownloadSelectedVolumes() {
    const volumes = _selectedVolumesData();
    if (volumes.length === 0) return;
    if (volumes.length === 1) {
        window.location.href = `/api/volumes/${volumes[0].id}/download`;
        return;
    }

    const toastId = 'bulk-download-volumes';
    showToast(toastId, `Préparation de l'archive (${volumes.length} ${pluralize(volumes.length, 'tome')})...`);
    try {
        const response = await fetch('/api/volumes/download-zip', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: volumes.map(v => v.id) })
        });
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            dismissToast(toastId);
            alert('❌ ' + (data.error || 'Erreur lors du téléchargement'));
            return;
        }
        const blob = await response.blob();
        const disposition = response.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="?([^"]+)"?/);
        const filename = match ? match[1] : 'tomes-selection.zip';
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        showToast(toastId, 'Terminé', { icon: 'check', autoHideMs: 4000 });
    } catch (error) {
        showToast(toastId, 'Erreur', { icon: 'circle-x', autoHideMs: 4000 });
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// "toutes ces series font parties du meme univers [...] on creera une entrée dans la
// base données pour univers" - chargé à part (pas dans data/bd du GET série principal):
// la grande majorité des séries n'appartiennent à aucun univers (pas de "Séries liées"
// sur Bédéthèque), autant ne pas alourdir la réponse principale pour ce cas courant.
// N'affiche rien (section vide) si universe est null.
async function loadSeriesUniverse(seriesId) {
    const container = document.getElementById(`series-universe-section-${seriesId}`);
    if (!container) return;
    try {
        const response = await fetch(`/api/bedetheque/universe/${seriesId}`);
        const data = await response.json();
        if (!data.success || !data.universe) return;

        const otherMembers = data.universe.members.filter(m => m.series_id !== seriesId);
        if (otherMembers.length === 0) return;

        // "les liens sont moches" - chips uniformes (même gabarit possédé/non possédé,
        // juste le logo Bédéthèque en plus pour distinguer un lien externe) plutôt
        // qu'une liste de liens texte à virgules mélangés à une image inline.
        // "met une petite icone + pour ajouter la bd dans l'application" - un membre pas
        // encore possédé garde son lien Bédéthèque (voir la fiche avant d'ajouter reste
        // utile) mais gagne un bouton "+" séparé, à côté plutôt que sur le lien
        // lui-même pour ne pas transformer un clic "je veux voir la fiche" en ajout
        // accidentel.
        const membersHtml = otherMembers.map(m => m.series_id
            ? `<a href="/series/${m.series_id}" class="universe-member-chip">${escapeHtml(m.title)}</a>`
            : `<span class="universe-member-chip universe-member-chip-external-wrap">
                   <a href="${escapeHtml(m.bedetheque_url)}" target="_blank" rel="noopener" class="universe-member-chip-external" data-tooltip="Pas encore dans votre bibliothèque"><img src="/static/img/bedetheque-logo.png" alt="">${escapeHtml(m.title)}</a>
                   <button type="button" class="universe-member-add-btn" onclick="addUniverseMemberSeries('${escapeForAttribute(m.bedetheque_url)}', ${data.universe.library_id}, ${seriesId}, '${escapeForAttribute(m.title)}', this)" data-tooltip="Ajouter cette série à la bibliothèque">+</button>
               </span>`
        ).join('');

        container.innerHTML = `
            <div class="series-detail-universe">
                <span class="series-detail-universe-label">
                    🌐 Univers <strong>${escapeHtml(data.universe.name || '')}</strong>
                    <button type="button" class="btn-icon-only" style="vertical-align:middle;" onclick="renameUniverse(${data.universe.id}, '${escapeForAttribute(data.universe.name || '')}', ${seriesId})" data-tooltip="Renommer cet univers">${svgIcon('pencil')}</button>
                </span>
                <div class="series-detail-universe-members">${membersHtml}</div>
            </div>
        `;
    } catch (error) {
        // Best-effort, section purement informative - une erreur ne doit pas gêner le
        // reste de la fiche série déjà affichée.
    }
}

// "met une petite icone + pour ajouter la bd dans l'application" - ajout direct d'une
// série liée pas encore possédée, même endpoint que "Albums de l'auteur"
// (author-albums.js) plutôt qu'une nouvelle route pour le même besoin. viewingSeriesId:
// rafraîchit la section univers de LA FICHE ACTUELLEMENT AFFICHÉE une fois l'ajout fait,
// pour que le chip bascule immédiatement en "possédée" (lien interne, plus de bouton +).
async function addUniverseMemberSeries(bedethequeUrl, libraryId, viewingSeriesId, title, button) {
    button.disabled = true;
    // "ca ne fait rien et le toast apparait seulement apres qu'il ait chargé. ajoute le
    // toast instantané avec le nom de la série" - feedback immédiat au clic, avant même
    // la requête réseau (scraping Bédéthèque, peut prendre plusieurs secondes), plutôt
    // que de laisser le bouton "+" comme seul indice qu'un clic a bien été pris en compte.
    showToast('universe-add-series', `⏳ Ajout de « ${title} »...`, { icon: 'loader-circle' });
    try {
        const response = await fetch('/api/bedetheque/add-series', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: bedethequeUrl, library_id: libraryId })
        });
        const data = await response.json();
        if (!data.success) {
            showToast('universe-add-series', `❌ « ${title} » : ${data.error || 'erreur inconnue'}`, { icon: 'circle-x', autoHideMs: 5000 });
            button.disabled = false;
            return;
        }
        showToast('universe-add-series', `✅ « ${title} » ajoutée`, { icon: 'check', autoHideMs: 4000, href: `/series/${data.series_id}` });
        loadSeriesUniverse(viewingSeriesId);
    } catch (error) {
        showToast('universe-add-series', `❌ « ${title} » : ${error.message}`, { icon: 'circle-x', autoHideMs: 5000 });
        button.disabled = false;
    }
}

// "renaming the universe will have to rename the files" - le backend déplace le(s)
// dossier(s) d'univers déjà nichés (voir rename_universe côté bedetheque/routes.py), pas
// juste la ligne en base - le toast reflète donc le résultat réel (nb de séries
// déplacées / erreurs) plutôt qu'un simple "renommé" optimiste.
async function renameUniverse(universeId, currentName, viewingSeriesId) {
    const newName = prompt('Nouveau nom de l\'univers:', currentName);
    if (!newName || !newName.trim() || newName.trim() === currentName) return;

    showToast('universe-rename', `⏳ Renommage de l'univers...`, { icon: 'loader-circle' });
    try {
        const response = await fetch(`/api/bedetheque/universe/${universeId}/name`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName.trim() })
        });
        const data = await response.json();
        if (!data.success) {
            showToast('universe-rename', `❌ ${data.error || 'Erreur inconnue'}`, { icon: 'circle-x', autoHideMs: 5000 });
            return;
        }
        if (data.errors && data.errors.length > 0) {
            showToast('universe-rename', `⚠️ Renommé, mais ${data.errors.length} déplacement(s) en échec`, { icon: 'triangle-alert', autoHideMs: 6000 });
        } else {
            showToast('universe-rename', `✅ Univers renommé « ${data.name} »`, { icon: 'check', autoHideMs: 4000 });
        }
        loadSeriesUniverse(viewingSeriesId);
    } catch (error) {
        showToast('universe-rename', `❌ ${error.message}`, { icon: 'circle-x', autoHideMs: 5000 });
    }
}

// Charge et affiche les détails d'une série dans le conteneur #modal-body de la page
// courante. Utilisé au chargement de series-detail.html, et pour rafraîchir la vue en
// place après une action (scan, renommage, toggle one-shot, enrichissement...)
// Série précédente/suivante (ordre alphabétique de la bibliothèque, comme la liste de la
// page bibliothèque) pour la navigation ◀/▶ du bandeau d'actions - extrait de
// renderSeriesDetail (qui mélangeait ce fetch/cette logique avec la construction du
// template) pour rester isolément lisible/modifiable sans toucher au reste de la fonction.
async function _computeAdjacentSeriesNav(seriesId, libraryId) {
    try {
        const librarySeriesResponse = await fetch(`/api/library/${libraryId}/series`);
        const librarySeries = await librarySeriesResponse.json();
        const currentIndex = librarySeries.findIndex(s => s.id === seriesId);
        return {
            prev: currentIndex > 0 ? librarySeries[currentIndex - 1] : null,
            next: (currentIndex >= 0 && currentIndex < librarySeries.length - 1) ? librarySeries[currentIndex + 1] : null
        };
    } catch (error) {
        console.error('Erreur chargement séries adjacentes:', error);
        return { prev: null, next: null };
    }
}

// Taille totale de la collection (somme des fichiers) - pas exposée telle quelle par
// l'API, calculée côté client à partir des volumes déjà chargés.
function _computeSeriesTotalSize(volumes) {
    return (volumes || []).reduce((sum, v) => sum + (v.file_size || 0), 0);
}

async function renderSeriesDetail(seriesId) {
    const modalBody = document.getElementById('modal-body');

    // Donnée préchargée depuis la fiche précédente (voir prefetchAdjacentSeries) - si
    // présente et fraîche, on saute complètement le fetch ET le spinner: la navigation
    // ⬅️/➡️ arrive alors vraiment instantanée au lieu d'afficher un chargement pendant
    // l'aller-retour réseau qu'on vient justement d'éviter.
    const prefetched = _consumeSeriesPrefetch(seriesId);
    let data;

    if (prefetched) {
        data = prefetched;
    } else {
        modalBody.innerHTML = '<div class="loading"><div class="spinner"></div><p>Chargement des détails...</p></div>';

        try {
            const response = await fetch(`/api/series/${seriesId}`);

            // "quand une série a été supprimé ca fait l'erreur... change pour quelque
            // chose de mieux" - un lien/retour arrière/onglet resté ouvert vers une série
            // depuis supprimée (ou fusionnée dans une autre) tombait sur le message
            // d'erreur générique ci-dessous, affichant le JSON brut de la réponse Flask
            // ("Erreur serveur 404: {"error":"Série introuvable"}") - un cas suffisamment
            // fréquent et normal (pas un vrai problème serveur) pour son propre message,
            // avec un lien pour repartir vers la bibliothèque plutôt qu'une page bloquée.
            if (response.status === 404) {
                // "c'est un peu moche. tout est collé. le lien en bleu j'aime pas" -
                // .no-results (utilisée juste avant) n'est stylée que dans style-search.css,
                // jamais chargée sur cette page (library.html/series-detail.html) - h2/p/a
                // s'affichaient donc avec les styles par défaut du navigateur, dont le bleu
                // natif du lien. .no-data (style.css, déjà chargée ici et déjà utilisée
                // ailleurs dans ce même fichier pour un message similaire) + le lien "retour"
                // déjà établi de cette page (.series-detail-back-link) plutôt qu'un <a> nu.
                modalBody.innerHTML = `
                    <div class="no-data">
                        <span class="no-data-icon">🗑️</span>
                        <h2>Série introuvable</h2>
                        <p>Cette série n'existe plus - elle a probablement été supprimée ou fusionnée avec une autre depuis.</p>
                        <a href="/" class="series-detail-back-link">← Retour à la bibliothèque</a>
                    </div>
                `;
                return;
            }

            if (!response.ok) {
                const text = await response.text();
                throw new Error(`Erreur serveur ${response.status}: ${text.substring(0, 200)}`);
            }

            data = await response.json();
        } catch (error) {
            console.error('Erreur chargement série:', error);
            modalBody.innerHTML = `
                <div class="no-results">
                    <h2>❌ Erreur</h2>
                    <p>${escapeHtml(error.message)}</p>
                </div>
            `;
            return;
        }
    }

    try {
        // Sauvegarder le titre de la série pour la recherche
        currentSeriesTitle = data.title;
        currentSeriesDetail = data;

        adjacentSeriesNav = await _computeAdjacentSeriesNav(seriesId, data.library.id);
        prefetchAdjacentSeries();

        const totalSize = _computeSeriesTotalSize(data.volumes);

        // Liens directs Komga/Bédéthèque de la série, affichés à côté du titre (même
        // traitement que pour chaque volume): Komga = série matchée, Bédéthèque = match
        // persisté au niveau série (series.bedetheque_url, voir openBedethequeMatchModal),
        // avec repli sur le lien trouvé dans le ComicInfo d'un tome pour les séries
        // enrichies avant l'ajout de ce champ dédié
        const seriesBedethequeUrl = data.bedetheque.url || (data.volumes || [])
            .map(v => v.comicinfo && v.comicinfo.web)
            .find(web => web && /bedetheque\.com/i.test(web)) || null;
        const seriesKomgaLinkHtml = data.komga.url
            ? `<a href="${escapeHtml(data.komga.url)}" target="_blank" rel="noopener" class="volume-link-icon" title="Ouvrir sur Komga"><img src="/static/img/komga-logo.svg" alt="Komga"></a>`
            : '';
        const seriesBedethequeLinkHtml = seriesBedethequeUrl
            ? `<a href="${escapeHtml(seriesBedethequeUrl)}" target="_blank" rel="noopener" class="volume-link-icon" title="Ouvrir sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque"></a>`
            : '';

        // Un one-shot n'affiche pas de carte volume plus bas (un seul fichier, déjà
        // représenté par la couverture/le titre ci-dessus): ses tags/auteur/artistes/
        // éditeur/genre (lus depuis son ComicInfo.xml) sont donc affichés ici, dans le
        // header, plutôt que perdus faute de carte pour les porter. Pas d'icônes
        // d'action (🏷️/✏️) ici: pour un one-shot/intégrale, seuls les liens externes
        // (seriesLinksHtml) sont affichés à cet endroit - MAJ métadonnées et Renommer
        // restent accessibles via la toolbar en haut de page, pas doublés ici. La
        // recherche EBDZ/Prowlarr (manquant ou remplacement) n'a pas cet équivalent
        // ailleurs pour un one-shot (pas de carte volume = pas de bouton "Rechercher"):
        // un bouton dédié est ajouté dans la toolbar, à côté de "⚙️ Actions" (voir plus
        // bas, oneshotSearchNumber/oneshotSearchToolbarHtml).
        // "2020 • pages null • N/A" - un one-shot pouvait porter DEUX lignes volumes en
        // base (un placeholder vide + le fichier réellement possédé, voir le correctif
        // dans add_series_from_bedetheque, blueprints/bedetheque/routes.py, pour la cause
        // racine) - data.volumes[0] prenait alors le premier de la liste sans distinguer
        // lequel, affichant parfois les stats (pages/taille) du placeholder vide au lieu
        // du vrai fichier. Préfère toujours une ligne avec un fichier réel (filepath) si
        // l'une existe, quel que soit son rang dans le tableau.
        const oneshotFile = (data.is_oneshot && (data.volumes || []).length > 0)
            ? (data.volumes.find(v => v.filepath) || data.volumes[0])
            : null;
        const oneshotSearchNumber = oneshotFile
            ? (oneshotFile.volume_number ?? oneshotFile.integral_number ?? oneshotFile.hs_number ?? 'null')
            : 'null';
        // Plus de badges auteur/dessinateur/éditeur/genre ici (buildVolumeMetaBadgesHtml):
        // doublon avec la grille de métadonnées de l'en-tête (Auteur/Éditeur/Genre/Statut,
        // voir bdMetaHtml) qui couvre déjà ces champs pour toute série matchée, one-shot
        // compris - juste le détail du fichier lui-même (format/taille), qui lui n'a pas
        // d'équivalent ailleurs.
        // "dans les pages one-shot je n'ai pas l'information du releaser et de la qualité"
        // - resolution/release_group ne sont PAS dans bdMetaHtml (ce sont des champs du
        // FICHIER, pas de la fiche Bédéthèque de la série), donc pas de doublon à éviter
        // ici contrairement aux badges retirés ci-dessus - le premier passage n'avait
        // ajouté ces deux champs qu'à buildVolumeMetaBadgesHtml, jamais appelée pour un
        // one-shot (celui-ci n'affiche pas de carte volume, voir plus haut), donc restés
        // invisibles pour toute série one-shot malgré la donnée déjà présente en base.
        // "too big. remove the background. keep it at the same level as the details
        // section. on the right of the details" - retour sur le premier essai (badges
        // .badge, ligne séparée en dessous): texte simple sans fond, sur la MÊME ligne
        // que buildVolumeFileDetailHtml (flex + space-between plutôt que deux <div>).
        // "les releasers ne les met pas a droite mais collé apres les details" -
        // resolution reste à droite (demande d'origine ci-dessus, inchangée), mais le
        // releaser rejoint maintenant le texte de détails lui-même (même <span>, à la
        // suite) plutôt que le bloc droit - ce n'est pas une "qualité" au même titre
        // que la résolution, juste une info supplémentaire sur le fichier.
        // "the quality is too much on the right with one-shot. this should be close
        // to the releaser separated by ." - retour sur le bloc droit ci-dessus:
        // qualité déplacée dans le même <span> que le releaser, à sa suite, séparée
        // par un "." plutôt qu'un bloc à part en justify-content:space-between.
        const oneshotQualityText = oneshotFile && oneshotFile.resolution
            ? `🖼️ ${escapeHtml(String(oneshotFile.resolution))}` : '';
        const oneshotReleaserText = oneshotFile && oneshotFile.release_group
            ? `📀 ${escapeHtml(String(oneshotFile.release_group))}` : '';
        const oneshotExtraParts = [oneshotReleaserText, oneshotQualityText].filter(Boolean);
        const oneshotExtraSuffix = oneshotExtraParts.length ? ` • ${oneshotExtraParts.join(' . ')}` : '';
        const oneshotMetaHtml = oneshotFile
            ? `<div class="volume-file-detail">
                <span>${buildVolumeFileDetailHtml(oneshotFile)}${oneshotExtraSuffix}</span>
              </div>`
            : '';

        let completenessIconHtml = '';
        if (data.bedetheque_complete != null) {
            const icon = data.bedetheque_complete ? '✅' : '⚠️';
            const label = data.bedetheque_complete ? 'Série complète' : 'Série incomplète';
            completenessIconHtml = `<span class="series-detail-completeness-icon" data-tooltip="${escapeHtml(label)} - ${escapeHtml(data.bedetheque_complete_reason || '')}">${icon}</span>`;
        }

        // Mise en page façon Sonarr: couverture en colonne de gauche occupant toute la
        // hauteur du bloc, titre + navigation précédent/suivant en haut à droite, puis
        // emplacement/taille du fichier et liens externes (révélés au survol pour ne pas
        // surcharger l'en-tête), et enfin le résumé
        const { prev: prevSeries, next: nextSeries } = adjacentSeriesNav;
        const navHtml = `
            <div class="series-detail-nav">
                <button class="series-nav-arrow" ${prevSeries ? `onclick="navigateToSeriesInPage(${prevSeries.id})"` : 'disabled'} data-tooltip="${prevSeries ? 'Précédent: ' + escapeHtml(prevSeries.title) : 'Aucune série précédente'}">‹</button>
                <button class="series-nav-arrow" ${nextSeries ? `onclick="navigateToSeriesInPage(${nextSeries.id})"` : 'disabled'} data-tooltip="${nextSeries ? 'Suivant: ' + escapeHtml(nextSeries.title) : 'Aucune série suivante'}">›</button>
            </div>
        `;

        const seriesEbdzLinkHtml = data.ebdz.thread_url
            ? `<a href="${escapeHtml(data.ebdz.thread_url)}" target="_blank" rel="noopener" class="volume-link-icon" title="Ouvrir sur EBDZ"><img src="/static/img/ebdz-logo.png" alt="EBDZ"></a>`
            : '';

        // Un seul et unique bloc de liens externes, quel que soit le type de série
        // (one-shot, intégrale ou album classique): les cartes/actions par tome
        // (buildVolumeActionIconsHtml) n'affichent plus Komga/Bédéthèque pour éviter
        // tout doublon avec ce bloc.
        //
        // Ouvert au clic (comme le menu ⚙️ Actions) plutôt qu'au survol avec une
        // animation max-width: cette ligne (.series-detail-stats) est en flex-wrap, donc
        // avec une fenêtre large tous les éléments tiennent sur une seule ligne sans
        // laisser de place à droite du déclencheur pour que les icônes s'y déploient -
        // elles se retrouvaient coupées/à moitié hors champ. Un menu détaché en
        // position:fixed (voir toggleToolbarDropdown) n'a plus ce problème: il s'affiche
        // par-dessus, sans dépendre de l'espace disponible dans la ligne.
        const seriesLinksMenuId = `series-links-menu-${seriesId}`;
        const seriesLinksHtml = (seriesKomgaLinkHtml || seriesEbdzLinkHtml || seriesBedethequeLinkHtml)
            ? `
                <div class="toolbar-dropdown series-detail-links">
                    <span class="series-detail-links-trigger" onclick="toggleToolbarDropdown('${seriesLinksMenuId}', this)" title="Liens externes">${svgIcon('link')} Liens</span>
                    <div class="toolbar-dropdown-menu series-detail-links-menu" id="${seriesLinksMenuId}" style="display: none;">
                        <div class="series-detail-links-icons">
                            ${seriesKomgaLinkHtml}
                            ${seriesEbdzLinkHtml}
                            ${seriesBedethequeLinkHtml}
                        </div>
                    </div>
                </div>
            `
            : '';

        // Couverture: le local (extrait des fichiers) prime, avec repli sur les
        // métadonnées Bédéthèque - indispensable pour une série ajoutée depuis la
        // recherche Bédéthèque, qui n'a encore aucun fichier
        const bd = data.bedetheque || {};
        const detailCoverPath = data.local_cover_path || bd.cover_path
            || (data.komga && data.komga.cover_path) || null;
        // Résumé: Bédéthèque prime sur le local (contrairement à la couverture ci-dessus)
        // - Bédéthèque est la source de référence pour le texte descriptif (voir
        // CLAUDE.md), alors que local_summary vient du tag <Summary> du ComicInfo.xml
        // des tomes, souvent renseigné par un uploader avec des notes d'édition plutôt
        // qu'un vrai synopsis (constaté: "Info édition: Noté 'Première édition'...' au
        // lieu du résumé Bédéthèque pourtant disponible pour cette série). manual_summary
        // (édition manuelle, voir openManualEditModal) reste prioritaire sur tout le reste
        // s'il a été explicitement saisi.
        const detailSummary = data.manual_summary || bd.description || data.local_summary || null;

        // "click on author will open a modal for other album from the same author"
        // (item #25 improvement.txt) - un nom cliquable seulement s'il existe une URL de
        // fiche auteur connue pour lui (bd.author_links, voir get_series_details côté
        // Flask): un nom sans URL (auteur sans fiche dédiée sur Bédéthèque) reste du texte
        // brut plutôt qu'un lien mort. Plusieurs noms possibles par cellule ("Untel,
        // Machin"), chacun cliquable indépendamment - même séparateur ", " que le join()
        // qui a produit cette chaîne côté serveur (voir update_series_bedetheque_info).
        const authorLinks = bd.author_links || {};
        // "dans panthéon tu as les icones des auteurs. c'est possible de les récupérer
        // en static pour les afficher dans les pages de serie" - un slot vide par
        // auteur cliquable (donc avec URL connue), rempli après coup par
        // loadSeriesAuthorPhotos une fois la fiche série déjà affichée (voir plus bas):
        // aucune photo n'est encore en cache local la première fois qu'un auteur est
        // vu, et la récupérer bloquerait l'affichage de plusieurs secondes (anti-bot
        // Bédéthèque, voir get_author_photo_path côté Flask).
        function _authorNamesHtml(namesString) {
            if (!namesString) return '';
            return namesString.split(', ').map(name => {
                const url = authorLinks[name];
                return url
                    ? `<span class="author-photo-slot" data-author-url="${escapeHtml(url)}"></span><a href="javascript:void(0)" style="cursor:pointer;" data-tooltip="Voir les autres albums de ${escapeHtml(name)}" onclick="openAuthorAlbumsModal('${escapeForAttribute(url)}', '${escapeForAttribute(name)}')">${escapeHtml(name)}</a>`
                    : escapeHtml(name);
            }).join(', ');
        }

        // Métadonnées Bédéthèque de la série (auteurs, éditeur, genre, statut, tomes
        // parus), affichées en grille "façon console" (voir .series-detail-meta-grid) -
        // scénariste et dessinateur sur une seule cellule "Auteur" quand c'est la même
        // personne (cas le plus courant), sinon deux cellules distinctes. Valeurs déjà
        // échappées/construites en HTML sûr ICI (voir _authorNamesHtml ci-dessus pour les
        // cellules auteur) - le rendu final ci-dessous n'échappe plus une seconde fois.
        const metaCells = [];
        if (bd.scenaristes && bd.dessinateurs && bd.dessinateurs !== bd.scenaristes) {
            metaCells.push(['Scénario', _authorNamesHtml(bd.scenaristes)]);
            metaCells.push(['Dessin', _authorNamesHtml(bd.dessinateurs)]);
        } else if (bd.scenaristes || bd.dessinateurs) {
            metaCells.push(['Auteur', _authorNamesHtml(bd.scenaristes || bd.dessinateurs)]);
        }
        if (bd.editeurs) metaCells.push(['Éditeur', escapeHtml(bd.editeurs)]);
        if (bd.genre) metaCells.push(['Genre', escapeHtml(bd.genre)]);
        if (bd.status) {
            const years = bd.year_start ? ` (${bd.year_start}${bd.year_end && bd.year_end !== bd.year_start ? '-' + bd.year_end : ''})` : '';
            metaCells.push(['Statut', escapeHtml(`${bd.status}${years}`)]);
        }
        if (bd.total_volumes) metaCells.push(['Parus', escapeHtml(`${bd.total_volumes} tome${bd.total_volumes > 1 ? 's' : ''}`)]);
        const bdMetaHtml = metaCells.length
            ? `<div class="series-detail-meta-grid">${metaCells.map(([k, v]) =>
                `<div class="series-detail-meta-cell"><span class="k">${escapeHtml(k)}</span><span class="v">${v}</span></div>`
              ).join('')}</div>`
            : '';

        const detailCoverUrl = detailCoverPath ? `/${detailCoverPath}` : null;

        const headerHtml = `
            <a href="/" class="settings-back-link series-detail-back-link">← Retour à la bibliothèque</a>
            <div class="series-detail-header">
                <div class="series-detail-cover-col">
                    ${detailCoverPath
                        ? `<img class="series-detail-cover clickable-cover" src="${escapeHtml(detailCoverUrl)}" alt="${escapeHtml(data.title)}" tabindex="0" role="button" title="Afficher la couverture">`
                        : `<div class="series-detail-cover series-detail-cover-placeholder">📚</div>`}
                </div>
                <div class="series-detail-info">
                    <div class="series-detail-title-row">
                        <div class="series-detail-title-with-icon">
                            <h2 class="series-detail-title">${escapeHtml(data.title)}</h2>
                            ${completenessIconHtml}
                        </div>
                        ${navHtml}
                    </div>
                    <div class="series-detail-stats">
                        <span>📖 ${data.total_volumes} ${pluralize(data.total_volumes, 'volume')}${
                            // Une intégrale (badge déjà affiché ci-dessus, avec son numéro)
                            // n'est pas un "vrai" one-shot au sens propre (une œuvre jamais
                            // découpée en tomes, alors qu'une intégrale en compile plusieurs):
                            // on n'affiche donc "One-shot" que si ce n'en est pas une. Un
                            // hors-série, lui, reste un one-shot valide (une parution spéciale
                            // publiée seule n'est pas contradictoire) : il ne supprime pas ce
                            // libellé
                            ((data.is_oneshot || data.bedetheque_complete_reason === 'One-Shot') && !(oneshotFile && oneshotFile.is_integral)) ? ' 🔸 One-Shot' : ''
                        }${data.has_parts ? ' • arcs/parties' : ''}</span>
                        <span>💾 ${formatBytes(totalSize)}</span>
                        <span>📁 ${escapeHtml(data.path)}</span>
                        ${oneshotFile ? `<span>📄 ${escapeHtml(oneshotFile.filename)}</span>` : ''}
                        ${seriesLinksHtml}
                    </div>
                    ${bdMetaHtml}
                    ${oneshotMetaHtml}
                    <div id="series-universe-section-${seriesId}"></div>
                    ${detailSummary ? `<p class="series-detail-summary">${escapeHtml(detailSummary)}</p>` : ''}
                </div>
            </div>
        `;

        // Bandeau d'actions façon Sonarr (icône + libellé), avec le statut de matching
        // EBDZ/Komga directement dans la toolbar (à côté de leur bouton respectif) et la
        // navigation vers la série précédente/suivante de la bibliothèque. MAJ
        // métadonnées/Renommer restent toujours ici, y compris pour un one-shot/
        // intégrale: la zone sous le titre (oneshotMetaHtml) n'affiche plus que les
        // liens externes pour ces séries, ces deux actions ne sont donc jamais doublées
        const oneshotDropdownItemHtml = (data.total_volumes <= 1 || data.is_oneshot) ? `
                            <button class="toolbar-dropdown-item" id="oneshot-btn-${seriesId}" onclick="toggleOneshot(${seriesId})" title="${data.is_oneshot ? 'Série marquée one-shot (pas de numérotation de tomes)' : 'Marquer cette série comme one-shot'}">${data.is_oneshot ? svgIcon('check') : svgIcon('star')} ${data.is_oneshot && oneshotFile && oneshotFile.is_integral ? 'Intégrale' : 'One-shot'}</button>
        ` : '';

        const toolbarHtml = `
            <div class="detail-toolbar">
                <div class="detail-toolbar-actions">
                    <div class="toolbar-dropdown">
                        <button class="toolbar-btn" id="series-actions-gear-${seriesId}" onclick="toggleToolbarDropdown('series-actions-menu-${seriesId}', this)" data-tooltip="Autres actions sur cette série">
                            <span class="toolbar-btn-icon">${svgIcon('settings')}</span><span class="toolbar-btn-label">Actions</span>
                        </button>
                        <div class="toolbar-dropdown-menu toolbar-dropdown-menu-autoclose" id="series-actions-menu-${seriesId}" style="display: none;">
                            <button class="toolbar-dropdown-item" onclick="scanSeries(${seriesId})" title="Scan rapide: ne retraite que les fichiers nouveaux/modifiés">${svgIcon('refresh-cw')} Actualiser</button>
                            <button class="toolbar-dropdown-item" onclick="updateSeriesOnlyMetadataFromBedetheque(${seriesId}, this)" title="Rafraîchit uniquement les infos de la série (résumé, couverture, statut, titre...) sans toucher aux fichiers des tomes">${svgIcon('tag')} MAJ métadonnées (série uniquement)</button>
                            <button class="toolbar-dropdown-item" onclick="updateSeriesMetadataFromBedetheque(${seriesId}, this)" title="Rafraîchit les infos de la série (résumé, couverture, statut...) ET écrit le ComicInfo.xml de tous les tomes (cbz uniquement)">${svgIcon('tag')} MAJ métadonnées (série + tomes)</button>
                            <button class="toolbar-dropdown-item" onclick="openMergeSeriesModal(${seriesId})" title="Fusionner cette série dans une autre (déplace tous ses tomes, cette fiche disparaît)">${svgIcon('git-merge')} Fusionner</button>
                            <button class="toolbar-dropdown-item" onclick="openManualEditModal(${seriesId})" title="Éditer à la main le résumé/genre/statut/auteur/année de la série et le ComicInfo.xml de chaque tome, sans passer par Bédéthèque">${svgIcon('pencil')} Éditer manuellement</button>
                            ${data.is_oneshot ? `
                            <button class="toolbar-dropdown-item" onclick="triggerSeriesFileUpload(${seriesId})" title="Envoyer directement le fichier de cette édition unique, sans passer par un répertoire d'import surveillé">${svgIcon('upload')} Ajouter un fichier</button>
                            ` : ''}
                            ${(data.is_oneshot && oneshotFile && oneshotFile.filepath) ? `
                            <button class="toolbar-dropdown-item" onclick="window.location.href='/api/volumes/${oneshotFile.id}/download'">${svgIcon('download')} Télécharger le fichier</button>
                            ` : ''}
                            ${oneshotDropdownItemHtml}
                            ${!data.is_oneshot ? `
                            <button class="toolbar-dropdown-item" onclick="toggleSeriesCompleteOverride(${seriesId})" title="${data.manual_complete_override ? 'Le calcul automatique (Terminé/Incomplet/En cours) reprendra la main' : 'Force le statut \'Terminé\' quel que soit le calcul automatique - utile si un mix d\'intégrales/albums le trompe'}">${data.manual_complete_override ? svgIcon('x') : svgIcon('check')} ${data.manual_complete_override ? 'Retirer la déclaration "complète"' : 'Déclarer cette série complète'}</button>
                            ` : ''}
                            ${(data.total_volumes > 1 && !data.is_oneshot) ? `
                            <button class="toolbar-dropdown-item" onclick="downloadSeriesZip(${seriesId})" title="Télécharge tous les tomes possédés de cette série dans une seule archive zip">${svgIcon('download')} Télécharger la série (zip)</button>
                            ` : ''}
                            <!-- "supprimer dans one-shot. je voudrais d'abord supprimer le fichier. ensuite on
                                 pourra supprimer la série. 2 étapes distinctes" puis "met Supprimer le fichier
                                 et Supprimer la série en derniere position. one-shot met le avant" - les deux
                                 actions de suppression sont désormais TOUJOURS les deux derniers éléments du
                                 menu, adjacentes (le toggle One-shot/Intégrale ci-dessus est passé avant elles,
                                 pas entre les deux comme précédemment) - "Supprimer le fichier" (deleteVolume,
                                 garde la fiche comme tome manquant) reste avant "Supprimer la série"
                                 (deleteSeries, tout en bas) pour un one-shot avec fichier. -->
                            ${(data.is_oneshot && oneshotFile && oneshotFile.filepath) ? `
                            <button class="toolbar-dropdown-item toolbar-dropdown-item-danger" onclick="deleteVolume(${oneshotFile.id}, ${seriesId}, '${escapeForAttribute(data.title)}')" title="Supprime uniquement le fichier - la fiche de cette série reste, pour supprimer aussi la série elle-même utilisez 'Supprimer la série' ci-dessous">${svgIcon('trash-2')} Supprimer le fichier</button>
                            ` : ''}
                            <button class="toolbar-dropdown-item toolbar-dropdown-item-danger" onclick="deleteSeries(${seriesId})" title="Supprimer définitivement cette série et son dossier du disque">${svgIcon('trash-2')} Supprimer la série</button>
                        </div>
                    </div>
                    ${oneshotFile ? `
                    <button class="toolbar-btn" onclick="searchMissingVolume(currentSeriesDetail.title, ${oneshotSearchNumber}, {seriesId: ${seriesId}, isIntegral: ${!!oneshotFile.is_integral}, isHs: ${!!oneshotFile.is_hs}${oneshotFile.filepath ? `, currentVolumeId: ${oneshotFile.id}` : ''}})" data-tooltip="Rechercher ${oneshotFile.filepath ? 'un remplacement' : 'une source'} sur EBDZ/Prowlarr">
                        <span class="toolbar-btn-icon">${svgIcon('search')}</span><span class="toolbar-btn-label">Rechercher</span>
                    </button>` : `
                    <button class="toolbar-btn" onclick="searchSeriesInSearchTab(${seriesId})" data-tooltip="Rechercher cette série dans l'onglet Recherche">
                        <span class="toolbar-btn-icon">${svgIcon('search')}</span><span class="toolbar-btn-label">Rechercher</span>
                    </button>`}
                    <!-- "recherche auto should be available in any case. does not matter what
                         is the condition" - n'exige plus (data.missing_volumes || []).length > 0:
                         le serveur (run_auto_acquire_for_series) sait déjà répondre "Aucun tome
                         manquant à chercher" via un toast quand il n'y a rien à faire (voir
                         runSeriesAutoAcquire, search-results-table.js) - inutile de deviner côté
                         client si ça vaut le coup avant même d'essayer. Seul is_oneshot reste un
                         vrai garde-fou (pas un calcul incertain): un one-shot a son propre bouton
                         dédié juste en dessous (volumeNumber/oneshot), les deux ne doivent
                         jamais coexister. -->
                    <!-- "recherche auto change l'icône pour moderne. toutes les autres en gris
                         celle ci en couleur" - svgIcon('radar') (même icône que le bouton
                         équivalent de la liste des séries/menu ⚙️ d'un tome, voir
                         bulkAutoAcquireSelectedSeries) au lieu de l'emoji 🔎, et
                         toolbar-btn-accent (voir style-library-search.css,
                         .detail-toolbar-actions) pour ressortir en couleur d'accent parmi les
                         icônes neutres des boutons voisins. -->
                    ${!data.is_oneshot ? `
                    <button class="toolbar-btn toolbar-btn-accent" onclick="runSeriesAutoAcquire(${seriesId}, {seriesTitle: currentSeriesDetail.title, buttonEl: this})" data-tooltip="Cherche et télécharge directement chaque tome manquant trouvé avec confiance, sans recherche manuelle">
                        <span class="toolbar-btn-icon">${svgIcon('radar')}</span><span class="toolbar-btn-label">Recherche auto</span>
                    </button>` : ''}
                    <!-- "pour les one-shot il n'y a pas de recherche automatique" - numéroté
                         (intégrale/HS/tome identifiable) -> même chemin que la molette d'un
                         tome (confiance par numéro exact) ; sans numéro -> confiance par
                         similarité de titre (voir runSeriesAutoAcquire, oneshot:true). -->
                    ${data.is_oneshot ? (oneshotSearchNumber !== 'null' ? `
                    <button class="toolbar-btn toolbar-btn-accent" onclick="runSeriesAutoAcquire(${seriesId}, {volumeNumber: ${oneshotSearchNumber}, seriesTitle: currentSeriesDetail.title, buttonEl: this})" data-tooltip="Cherche et télécharge directement une source trouvée avec confiance, sans recherche manuelle">
                        <span class="toolbar-btn-icon">${svgIcon('radar')}</span><span class="toolbar-btn-label">Recherche auto</span>
                    </button>` : `
                    <button class="toolbar-btn toolbar-btn-accent" onclick="runSeriesAutoAcquire(${seriesId}, {oneshot: true, seriesTitle: currentSeriesDetail.title, buttonEl: this})" data-tooltip="Cherche et télécharge directement une source trouvée avec confiance (vérifiée par similarité de titre), sans recherche manuelle">
                        <span class="toolbar-btn-icon">${svgIcon('radar')}</span><span class="toolbar-btn-label">Recherche auto</span>
                    </button>`) : ''}
                    <button class="toolbar-btn" id="monitor-btn-${seriesId}" onclick="toggleSeriesMonitor(${seriesId}, ${data.monitored ? 'true' : 'false'})" data-tooltip="${data.monitored ? 'Série surveillée: recherche EBDZ/Prowlarr des tomes manquants active' : 'Activer la surveillance de cette série (recherche EBDZ/Prowlarr des tomes manquants)'}">
                        <span class="toolbar-btn-icon">${data.monitored ? svgIcon('eye') : svgIcon('eye-off')}</span><span class="toolbar-btn-label">${data.monitored ? 'Surveillé' : 'Surveiller'}</span>
                    </button>
                    <div class="toolbar-dropdown">
                        <button class="toolbar-btn" onclick="toggleToolbarDropdown('series-metadata-menu-${seriesId}', this)" data-tooltip="Sources de métadonnées: Bédéthèque, EBDZ, Komga">
                            <span class="toolbar-btn-icon">${svgIcon('database')}</span><span class="toolbar-btn-label">Métadonnées</span>
                        </button>
                        <div class="toolbar-dropdown-menu series-metadata-menu toolbar-dropdown-menu-autoclose" id="series-metadata-menu-${seriesId}" style="display: none;">
                            <button class="toolbar-btn" onclick="openBedethequeMatchModal(${seriesId}, () => renderSeriesDetail(${seriesId}))" data-tooltip="Changer le match Bédéthèque de cette série">
                                <img src="/static/img/bedetheque-logo.png" alt="" class="toolbar-btn-logo"><span class="toolbar-btn-label">Bédéthèque</span>
                            </button>
                            ${enabledIntegrations.ebdz ? `<div class="toolbar-group" id="ebdz-toolbar-group-${seriesId}">
                                ${buildEbdzToolbarButtonHtml(seriesId, data.ebdz.thread_url)}
                                <span id="ebdz-status-modal-${seriesId}" class="toolbar-status">${buildEbdzStatusHtml({
                                    seriesId: seriesId, context: 'modal', ownedCount: data.total_volumes, ebdzCount: data.ebdz.volumes_count,
                                    missingVolumes: data.ebdz.missing_volumes, threadUrl: data.ebdz.thread_url, threadId: data.ebdz.thread_id,
                                    matchStatus: data.ebdz.match_status, matchedTitle: data.ebdz.matched_title, isOneshot: data.is_oneshot
                                })}</span>
                            </div>` : ''}
                            ${enabledIntegrations.komga ? `<div class="toolbar-group" id="komga-toolbar-group-${seriesId}">
                                ${buildKomgaToolbarButtonHtml(seriesId, data.komga.url)}
                                <span id="komga-status-modal-${seriesId}" class="toolbar-status">${buildKomgaStatusHtml({
                                    seriesId: seriesId, context: 'modal', matchStatus: data.komga.match_status, matchedTitle: data.komga.matched_title,
                                    komgaUrl: data.komga.url
                                })}</span>
                            </div>` : ''}
                        </div>
                    </div>
                    ${(data.bedetheque && data.bedetheque.url) ? `
                    <button class="toolbar-btn" onclick="openReadAlsoModal(${seriesId}, '${escapeForAttribute(data.title)}')" data-tooltip="Séries recommandées par Bédéthèque">
                        <span class="toolbar-btn-icon">${svgIcon('book-open')}</span><span class="toolbar-btn-label">À lire aussi</span>
                    </button>` : ''}
                    <button class="toolbar-btn" onclick="openSeriesHistoryModal(${seriesId}, '${escapeForAttribute(data.title)}')" data-tooltip="Historique de cette série: imports, téléchargements, renommages, suppressions">
                        <span class="toolbar-btn-icon">${svgIcon('history')}</span><span class="toolbar-btn-label">Historique</span>
                    </button>
                </div>
            </div>
        `;

        // Les tomes manquants "connus" (data.missing_volumes: gap filesystem + tomes
        // placeholder Bédéthèque, voir update_series_stats côté Flask) apparaissent
        // directement dans volumesHtml, une carte par tome à côté des tomes possédés (voir
        // buildVolumeItemHtml). "retire autre volumes manquants ebdz dans l'interface... je
        // n'en veux pas" - la section supplémentaire "Autres volumes manquants (détectés
        // via EBDZ)" (volumes que le thread EBDZ matché laisse deviner mais absents de
        // cette liste connue) est retirée; le badge de match EBDZ (ratio possédé/EBDZ,
        // lien vers le thread, parcourir les fichiers) reste inchangé - voir
        // buildEbdzStatusHtml.
        cleanupDetachedDropdownMenus();
        modalBody.innerHTML = `
            ${headerHtml}
            ${toolbarHtml}
            <div id="series-volumes-section">
                ${buildVolumesSectionHtml(data)}
            </div>
        `;
        const detailCover = modalBody.querySelector('.series-detail-cover.clickable-cover');
        if (detailCover) {
            const showDetailCover = event => {
                event.preventDefault();
                event.stopPropagation();
                openCoverModal(detailCover.currentSrc || detailCover.src, detailCover.alt);
            };
            detailCover.addEventListener('click', showDetailCover);
            detailCover.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') showDetailCover(event);
            });
        }
        renderVolumeTableColumnsMenu();
        initClearableSearchInputs(modalBody);
        loadSeriesUniverse(seriesId);
        loadSeriesAuthorPhotos(authorLinks);

        // Attacher écouteurs au badge "Non possédé" cliquable des tomes placeholder de la
        // liste principale (voir buildVolumeItemHtml) - évite des handlers inline cassés
        // par des apostrophes
        modalBody.querySelectorAll('.volume-missing-badge-clickable').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const title = decodeURIComponent(btn.getAttribute('data-series-title'));
                const volAttr = btn.getAttribute('data-volume-number');
                const vol = volAttr ? parseInt(volAttr) : null;
                const seriesId = btn.getAttribute('data-series-id') || null;
                const isIntegral = btn.getAttribute('data-is-integral') === '1';
                const isHs = btn.getAttribute('data-is-hs') === '1';
                const isEpisode = btn.getAttribute('data-is-episode') === '1';
                searchMissingVolume(title, vol, { seriesId, isIntegral, isHs, isEpisode });
            });
        });

        // La file de validation manuelle ouvre directement la même recherche que depuis
        // la fiche série, via ?review_volume=...; le contrôle reste entièrement manuel.
        const reviewParams = new URLSearchParams(window.location.search);
        if (reviewParams.has('review_id')) {
            const reviewVolumeRaw = reviewParams.get('review_volume');
            const reviewVolume = reviewVolumeRaw === '' ? null : parseInt(reviewVolumeRaw, 10);
            const reviewLabel = reviewParams.get('review_label') || '';
            const reviewOptions = {
                seriesId,
                isIntegral: /^Intégrale/i.test(reviewLabel),
                isHs: /^(?:HS|Hors-série)/i.test(reviewLabel),
                isEpisode: /^Épisode/i.test(reviewLabel),
            };
            setTimeout(() => searchMissingVolume(data.title, Number.isNaN(reviewVolume) ? null : reviewVolume, reviewOptions), 0);
        }
    } catch (error) {
        modalBody.innerHTML = `<div class="no-data"><h3>Erreur</h3><p>${escapeHtml(String(error.message))}</p></div>`;
    }
}

// Démarre l'écriture des métadonnées Bedetheque (résumé, auteurs, éditeur, genre,
// date...) dans le ComicInfo.xml de tous les tomes cbz d'une série (s'assure d'abord
// qu'un match Bedetheque existe). L'écriture elle-même se fait en arrière-plan côté
// serveur (une requête réseau par tome, volontairement espacée par Bédéthèque - voir
// _write_series_volumes_metadata_async côté Flask): cette fonction ne fait donc que
// démarrer le traitement, pas attendre qu'il finisse - pour une grosse série ça peut
// prendre plusieurs minutes, largement au-delà d'un timeout HTTP raisonnable.
function updateSeriesMetadataFromBedetheque(seriesId, buttonEl) {
    return _updateSeriesMetadataFromBedetheque(seriesId, buttonEl, 'all');
}

// "MAJ métadonnées (série uniquement)": rafraîchit résumé/couverture/statut/titre depuis
// Bédéthèque sans réécrire le ComicInfo.xml des tomes (voir scope=series côté Flask) -
// plus rapide qu'une MAJ complète quand on veut juste resynchroniser la fiche série
function updateSeriesOnlyMetadataFromBedetheque(seriesId, buttonEl) {
    return _updateSeriesMetadataFromBedetheque(seriesId, buttonEl, 'series');
}

async function _updateSeriesMetadataFromBedetheque(seriesId, buttonEl, scope) {
    const button = buttonEl;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="toolbar-btn-icon">⏳</span><span class="toolbar-btn-label">En cours...</span>';
    setSeriesActionsGearBusy(seriesId, true);
    const toastId = `metadata-series-${seriesId}`;
    showToast(toastId, 'Mise à jour des métadonnées...');
    let keepToastAlive = false;

    try {
        const response = await fetch(`/api/bedetheque/update-metadata/series/${seriesId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scope })
        });
        const data = await response.json();

        if (data.success && data.already_running) {
            // Une écriture était déjà en cours pour cette série (voir la garde dans
            // _align_title_and_start_metadata_write): pas de nouveau thread lancé pour
            // éviter une double écriture concurrente des mêmes cbz, on se contente de
            // reprendre le sondage de la progression déjà en cours
            keepToastAlive = true;
            pollMetadataWriteProgress(seriesId, toastId);
            setSeriesActionsGearBusy(seriesId, false);
        } else if (data.success && data.started) {
            keepToastAlive = true;
            pollMetadataWriteProgress(seriesId, toastId);
            setSeriesActionsGearBusy(seriesId, false);
            // Ne recharge la page série que si l'utilisateur la consulte encore - une
            // navigation AJAX (précédent/suivant) entretemps ne doit pas écraser une
            // autre série que l'utilisateur est en train de regarder
            if (currentSeriesDetail && currentSeriesDetail.id === seriesId) await renderSeriesDetail(seriesId);
        } else if (data.success) {
            // scope=series: pas de thread d'écriture lancé, tout est déjà à jour
            setSeriesActionsGearBusy(seriesId, false);
            if (currentSeriesDetail && currentSeriesDetail.id === seriesId) await renderSeriesDetail(seriesId);
        } else if (data.error === 'Impossible de trouver la série sur Bedetheque') {
            // Pas de match auto: laisser l'utilisateur choisir/coller une fiche Bedetheque,
            // puis relancer automatiquement cette même mise à jour une fois le match fait
            button.innerHTML = originalHtml;
            button.disabled = false;
            setSeriesActionsGearBusy(seriesId, false);
            openBedethequeMatchModal(seriesId, () => _updateSeriesMetadataFromBedetheque(seriesId, button, scope));
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            button.innerHTML = originalHtml;
            button.disabled = false;
            setSeriesActionsGearBusy(seriesId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        button.innerHTML = originalHtml;
        button.disabled = false;
        setSeriesActionsGearBusy(seriesId, false);
    } finally {
        if (!keepToastAlive) dismissToast(toastId);
    }
}

// pollMetadataWriteProgress vit maintenant dans nav.js (chargé sur toutes les pages, pas
// seulement ici) - une navigation vers une autre page pendant le sondage peut ainsi le
// reprendre au lieu de laisser le toast persisté figé sur son dernier message connu.

// Écrit les métadonnées Bedetheque dans le ComicInfo.xml d'un seul tome (bouton à côté
// du nom de fichier, voir buildVolumeItemHtml)
async function updateVolumeMetadataFromBedetheque(volumeId, evt) {
    const button = evt.currentTarget;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';
    setVolumeActionsGearBusy(volumeId, true);

    // Capturée maintenant, avant tout await: currentSeriesDetail est un global partagé
    // que la navigation AJAX précédent/suivant (navigateToSeriesInPage) peut réécrire
    // PENDANT que cette requête est en vol si l'utilisateur change de série entretemps -
    // sans cette capture, le code après le fetch relisait currentSeriesDetail.id et
    // pouvait se retrouver à agir sur la mauvaise série (ou aucune), laissant le toast de
    // progression sans son message final et donc jamais correctement "terminé" à l'écran.
    const targetSeriesId = currentSeriesDetail ? currentSeriesDetail.id : null;

    // Même format "Tome N - Titre" que le toast de MAJ "série + tomes" (voir
    // volumeProgressLabel / pollMetadataWriteProgress) - sans ça ce toast-ci était le
    // seul à ne pas nommer le tome concerné, incohérent avec l'autre affichage. Le titre
    // local (avant MAJ) est souvent vide au tout premier match, d'où la mise à jour du
    // toast avec le titre fraîchement récupéré une fois la réponse arrivée (voir plus bas)
    const toastId = `metadata-volume-${volumeId}`;
    const isOneshot = currentSeriesDetail && currentSeriesDetail.is_oneshot;
    const localVolume = currentSeriesDetail
        ? (currentSeriesDetail.volumes || []).find(v => v.id === volumeId)
        : null;
    const progressLabel = volumeProgressLabel(localVolume, isOneshot);
    const toastMessage = progressLabel
        ? `Mise à jour des métadonnées... (${progressLabel})`
        : 'Mise à jour des métadonnées...';
    showToast(toastId, toastMessage);
    let keepToastAlive = false;

    try {
        const response = await fetch(`/api/bedetheque/update-metadata/volume/${volumeId}`, { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            button.innerHTML = '✅';
            showToast('komga-scan', 'Scan Komga demandé', { icon: 'radio', autoHideMs: 4000 });

            // Ne rafraîchit/ne relit currentSeriesDetail que si l'utilisateur est
            // toujours sur la série d'origine - sinon il pointe désormais vers une autre
            // série (navigation AJAX entretemps) et la recharger ici écraserait ce que
            // l'utilisateur est en train de consulter
            const stillOnSameSeries = currentSeriesDetail && currentSeriesDetail.id === targetSeriesId;
            if (stillOnSameSeries) {
                await renderSeriesDetail(targetSeriesId);
            } else {
                // Pas de re-rendu (autre série affichée entretemps) donc ce bouton précis
                // ne sera jamais remplacé par un tout neuf - le remettre à son état normal
                // explicitement, sinon il resterait bloqué sur ✅ si l'utilisateur revenait
                // un jour sur cette série sans rechargement complet de page.
                button.innerHTML = originalHtml;
                button.disabled = false;
                setVolumeActionsGearBusy(volumeId, false);
            }

            // Le nom affiché vient de la base (currentSeriesDetail rechargé par
            // renderSeriesDetail ci-dessus, pas de data.comicinfo renvoyé tel quel par la
            // requête de MAJ) - c'est la valeur persistée qui fait foi, pas l'aller-retour
            // fichier de la réponse HTTP. Souvent la première fois qu'on connaît ce titre
            // pour ce tome (voir commentaire plus haut) - montrer le toast final avec ce
            // nom plutôt que de le faire disparaître silencieusement
            const updatedVolume = stillOnSameSeries
                ? (currentSeriesDetail.volumes || []).find(v => v.id === volumeId)
                : null;
            const updatedLabel = volumeProgressLabel(updatedVolume, isOneshot);
            keepToastAlive = true;
            showToast(toastId, updatedLabel ? `Métadonnées mises à jour (${updatedLabel})` : 'Métadonnées mises à jour', { icon: 'check', autoHideMs: 3000 });
        } else if (data.error === 'Impossible de trouver la série sur Bedetheque') {
            button.innerHTML = originalHtml;
            button.disabled = false;
            setVolumeActionsGearBusy(volumeId, false);
            if (targetSeriesId) {
                openBedethequeMatchModal(targetSeriesId, () => updateVolumeMetadataFromBedetheque(volumeId, evt));
            } else {
                alert('❌ Erreur: ' + data.error);
            }
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            button.innerHTML = originalHtml;
            button.disabled = false;
            setVolumeActionsGearBusy(volumeId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        button.innerHTML = originalHtml;
        button.disabled = false;
        setVolumeActionsGearBusy(volumeId, false);
    } finally {
        if (!keepToastAlive) dismissToast(toastId);
    }
}

// "telecharger toute la serie" (molette ⚙️ Actions de la fiche série, voir
// buildSeriesToolbar) - contrairement au téléchargement d'un seul tome
// (window.location.href='/api/volumes/<id>/download'), une simple navigation ne permet
// pas d'afficher l'erreur JSON renvoyée par download_series quand la série n'a aucun
// fichier (le navigateur tenterait de "télécharger" le corps JSON tel quel) - fetch +
// blob pour pouvoir distinguer les deux cas.
async function downloadSeriesZip(seriesId) {
    try {
        const response = await fetch(`/api/series/${seriesId}/download`);
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            alert('❌ ' + (data.error || 'Erreur lors du téléchargement'));
            return;
        }
        const blob = await response.blob();
        const disposition = response.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="?([^"]+)"?/);
        const filename = match ? match[1] : `serie-${seriesId}.zip`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Convertit un tome cbr/pdf/zip en cbz (bouton dédié affiché uniquement sur ces formats,
// voir buildVolumeActionsGearHtml) - action indépendante de la mise à jour des métadonnées
// Bedetheque. Une route Flask distincte par format source (voir convert-cbr/convert-pdf/
// convert-zip dans blueprints/bedetheque/routes.py, toutes dérivées de _convert_volume_to_cbz).
async function convertVolumeToCbz(volumeId, evt, format) {
    const button = evt.currentTarget;
    if (!confirm(`Convertir ce tome ${format} en cbz ? Le fichier ${format} original sera remplacé.`)) return;

    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';
    setVolumeActionsGearBusy(volumeId, true);

    try {
        const response = await fetch(`/api/bedetheque/convert-${format}/${volumeId}`, { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            button.innerHTML = '✅';
            const seriesId = currentSeriesDetail ? currentSeriesDetail.id : null;
            if (seriesId) {
                await renderSeriesDetail(seriesId);
            } else {
                // Voir le commentaire équivalent dans updateVolumeMetadataFromBedetheque:
                // pas de re-rendu possible ici, remettre ce bouton précis à son état normal
                button.innerHTML = originalHtml;
                button.disabled = false;
                setVolumeActionsGearBusy(volumeId, false);
            }
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            button.innerHTML = originalHtml;
            button.disabled = false;
            setVolumeActionsGearBusy(volumeId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        button.innerHTML = originalHtml;
        button.disabled = false;
        setVolumeActionsGearBusy(volumeId, false);
    }
}

// Relit taille/nombre de pages depuis le fichier réel sur disque pour CE tome (voir
// POST /api/volumes/<id>/refresh) - "seuls #16 indique size 1B. ajoute un bouton
// actualiser", pour un fichier corrigé/remplacé après coup sans qu'un scan de la série
// n'ait eu lieu depuis. Même pattern UI que convertVolumeToCbz.
async function refreshVolumeFromDisk(volumeId, evt) {
    const button = evt.currentTarget;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳';
    setVolumeActionsGearBusy(volumeId, true);

    try {
        const response = await fetch(`/api/volumes/${volumeId}/refresh`, { method: 'POST' });
        const data = await response.json();

        if (data.success) {
            button.innerHTML = '✅';
            const seriesId = currentSeriesDetail ? currentSeriesDetail.id : null;
            if (seriesId) {
                await renderSeriesDetail(seriesId);
            } else {
                button.innerHTML = originalHtml;
                button.disabled = false;
                setVolumeActionsGearBusy(volumeId, false);
            }
        } else {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            button.innerHTML = originalHtml;
            button.disabled = false;
            setVolumeActionsGearBusy(volumeId, false);
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        button.innerHTML = originalHtml;
        button.disabled = false;
        setVolumeActionsGearBusy(volumeId, false);
    }
}

// Renseigné par les boutons "Bédéthèque/EBDZ/Komga" de la modale "Éditer manuellement"
// (voir renderManualEditModal): permet au bouton "← Retour à l'édition" de ces trois
// modales de matching de rouvrir la modale d'édition là où l'utilisateur l'avait
// laissée, plutôt que de simplement se fermer. null quand une de ces modales est ouverte
// depuis ailleurs (fiche série classique, hub de la grille bibliothèque...), auquel cas
// aucun bouton retour n'a de sens - un seul et même flag pour les trois car une seule de
// ces modales peut être ouverte à la fois.
let matchModalReturnToEdit = null;

function _matchModalBackButtonHtml() {
    return matchModalReturnToEdit
        ? `<button class="btn-neutral" onclick="backToManualEditFromMatch()" style="margin-bottom:12px;">← Retour à l'édition</button>`
        : '';
}

function backToManualEditFromMatch() {
    const returnTo = matchModalReturnToEdit;
    if (!returnTo) return;
    closeBedethequeMatchModal();
    closeEbdzMatchModal();
    closeKomgaMatchModal();
    openManualEditModal(returnTo.seriesId, returnTo.volumeId);
}

// ===== MATCHING MANUEL BEDETHEQUE (recherche par titre ou URL directe de la fiche) =====
// Ouvert quand une mise à jour de métadonnées échoue faute de correspondance automatique;
// `onMatched` est rappelée une fois le match enregistré, pour relancer l'action en attente
async function openBedethequeMatchModal(seriesId, onMatched, returnToEdit = null) {
    const s = resolveSeriesForModal(seriesId);
    const modal = document.getElementById('bedetheque-match-modal');
    const body = document.getElementById('bedetheque-match-modal-body');
    modal.classList.add('active');
    modal.dataset.seriesId = seriesId;
    modal._onMatched = onMatched || null;
    matchModalReturnToEdit = returnToEdit;

    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/bedetheque-logo.png',
        title: 'Matcher manuellement sur Bedetheque',
        helpText: "Cherchez par titre ou collez directement l'URL de la fiche série Bedetheque.",
        queryId: 'bedetheque-match-query',
        queryPlaceholder: 'Entrez le nom de série ou adresse Bedetheque...',
        prefillValue: s ? s.title : '',
        resultsId: 'bedetheque-match-results',
        searchOnclick: `searchBedethequeMatchCandidates(${seriesId})`,
        autoSearch: true,
        backButtonHtml: _matchModalBackButtonHtml(),
    });

    wireMatchModalEnterKeys('bedetheque-match-query', () => searchBedethequeMatchCandidates(seriesId));
    initClearableSearchInputs(body);

    // Recherche automatique à l'ouverture, comme EBDZ/Komga - même si Bédéthèque est
    // scrapé avec un anti-bot rate-limit (~2s/requête), un utilisateur qui ouvre cette
    // modale veut voir des résultats tout de suite plutôt que de devoir cliquer
    // "Rechercher" une première fois pour le titre déjà pré-rempli (il reste libre
    // d'ajuster le titre et de relancer une recherche ensuite).
    await searchBedethequeMatchCandidates(seriesId);
}

function closeBedethequeMatchModal() {
    document.getElementById('bedetheque-match-modal').classList.remove('active');
    matchModalReturnToEdit = null;
}

async function searchBedethequeMatchCandidates(seriesId) {
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
            `selectBedethequeMatchCandidate(${seriesId}, '${escapeForAttribute(r.url)}')`,
            r.title, escapeHtml(r.genre || ''), index, 'bedetheque-match'
        )).join('');
        loadBedethequeMatchCandidateCovers(data.results, 'bedetheque-match');
    } catch (error) {
        resultsEl.innerHTML = matchModalErrorHtml(error.message);
    }
}

function selectBedethequeMatchCandidate(seriesId, url) {
    confirmBedethequeMatch(seriesId, url);
}

// Enregistre le match choisi (candidat cliqué ou URL collée) puis relance l'action qui
// avait échoué faute de correspondance (mise à jour de métadonnées série ou volume)
async function confirmBedethequeMatch(seriesId, url) {
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

        const modal = document.getElementById('bedetheque-match-modal');
        const onMatched = modal._onMatched;
        closeBedethequeMatchModal();

        if (onMatched) await onMatched();

    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Recherche un tome manquant sur EBDZ ET Prowlarr (réutilise l'endpoint combiné déjà
// utilisé par /missing-monitor plutôt que le blueprint `search` qui ne couvre que EBDZ
// et ne prenait pas en charge Prowlarr depuis la fiche série) - voir plan-sonarr-monitoring.md
// Recherche une seule source (EBDZ, Prowlarr ou Telegram) - voir searchMissingVolumeSource
// (search-results-table.js, chargé par cette page ET par Découvrir): point d'entrée
// unique, pas une fonction propre à cette page, pour que la fiche série et Découvrir
// cherchent exactement de la même façon.

function _renderNoSearchResults(seriesTitle, displayLabel, hasNumber, options, volumeNumber) {
    const searchModalBody = document.getElementById('search-modal-body');
    searchModalBody.innerHTML = `
        <div class="search-header">
            <h2>🔍 Recherche: ${escapeHtml(seriesTitle)} - ${escapeHtml(displayLabel)}</h2>
        </div>
        <div class="no-data">
            <h3>😕 Aucun résultat</h3>
            <p>Aucun lien trouvé. Vérifiez que vos sources sont configurées et activées dans Configuration → Indexeurs (EBDZ, Prowlarr, Telegram ou sources web).</p>
            ${hasNumber ? `
                <p style="margin-top: 10px; color: #666;">Essayer une recherche plus large, sur la série entière ?</p>
                <button class="btn" id="search-retry-whole-series-btn" style="margin-top: 10px;">${svgIcon('search')} Rechercher la série entière</button>
            ` : ''}
            <div style="margin-top: 16px; display: flex; gap: 8px; align-items: center; justify-content: center;">
                <input type="text" id="search-retry-custom-title" value="${escapeHtml(seriesTitle)}" placeholder="Autre terme de recherche" style="max-width: 320px;">
                <button class="btn-neutral-sm" id="search-retry-custom-btn">${svgIcon('search')} Rechercher</button>
            </div>
            <button class="btn" onclick="closeSearchModal()" style="margin-top: 20px;">Fermer</button>
        </div>
    `;
    if (hasNumber) {
        document.getElementById('search-retry-whole-series-btn').addEventListener('click', () => {
            searchMissingVolume(seriesTitle, null, options);
        });
    }
    const customInput = document.getElementById('search-retry-custom-title');
    const retryCustom = () => {
        const customTitle = customInput.value.trim();
        if (customTitle) searchMissingVolume(customTitle, volumeNumber != null ? volumeNumber : null, options);
    };
    document.getElementById('search-retry-custom-btn').addEventListener('click', retryCustom);
    customInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') retryCustom(); });
}

async function searchMissingVolume(seriesTitle, volumeNumber, options = {}) {
    const { seriesId = null, isIntegral = false, isHs = false, isEpisode = false, currentVolumeId = null } = options;

    // Tome actuellement possédé qu'on cherche à remplacer (voir "📡 Rechercher un
    // remplacement" dans buildVolumeActionIconsHtml) - récupéré par id plutôt que passé
    // en argument pour éviter d'inliner un nom de fichier potentiellement piégeux (guillemets)
    // dans l'attribut onclick qui déclenche cette fonction. null pour une recherche de tome
    // manquant (rien à comparer, pas encore possédé).
    const currentFile = currentVolumeId
        ? (currentSeriesDetail?.volumes || []).find(v => v.id === currentVolumeId) || null
        : null;

    // Pas de numéro connu (tome sans numéro sur Bédéthèque, ex: intégrale non numérotée) ->
    // recherche par titre de série seul plutôt que de bloquer: la recherche reste toujours
    // possible, elle est juste moins précise (results non filtrés par numéro de tome)
    const hasNumber = volumeNumber != null && volumeNumber > 0;
    const displayLabel = !hasNumber
        ? (isIntegral ? 'Intégrale' : (isHs ? 'Hors-série' : (isEpisode ? 'Épisode' : 'Série entière')))
        : (isIntegral ? `Intégrale ${volumeNumber}` : (isHs ? `HS ${volumeNumber}` : (isEpisode ? `Épisode ${volumeNumber}` : `Volume ${volumeNumber}`)));

    // "aussi ajouter une section recherche dans l'historique" - voir logSearchHistoryEvent, nav.js.
    if (typeof logSearchHistoryEvent === 'function') logSearchHistoryEvent(seriesTitle, displayLabel, seriesId);

    const searchModal = document.getElementById('search-ed2k-modal');
    const searchModalBody = document.getElementById('search-modal-body');

    searchModal.classList.add('active');
    searchModalBody.innerHTML = '<div class="loading"><div class="spinner"></div><p>Recherche en cours...</p></div>';

    // EBDZ (base locale, quasi instantané) affiché dès qu'il répond ; Prowlarr (recherche
    // web) et Telegram (recherche plein texte via Telethon, "je ne vois pas telegram dans
    // ... rechercher dans le volume") sont plus lents et lancés en parallèle, chacun
    // complétant la liste dès qu'il répond, au lieu d'un appel combiné qui attend
    // systématiquement le plus lent des trois avant de montrer quoi que ce soit.
    let combined = [];
    let prowlarrDone = false;
    let telegramDone = false;
    let fourtouticiDone = false;
    let annasArchiveDone = false;
    // "quand prowlarr est mis à jour dans la recherche ça reset tous mes changements" -
    // Prowlarr (le plus lent) rappelle render() après EBDZ déjà affiché; sans ce
    // drapeau, buildSearchResultsTableHtml (via displaySearchResults) recommençait
    // filtres/tri/checkbox à zéro à chaque rappel au lieu de seulement compléter la
    // liste avec les nouveaux résultats.
    let renderedOnce = false;
    const allSlowSourcesDone = () => prowlarrDone && telegramDone && fourtouticiDone && annasArchiveDone;
    const render = () => {
        if (combined.length > 0) {
            // seriesId/currentVolumeId: connus dès qu'on arrive ici depuis une fiche série
            // (voir CLAUDE.md "le volume/album doit être matché si le clic vient d'une
            // fiche série") - currentVolumeId n'est non-null que pour une recherche de
            // REMPLACEMENT (voir "📡 Rechercher un remplacement"): le nouveau fichier
            // téléchargé prendra la place de ce même tome, donc le même volume_id
            // s'applique déjà au téléchargement lui-même. Un tome MANQUANT (pas encore
            // possédé) n'a par définition pas encore de ligne `volumes` en base à cet
            // instant - seriesId seul reste transmis dans ce cas.
            displaySearchResults(seriesTitle, volumeNumber, combined, displayLabel, currentFile, !allSlowSourcesDone(), seriesId, currentVolumeId, { isIntegral, isHs, isEpisode, preserveFilters: renderedOnce });
            renderedOnce = true;
        } else if (allSlowSourcesDone()) {
            _renderNoSearchResults(seriesTitle, displayLabel, hasNumber, { seriesId, isIntegral, isHs, isEpisode }, volumeNumber);
        }
        // sinon: EBDZ vide et Prowlarr/Telegram pas encore finis - on laisse le spinner
        // initial, pas de "aucun résultat" prématuré tant qu'ils n'ont pas répondu
    };

    const ebdzPromise = searchMissingVolumeSource('ebdz', seriesTitle, volumeNumber, seriesId, isIntegral, isHs, isEpisode)
        .then(results => { combined = [...combined, ...results]; render(); });
    const prowlarrPromise = searchMissingVolumeSource('prowlarr', seriesTitle, volumeNumber, seriesId, isIntegral, isHs, isEpisode)
        .then(results => { combined = [...combined, ...results]; prowlarrDone = true; render(); });
    const telegramPromise = searchMissingVolumeSource('telegram', seriesTitle, volumeNumber, seriesId, isIntegral, isHs, isEpisode)
        .then(results => { combined = [...combined, ...results]; telegramDone = true; render(); });
    const fourtouticiPromise = searchMissingVolumeSource('fourtoutici', seriesTitle, volumeNumber, seriesId, isIntegral, isHs, isEpisode)
        .then(results => { combined = [...combined, ...results]; fourtouticiDone = true; render(); });
    const annasArchivePromise = searchMissingVolumeSource('annas_archive', seriesTitle, volumeNumber, seriesId, isIntegral, isHs, isEpisode)
        .then(results => { combined = [...combined, ...results]; annasArchiveDone = true; render(); });

    await Promise.all([ebdzPromise, prowlarrPromise, telegramPromise, fourtouticiPromise, annasArchivePromise]);
}

// Bouton EBDZ de la toolbar de la page de détail: lien direct vers le thread quand la
// série est matchée, sinon ouvre directement le matching manuel (pas de tentative
// d'automatch silencieuse: l'utilisateur choisit le thread lui-même). Reconstruit après
// chaque match/unmatch (voir refreshEbdzToolbarButton) pour ne pas rester figé sur l'un
// ou l'autre état après une action
function buildEbdzToolbarButtonHtml(seriesId, threadUrl) {
    if (threadUrl) {
        // Icône seule, pas de libellé: le lien "Ouvrir sur EBDZ" en toutes lettres existe
        // déjà dans le bloc "🔗 Liens" du header - doublon ici, mais l'icône reste un
        // repère visuel rapide (matché/pas matché) dans ce menu Métadonnées
        return `
            <a class="toolbar-btn toolbar-btn-icon-only" href="${escapeHtml(threadUrl)}" target="_blank" rel="noopener" data-tooltip="Ouvrir sur EBDZ">
                <img src="/static/img/ebdz-logo.png" alt="EBDZ" class="toolbar-btn-logo">
            </a>
        `;
    }
    return `
        <button class="toolbar-btn" onclick="openEbdzMatchModal(${seriesId}, 'modal')" data-tooltip="Matcher manuellement avec EBDZ">
            <img src="/static/img/ebdz-logo.png" alt="" class="toolbar-btn-logo"><span class="toolbar-btn-label">EBDZ</span>
        </button>
    `;
}

// Reconstruit le bouton EBDZ de la toolbar (voir buildEbdzToolbarButtonHtml) après un
// match/unmatch, pour qu'il devienne/cesse d'être un lien direct sans recharger la page
function refreshEbdzToolbarButton(seriesId, threadUrl) {
    const groupEl = document.getElementById(`ebdz-toolbar-group-${seriesId}`);
    if (!groupEl) return;
    const btnEl = groupEl.querySelector('.toolbar-btn');
    if (btnEl) btnEl.outerHTML = buildEbdzToolbarButtonHtml(seriesId, threadUrl).trim();
}

// Construit le HTML du statut de matching EBDZ (icône matché/non matché + actions)
// et, si matché, le badge "possédé/EBDZ" + volumes manquants + lien vers EBDZ.
// Réutilisé pour l'affichage permanent (données stockées en base) et la mise à jour après clic
function buildEbdzStatusHtml({ seriesId, context, ownedCount, ebdzCount, missingVolumes, threadUrl, threadId, matchStatus, matchedTitle, isOneshot }) {
    if (matchStatus === 'unmatched') {
        return `
            <span class="badge badge-warning" title="Aucun match EBDZ non-ambigu trouvé automatiquement">❌</span>
            <span class="toolbar-status-actions">
                <button class="btn" onclick="checkEbdzVolumes(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em;" title="Réessayer un matching automatique">${svgIcon('refresh-cw')}</button>
                <button class="btn" onclick="openEbdzMatchModal(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em; background: #8b5cf6;" title="Matcher manuellement sur EBDZ">${svgIcon('search')}</button>
            </span>
        `;
    }

    if (ebdzCount === null || ebdzCount === undefined) {
        return '';
    }

    // Le bouton EBDZ de la toolbar est maintenant le lien direct vers le thread (quand il
    // existe): le rafraîchissement du matching se fait ici, dans les actions au survol
    const rematchHtml = `
        <span class="toolbar-status-actions">
            <button class="btn" onclick="rescrapeEbdzThread(${seriesId}, this)" style="padding: 4px 8px; font-size: 0.75em;" title="Rescraper ce thread EBDZ">${svgIcon('download')}</button>
            <button class="btn" onclick="checkEbdzVolumes(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em;" title="Recomparer avec le cache EBDZ">${svgIcon('refresh-cw')}</button>
            <button class="btn" onclick="openEbdzMatchModal(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em;" title="Changer le match EBDZ">${svgIcon('pencil')}</button>
            <button class="btn" onclick="unmatchEbdzSeries(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em; background: #ef4444;" title="Retirer le match EBDZ">${svgIcon('ban')}</button>
        </span>
    `;

    // Le badge ratio possédé/EBDZ ouvre une modale listant les fichiers ed2k trouvés sur
    // le thread EBDZ matché (uniquement possible dans le contexte "modal" de la page de
    // détail, où cette modale existe dans le DOM, et si un thread est bien identifié)
    const canBrowseFiles = context === 'modal' && threadId;
    const clickAttr = canBrowseFiles ? ` style="cursor: pointer;" onclick="openEbdzFilesModal(${seriesId}, ${threadId})"` : '';

    if (ebdzCount === 0) {
        return `<span class="badge"${clickAttr} title="${matchedTitle ? escapeHtml(matchedTitle) : ''}${canBrowseFiles ? ' — cliquer pour voir les fichiers EBDZ' : ''}"><img src="/static/img/ebdz-logo.png" alt="" class="badge-logo"> ${ownedCount}/0</span> ${rematchHtml}`;
    }

    // Sur un one-shot, "manquant/complet vs EBDZ" n'a pas de sens (pas de numérotation
    // de tomes à comparer): le ratio seul suffit
    const hasMissing = !isOneshot && missingVolumes && missingVolumes.length > 0;
    const ratioTitle = (matchedTitle ? escapeHtml(matchedTitle) : 'Série matchée sur EBDZ')
        + (hasMissing ? ` — ${pluralize(missingVolumes.length, 'manquant')}: ${missingVolumes.join(', ')}` : '')
        + (canBrowseFiles ? ' — cliquer pour voir les fichiers EBDZ' : '');

    return `<span class="badge ${hasMissing ? 'badge-warning' : 'badge-success'}"${clickAttr} title="${ratioTitle}"><img src="/static/img/ebdz-logo.png" alt="" class="badge-logo"> ${ownedCount}/${ebdzCount}</span> ${rematchHtml}`;
}

// Compare les volumes possédés d'une série au nombre d'entrées uniques trouvées sur EBDZ,
// persiste le résultat en base et affiche le badge (icône ratio + volumes manquants)
// dans le conteneur ciblé
async function checkEbdzVolumes(seriesId, context) {
    const containerId = context === 'modal' ? `ebdz-status-modal-${seriesId}` : `ebdz-status-card-${seriesId}`;
    const container = document.getElementById(containerId);
    if (!container) return;

    container.innerHTML = '<span class="badge">⏳ Comparaison EBDZ...</span>';

    try {
        const response = await fetch(`/api/series/${seriesId}/ebdz-enrich`, { method: 'POST' });
        const data = await response.json();

        if (!data.success) {
            container.innerHTML = `<span class="badge badge-warning">❌ ${escapeHtml(data.error || 'Erreur inconnue')}</span>`;
            return;
        }

        container.innerHTML = buildEbdzStatusHtml({
            seriesId, context, ownedCount: data.owned_count, ebdzCount: data.ebdz_count,
            missingVolumes: data.missing_volumes, threadUrl: data.ebdz_thread_url, threadId: data.ebdz_thread_id,
            matchStatus: data.match_status, matchedTitle: data.matched_title, isOneshot: isSeriesOneshot(seriesId)
        });
        if (context === 'modal') refreshEbdzToolbarButton(seriesId, data.ebdz_thread_url);

        // Garder le cache local en phase avec la valeur persistée en base,
        // pour que les re-rendus (filtres, tri) affichent toujours le dernier résultat
        updateCachedEbdzStatus(seriesId, data);
    } catch (error) {
        container.innerHTML = `<span class="badge badge-warning">❌ ${escapeHtml(error.message)}</span>`;
    }
}

// Met à jour le cache local seriesData avec le dernier statut EBDZ connu pour une série
function updateCachedEbdzStatus(seriesId, data) {
    const cached = seriesData.find(item => item.id === seriesId);
    if (!cached) return;
    cached.ebdz_volumes_count = data.ebdz_count ?? null;
    cached.ebdz_missing_volumes = data.missing_volumes || [];
    cached.ebdz_thread_url = data.ebdz_thread_url ?? null;
    cached.ebdz_match_status = data.match_status ?? null;
    cached.ebdz_matched_title = data.matched_title ?? null;
    cached.ebdz_thread_id = data.ebdz_thread_id ?? null;
    refreshSeriesItem(seriesId);
}

// seriesData (grille de library.html) n'est pas peuplé sur la page dédiée
// series-detail.html: on retombe alors sur currentSeriesDetail, déjà chargé pour la
// série affichée, pour ne pas laisser le champ de recherche vide dans ce contexte.
// Partagé par les modales de matching manuel EBDZ et Komga
function resolveSeriesForModal(seriesId) {
    return seriesData.find(item => item.id === seriesId)
        || (currentSeriesDetail && currentSeriesDetail.id === seriesId ? currentSeriesDetail : null);
}

// Modale "hub" façon Sonarr (bouton clé à molette de l'overlay au survol d'une affiche):
// point d'entrée unique vers les 3 actions d'édition d'une série plutôt que de dupliquer
// leurs boutons directement dans l'overlay, qui n'a la place que pour quelques icônes
// "dans one-shot met supprimer le fichier avant supprime. renomme le dernier supprimer
// par supprimer la série" - cette modale "hub" (grille/affiches) n'avait jusqu'ici QUE
// "Supprimer" (deleteSeries, fichier + fiche en un seul geste irréversible), contrairement
// au menu ⚙️ complet de la fiche série qui distingue déjà "Supprimer le fichier"
// (deleteVolume, garde la fiche comme tome manquant) de "Supprimer" (la série entière) -
// voir son commentaire ("2 étapes distinctes") plus bas dans ce fichier. Cette modale
// n'a que la liste légère seriesData/currentSeriesDetail (sans le détail des tomes) - un
// aller simple sur /api/series/<id> UNIQUEMENT pour un one-shot (jamais pour une série à
// plusieurs tomes, qui n'a pas besoin de cette distinction ici) retrouve le fichier unique
// à proposer séparément.
async function openSeriesEditModal(seriesId) {
    const s = resolveSeriesForModal(seriesId);
    const title = s ? s.title : '';

    let oneshotFile = null;
    if (s && s.is_oneshot) {
        try {
            const detail = await (await fetch(`/api/series/${seriesId}`)).json();
            oneshotFile = (detail.volumes || []).find(v => v.filepath) || null;
        } catch (e) { /* best-effort - repli sur "Supprimer la série" seule */ }
    }

    let modal = document.getElementById('series-edit-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'series-edit-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }

    const deleteFileHtml = oneshotFile ? `
                <button class="series-edit-action series-edit-action-danger" onclick="closeSeriesEditModal(); deleteVolume(${oneshotFile.id}, ${seriesId}, '${escapeForAttribute(title)}')" title="Supprime uniquement le fichier - la fiche de cette série reste, pour supprimer aussi la série elle-même utilisez 'Supprimer la série' ci-dessous">
                    <span class="series-edit-action-icon">${svgIcon('trash-2')}</span><span class="series-edit-action-label">Supprimer le fichier</span>
                </button>
    ` : '';

    // "dans editer mets l'icone de la serie... le cover de la serie" - même helper que
    // la grille/aperçu/tableau (buildCoverHtml), pour confirmer visuellement QUELLE série
    // on est en train d'éditer plutôt que le seul titre en texte. `s` peut être null (série
    // pas trouvée dans seriesData/currentSeriesDetail, cas limite) - pas de couverture
    // dans ce cas plutôt que de planter sur pickPosterCoverPath(null).
    const coverHtml = s ? buildCoverHtml(s, 'series-edit-modal-cover', 'series-edit-modal-cover-placeholder') : '';

    modal.innerHTML = `
        <div class="modal-content series-edit-modal-content">
            <span class="close-modal" onclick="closeSeriesEditModal()">×</span>
            <div class="series-edit-modal-header">
                ${coverHtml}
                <div class="series-edit-modal-header-text">
                    <h2 class="modal-title series-edit-modal-title">${svgIcon('settings')} Éditer</h2>
                    <p class="series-edit-modal-subtitle">${escapeHtml(title)}</p>
                </div>
            </div>
            <div class="series-edit-actions">
                <button class="series-edit-action" onclick="closeSeriesEditModal(); openRenameModal(${seriesId}, '${escapeForAttribute(title)}')">
                    <span class="series-edit-action-icon">${svgIcon('pencil')}</span><span class="series-edit-action-label">Renommer</span>
                </button>
                <button class="series-edit-action" onclick="closeSeriesEditModal(); openMergeSeriesModal(${seriesId})">
                    <span class="series-edit-action-icon">${svgIcon('git-merge')}</span><span class="series-edit-action-label">Fusionner</span>
                </button>
                <button class="series-edit-action" onclick="closeSeriesEditModal(); openEbdzMatchModal(${seriesId}, 'card')">
                    <img src="/static/img/ebdz-logo.png" alt="" class="series-edit-action-icon-img"><span class="series-edit-action-label">Matcher EBDZ</span>
                </button>
                <button class="series-edit-action" onclick="closeSeriesEditModal(); openBedethequeMatchModal(${seriesId}, () => refreshSeriesView(${seriesId}))">
                    <img src="/static/img/bedetheque-logo.png" alt="" class="series-edit-action-icon-img"><span class="series-edit-action-label">Matcher Bédéthèque</span>
                </button>
                <button class="series-edit-action" onclick="closeSeriesEditModal(); openKomgaMatchModal(${seriesId}, 'card')">
                    <img src="/static/img/komga-logo.svg" alt="" class="series-edit-action-icon-img"><span class="series-edit-action-label">Matcher Komga</span>
                </button>
                ${deleteFileHtml}
                <button class="series-edit-action series-edit-action-danger" onclick="closeSeriesEditModal(); deleteSeries(${seriesId})">
                    <span class="series-edit-action-icon">${svgIcon('trash-2')}</span><span class="series-edit-action-label">Supprimer la série</span>
                </button>
            </div>
        </div>
    `;
    modal.classList.add('active');
}

function closeSeriesEditModal() {
    const modal = document.getElementById('series-edit-modal');
    if (modal) modal.classList.remove('active');
}

// "j'aimerai que tu ajoutes un fiche histoire par serie... fichier telecharge, nom
// d'origine, clients, supprimer, renommer" - même 3 sources/le même rendu que la page
// Recommandations éditoriales « A lire aussi » de la fiche Bédéthèque. Les URL sont
// issues du cache serveur et `series_id` est recalculé à chaque chargement de la fiche,
// donc le statut possédé/non possédé reste exact sans refaire le scraping.
function closeReadAlsoModal() {
    const modal = document.getElementById('bedetheque-read-also-modal');
    if (modal) modal.classList.remove('active');
}

async function openReadAlsoModal(seriesId, seriesTitle) {
    const modal = document.getElementById('bedetheque-read-also-modal');
    const body = document.getElementById('bedetheque-read-also-modal-body');
    let items = (currentSeriesDetail && currentSeriesDetail.id === seriesId
        ? (currentSeriesDetail.bedetheque?.read_also || []) : []);
    if (!modal || !body) return;

    // Les séries matchées avant l'introduction du cache n'ont pas encore de liste.
    // On la récupère uniquement au clic, puis l'API la persiste pour les fois suivantes.
    if (!items.length && currentSeriesDetail?.bedetheque?.url) {
        modal.classList.add('active');
        body.innerHTML = '<div class="loading"><div class="spinner"></div><p>Chargement des recommandations...</p></div>';
        try {
            const response = await fetch('/api/bedetheque/read-also/' + seriesId);
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'Erreur de chargement');
            items = data.items || [];
            currentSeriesDetail.bedetheque.read_also = items;
        } catch (error) {
            body.innerHTML = '<div class="error-box">Erreur : ' + escapeHtml(error.message) + '</div>';
            return;
        }
    }

    body.innerHTML = `
        <h2 class="modal-title modal-title-with-logo">
            <img src="/static/img/bedetheque-logo.png" alt="" class="modal-title-logo">
            À lire aussi
        </h2>
        <p class="modal-help-text">Sélection de Bédéthèque pour les lecteurs de « ${escapeHtml(seriesTitle)} ».</p>
        <div class="author-albums-list read-also-list">
            ${items.length ? items.map((item, index) => `
                <div class="overview-row author-album-row${item.series_id ? ' author-album-row-owned' : ''}" data-read-also-index="${index}">
                    ${item.cover_url
                        ? `<img class="cover-thumb-overview" src="${escapeHtml(item.cover_url)}" alt="" loading="lazy" onerror="this.style.display='none'">`
                        : '<span class="cover-thumb-overview poster-placeholder-mini">📚</span>'}
                    <div class="overview-info">
                        <div class="overview-title">${escapeHtml(item.title)}</div>
                        <div class="overview-tags">
                            ${item.series_id
                                ? `<span class="overview-tag author-album-owned-tag">${svgIcon('check')} En bibliothèque</span>`
                                : '<span class="overview-tag">Pas dans la bibliothèque</span>'}
                        </div>
                    </div>
                    <div class="overview-actions">
                        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener" class="poster-action-btn poster-action-btn-light" data-tooltip="Voir sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:16px;height:16px;vertical-align:middle;"></a>
                        ${item.read_also_added
                            ? `<button type="button" class="poster-action-btn poster-action-btn-light read-also-added-btn" disabled data-tooltip="Série ajoutée" aria-label="Série ajoutée">Ajouté</button>`
                            : item.series_id
                                ? `<a href="/series/${item.series_id}" class="poster-action-btn poster-action-btn-light" data-tooltip="Ouvrir dans la bibliothèque">${svgIcon('folder')}</a>`
                                : `<button type="button" class="poster-action-btn poster-action-btn-light read-also-add-btn" onclick="addReadAlsoSeries(${index}, ${seriesId}, this)" data-tooltip="Ajouter dans la bibliothèque" aria-label="Ajouter ${escapeForAttribute(item.title)}">${svgIcon('plus')}</button>`}
                    </div>
                </div>
            `).join('') : '<div class="no-data"><p>Aucune recommandation trouvée sur cette fiche Bédéthèque.</p></div>'}
        </div>
    `;
    modal.classList.add('active');
}

async function addReadAlsoSeries(itemIndex, viewingSeriesId, button) {
    const items = currentSeriesDetail?.bedetheque?.read_also || [];
    const item = items[itemIndex];
    if (!item || !button) return;
    button.disabled = true;
    showToast('read-also-add', `⏳ Ajout de « ${item.title} »...`, { icon: 'loader-circle' });
    try {
        const response = await fetch('/api/bedetheque/add-series', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: item.url, library_id: currentSeriesDetail.library.id })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur lors de l’ajout');
        item.series_id = data.series_id;
        // Conserver explicitement l'état « Ajouté » lors du rerendu de la modale :
        // sinon le bouton + était immédiatement remplacé par l'icône dossier, ce qui
        // ne permettait pas de voir que l'action venait d'être effectuée.
        item.read_also_added = true;
        showToast('read-also-add', `✅ « ${item.title} » est maintenant dans la bibliothèque`, { icon: 'check', autoHideMs: 4500, href: `/series/${data.series_id}` });
        if (data.auto_acquire_started) {
            showToast('read-also-auto-acquire', `🔎 Recherche automatique lancée pour « ${item.title} »`, { icon: 'search', autoHideMs: 6000 });
        }
        openReadAlsoModal(viewingSeriesId, currentSeriesDetail.title);
    } catch (error) {
        button.disabled = false;
        showToast('read-also-add', `❌ « ${item.title} » : ${error.message}`, { icon: 'circle-x', autoHideMs: 5000 });
    }
}

// /history générale (imports, téléchargements, actions - voir mapHistoryResponsesToEvents/
// historyEventRowHtml, history-shared.js), filtrées à cette seule série côté serveur
// (?series_id=) plutôt qu'une vue/un rendu spécifique à réinventer ("dans l'historique
// utilise bien le meme style UI/couleur que l'application").
async function openSeriesHistoryModal(seriesId, seriesTitle) {
    let modal = document.getElementById('series-history-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'series-history-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = `
        <div class="modal-content" style="max-width: 900px;">
            <span class="close-modal" onclick="closeSeriesHistoryModal()">×</span>
            <h2 class="modal-title">${svgIcon('history')} Historique</h2>
            <p class="modal-subtitle">${escapeHtml(seriesTitle)}</p>
            <div id="series-history-body"><div class="loading"><div class="spinner"></div></div></div>
        </div>
    `;
    modal.classList.add('active');

    try {
        const [importResp, downloadResp, actionResp] = await Promise.all([
            fetch(`/api/import/history?limit=100&series_id=${seriesId}`).then(r => r.json()).catch(() => ({ history: [] })),
            fetch(`/api/missing-monitor/history?limit=100&series_id=${seriesId}`).then(r => r.json()).catch(() => ({ history: [] })),
            fetch(`/api/actions/history?limit=100&series_id=${seriesId}`).then(r => r.json()).catch(() => ({ history: [] }))
        ]);
        const events = mapHistoryResponsesToEvents(importResp, downloadResp, actionResp);
        const body = document.getElementById('series-history-body');
        if (!body) return;
        if (events.length === 0) {
            body.innerHTML = '<p class="help-text">Aucun historique pour cette série.</p>';
            return;
        }
        body.innerHTML = `
            <div class="series-list series-table-wrapper">
                <table class="series-table series-table-compact">
                    <thead>
                        <tr>
                            <th style="white-space:nowrap;">Date</th>
                            <th style="text-align:center;">Type</th>
                            <th>Détails</th>
                            <th style="text-align:center;">Statut</th>
                        </tr>
                    </thead>
                    <tbody>${events.map((e, i) => historyEventRowHtml(e, i, 'series-history-row', true)).join('')}</tbody>
                </table>
            </div>
        `;
    } catch (error) {
        const body = document.getElementById('series-history-body');
        if (body) body.innerHTML = `<p class="help-text">Erreur : ${escapeHtml(error.message)}</p>`;
    }
}

function closeSeriesHistoryModal() {
    const modal = document.getElementById('series-history-modal');
    if (modal) modal.classList.remove('active');
}

// "dans panthéon tu as les icones des auteurs. c'est possible de les récupérer en
// static pour les afficher dans les pages de serie" puis "add it" puis "but only when
// adding a new serie" - remplit après coup les slots .author-photo-slot posés par
// _authorNamesHtml (renderSeriesDetail) avec ce qui est DÉJÀ en cache, sans jamais
// déclencher de scraping depuis cette page: le seul déclencheur de scraping est
// l'ajout de série (voir _start_author_photos_fetch_thread côté Flask). GET (pas de
// body) plutôt que POST, cohérent avec une route qui ne fait plus qu'une lecture DB.
async function loadSeriesAuthorPhotos(authorLinks) {
    const urls = [...new Set(Object.values(authorLinks || {}).filter(Boolean))];
    if (urls.length === 0) return;
    try {
        const qs = urls.map(u => 'url=' + encodeURIComponent(u)).join('&');
        const data = await (await fetch('/api/bedetheque/authors/photos?' + qs)).json();
        if (!data.success) return;
        const photos = data.photos || {};
        document.querySelectorAll('.author-photo-slot').forEach(slot => {
            const path = photos[slot.dataset.authorUrl];
            if (path) slot.innerHTML = `<img src="/${path}" alt="" class="author-photo-avatar">`;
        });
    } catch (e) { /* best-effort - une photo manquante n'affecte que l'affichage */ }
}

// ===== FUSION DE SÉRIES =====
// Fusionne une série (source) dans une autre de la même bibliothèque: tous ses tomes
// sont déplacés dans le dossier de la série cible et la fiche source disparaît (la
// cible garde ses propres matchings EBDZ/Komga/Bédéthèque). Utile quand un
// import a créé deux séries pour la même œuvre (variantes de nommage des dossiers).
async function openMergeSeriesModal(seriesId) {
    let modal = document.getElementById('merge-series-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'merge-series-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = '<div class="modal-content"><div class="loading"><div class="spinner"></div></div></div>';
    modal.classList.add('active');

    try {
        const source = await (await fetch(`/api/series/${seriesId}`)).json();
        const seriesList = await (await fetch(`/api/library/${source.library.id}/series`)).json();
        const candidates = seriesList.filter(s => s.id !== seriesId);

        modal.innerHTML = `
            <div class="modal-content" style="max-width: 700px;">
                <span class="close-modal" onclick="closeMergeSeriesModal()">×</span>
                <h2 class="modal-title" style="margin-bottom: 10px;">🔀 Fusionner la série</h2>
                <p class="modal-subtitle">Tous les tomes de <strong>${escapeHtml(source.title)}</strong> seront déplacés
                    dans la série choisie ci-dessous, puis la fiche « ${escapeHtml(source.title)} » sera supprimée
                    (la série choisie garde ses matchings).</p>
                <input type="text" id="merge-series-filter" class="search-box" placeholder="Filtrer les séries..."
                       style="width: 100%; margin-bottom: 12px;">
                <div id="merge-series-candidates" style="max-height: 50vh; overflow-y: auto;"></div>
            </div>
        `;

        const candidatesEl = document.getElementById('merge-series-candidates');
        const filterInput = document.getElementById('merge-series-filter');

        const renderCandidates = () => {
            // "les filtres de l'application ne devrait pas avoir a différencier les
            // accents" - navNormalizeSearch (nav.js) plutôt qu'un simple toLowerCase().
            const needle = navNormalizeSearch(filterInput.value.trim());
            const matches = candidates.filter(c => navNormalizeSearch(c.title).includes(needle)).slice(0, 50);
            candidatesEl.innerHTML = matches.length ? matches.map(c => `
                <div class="series-card" style="cursor: pointer; margin-bottom: 8px;"
                     onclick="confirmMergeSeries(${seriesId}, '${escapeForAttribute(source.title)}', ${c.id}, '${escapeForAttribute(c.title)}')">
                    <div class="series-title">${escapeHtml(c.title)}</div>
                    <div class="series-info">${c.total_volumes || 0} album${(c.total_volumes || 0) > 1 ? 's' : ''}</div>
                </div>
            `).join('') : '<div class="no-data"><p>😕 Aucune série ne correspond</p></div>';
        };

        filterInput.addEventListener('input', renderCandidates);

        // Pré-filtre avec le début du titre source: les doublons à fusionner sont
        // presque toujours des variantes de nommage de la même œuvre, la cible remonte
        // donc directement. Si ce pré-filtre ne remonte rien, on liste tout.
        const prefix = source.title.split(/[(,\-]/)[0].trim().split(/\s+/).slice(0, 2).join(' ');
        filterInput.value = prefix;
        if (!candidates.some(c => navNormalizeSearch(c.title).includes(navNormalizeSearch(prefix)))) {
            filterInput.value = '';
        }
        renderCandidates();
        filterInput.focus();
    } catch (error) {
        modal.innerHTML = `
            <div class="modal-content">
                <span class="close-modal" onclick="closeMergeSeriesModal()">×</span>
                <div class="no-data"><p>❌ ${escapeHtml(error.message)}</p></div>
            </div>
        `;
    }
}

function closeMergeSeriesModal() {
    const modal = document.getElementById('merge-series-modal');
    if (modal) modal.classList.remove('active');
}

async function confirmMergeSeries(sourceId, sourceTitle, targetId, targetTitle) {
    if (!confirm(`Fusionner "${sourceTitle}" dans "${targetTitle}" ?\n\n` +
                 `Tous les tomes seront déplacés dans le dossier de "${targetTitle}" ` +
                 `et la série "${sourceTitle}" sera supprimée. Cette action ne peut pas être annulée.`)) {
        return;
    }

    const candidatesEl = document.getElementById('merge-series-candidates');
    candidatesEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>Fusion en cours...</p></div>';

    try {
        const response = await fetch(`/api/series/${sourceId}/merge`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_series_id: targetId })
        });
        const data = await response.json();

        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            closeMergeSeriesModal();
            return;
        }

        alert(`✅ ${data.moved} ${pluralize(data.moved, 'tome')} ${pluralize(data.moved, 'déplacé')} dans "${targetTitle}"`);
        // La série source n'existe plus: on navigue vers la fiche de la série cible
        window.location.href = `/series/${targetId}`;
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        closeMergeSeriesModal();
    }
}

// "can you change edit to be able to move albums to another serie" - déplace UN tome
// (pas toute la série, voir openMergeSeriesModal ci-dessus dont ce flux reprend le même
// principe de recherche/liste de candidats) vers une autre série de la même
// bibliothèque. Ouvert depuis le bouton "Déplacer vers une autre série" de la vue
// single-tome de l'édition manuelle (buildManualEditVolumeFieldsHtml).
async function openMoveVolumeModal(seriesId, volumeId, volumeLabel) {
    let modal = document.getElementById('move-volume-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'move-volume-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = '<div class="modal-content"><div class="loading"><div class="spinner"></div></div></div>';
    modal.classList.add('active');

    try {
        const source = await (await fetch(`/api/series/${seriesId}`)).json();
        const seriesList = await (await fetch(`/api/library/${source.library.id}/series`)).json();
        const candidates = seriesList.filter(s => s.id !== seriesId);

        modal.innerHTML = `
            <div class="modal-content" style="max-width: 700px;">
                <span class="close-modal" onclick="closeMoveVolumeModal()">×</span>
                <h2 class="modal-title" style="margin-bottom: 10px;">🔀 Déplacer ce tome</h2>
                <p class="modal-subtitle"><strong>${escapeHtml(volumeLabel)}</strong> sera déplacé dans le dossier de la
                    série choisie ci-dessous et rattaché à sa fiche (« ${escapeHtml(source.title)} » garde ses autres tomes).</p>
                <input type="text" id="move-volume-filter" class="search-box" placeholder="Filtrer les séries..."
                       style="width: 100%; margin-bottom: 12px;">
                <div id="move-volume-candidates" style="max-height: 50vh; overflow-y: auto;"></div>
            </div>
        `;

        const candidatesEl = document.getElementById('move-volume-candidates');
        const filterInput = document.getElementById('move-volume-filter');

        const renderCandidates = () => {
            // "les filtres de l'application ne devrait pas avoir a différencier les
            // accents" - navNormalizeSearch (nav.js) plutôt qu'un simple toLowerCase().
            const needle = navNormalizeSearch(filterInput.value.trim());
            const matches = candidates.filter(c => navNormalizeSearch(c.title).includes(needle)).slice(0, 50);
            candidatesEl.innerHTML = matches.length ? matches.map(c => `
                <div class="series-card" style="cursor: pointer; margin-bottom: 8px;"
                     onclick="confirmMoveVolume(${volumeId}, '${escapeForAttribute(volumeLabel)}', ${seriesId}, ${c.id}, '${escapeForAttribute(c.title)}')">
                    <div class="series-title">${escapeHtml(c.title)}</div>
                    <div class="series-info">${c.total_volumes || 0} album${(c.total_volumes || 0) > 1 ? 's' : ''}</div>
                </div>
            `).join('') : '<div class="no-data"><p>😕 Aucune série ne correspond</p></div>';
        };

        filterInput.addEventListener('input', renderCandidates);
        renderCandidates();
        filterInput.focus();
    } catch (error) {
        modal.innerHTML = `
            <div class="modal-content">
                <span class="close-modal" onclick="closeMoveVolumeModal()">×</span>
                <div class="no-data"><p>❌ ${escapeHtml(error.message)}</p></div>
            </div>
        `;
    }
}

function closeMoveVolumeModal() {
    const modal = document.getElementById('move-volume-modal');
    if (modal) modal.classList.remove('active');
}

async function confirmMoveVolume(volumeId, volumeLabel, sourceSeriesId, targetSeriesId, targetTitle) {
    if (!confirm(`Déplacer "${volumeLabel}" vers "${targetTitle}" ?`)) return;

    const candidatesEl = document.getElementById('move-volume-candidates');
    candidatesEl.innerHTML = '<div class="loading"><div class="spinner"></div><p>Déplacement en cours...</p></div>';

    try {
        const response = await fetch(`/api/volumes/${volumeId}/move-to-series`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_series_id: targetSeriesId })
        });
        const data = await response.json();

        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            closeMoveVolumeModal();
            return;
        }

        closeMoveVolumeModal();
        closeManualEditModal();
        // Le tome vient de quitter la série actuellement affichée - rafraîchit sa fiche
        // (même garde "modal-body existe" que deleteVolume/toggleOneshot ci-dessous).
        if (document.getElementById('modal-body') && currentSeriesDetail && currentSeriesDetail.id === sourceSeriesId) {
            await renderSeriesDetail(sourceSeriesId);
        }
        if (typeof loadLibraryData === 'function') loadLibraryData();
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        closeMoveVolumeModal();
    }
}

// Rafraîchit la fiche série ouverte (#modal-body n'existe que sur la page de détail) ou,
// si ce hub a été ouvert depuis l'overlay au survol d'une affiche de la grille, la grille
// elle-même - même garde que scanSeries pour éviter l'erreur "Cannot set properties of
// null" quand ce callback est déclenché depuis la grille plutôt que la fiche détail
function refreshSeriesView(seriesId) {
    if (document.getElementById('modal-body')) {
        renderSeriesDetail(seriesId);
    } else if (typeof loadLibraryData === 'function') {
        loadLibraryData();
    }
}

// Ouvre la modale de matching manuel EBDZ pour une série, pré-remplie avec son titre
async function openEbdzMatchModal(seriesId, context, returnToEdit = null) {
    const s = resolveSeriesForModal(seriesId);
    const modal = document.getElementById('ebdz-match-modal');
    const body = document.getElementById('ebdz-match-modal-body');
    modal.classList.add('active');
    modal.dataset.seriesId = seriesId;
    modal.dataset.context = context;
    matchModalReturnToEdit = returnToEdit;

    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/ebdz-logo.png',
        title: 'Matcher manuellement sur EBDZ',
        queryId: 'ebdz-match-query',
        queryPlaceholder: 'Titre à rechercher sur EBDZ, ou URL directe du thread...',
        prefillValue: s ? s.title : '',
        resultsId: 'ebdz-match-results',
        searchOnclick: `searchEbdzMatchCandidates(${seriesId})`,
        autoSearch: true,
        backButtonHtml: _matchModalBackButtonHtml(),
    });

    wireMatchModalEnterKeys('ebdz-match-query', () => searchEbdzMatchCandidates(seriesId));
    initClearableSearchInputs(body);

    await searchEbdzMatchCandidates(seriesId);
}

function closeEbdzMatchModal() {
    document.getElementById('ebdz-match-modal').classList.remove('active');
    matchModalReturnToEdit = null;
}

// Recherche des threads EBDZ candidats pour la série en cours de matching et les affiche.
// Un seul champ pour titre ET URL directe ("je n'ai besoin que d'une seule entrée (nom et
// URL)") - même principe que le champ Bédéthèque (openBedethequeMatchModal), qui n'a
// jamais eu de second champ séparé: si la saisie ressemble à une URL, on confirme
// directement le match plutôt que de la chercher comme un titre (remplace l'ancien champ
// urlFallback + submitEbdzMatchUrl).
async function searchEbdzMatchCandidates(seriesId) {
    const resultsEl = document.getElementById('ebdz-match-results');
    const query = document.getElementById('ebdz-match-query').value.trim();

    if (/^https?:\/\//i.test(query)) {
        await confirmEbdzMatch(seriesId, { thread_url: query });
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
            `selectEbdzCandidate(${seriesId}, ${c.thread_id})`,
            c.thread_title,
            `📁 ${escapeHtml(c.forum_category || '')} • ${c.file_count} ${pluralize(c.file_count, 'fichier')}${c.volumes.length ? ` • Volumes: ${c.volumes.join(', ')}` : ''}`
        )).join('');
    } catch (error) {
        resultsEl.innerHTML = matchModalErrorHtml(error.message);
    }
}

// Confirme le matching manuel d'une série avec un thread EBDZ précis choisi par l'utilisateur
function selectEbdzCandidate(seriesId, threadId) {
    return confirmEbdzMatch(seriesId, { thread_id: threadId });
}

// Confirme le matching manuel d'une série avec un thread EBDZ précis, identifié soit par
// son thread_id (candidat cliqué dans les résultats de recherche) soit par thread_url
// (URL collée directement, voir submitEbdzMatchUrl)
async function confirmEbdzMatch(seriesId, payload) {
    const modal = document.getElementById('ebdz-match-modal');
    const context = modal.dataset.context || 'card';

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
        updateCachedEbdzStatus(seriesId, data);

        // context 'modal' = fiche série (soit via son propre menu Métadonnées, soit via
        // les boutons Matcher de "Éditer manuellement") - un rafraîchissement complet
        // plutôt qu'un patch ciblé du seul badge de statut: matcher/rematcher EBDZ peut
        // aussi changer les volumes manquants détectés, pas seulement l'icône de statut
        if (context === 'modal') {
            await renderSeriesDetail(seriesId);
            return;
        }

        const container = document.getElementById(`ebdz-status-card-${seriesId}`);
        if (container) {
            container.innerHTML = buildEbdzStatusHtml({
                seriesId, context, ownedCount: data.owned_count, ebdzCount: data.ebdz_count,
                missingVolumes: data.missing_volumes, threadUrl: data.ebdz_thread_url, threadId: data.ebdz_thread_id,
                matchStatus: data.match_status, matchedTitle: data.matched_title, isOneshot: isSeriesOneshot(seriesId)
            });
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Retire le matching EBDZ d'une série (repasse en état "non matché")
async function unmatchEbdzSeries(seriesId, context) {
    if (!confirm('Retirer le match EBDZ de cette série ?')) return;

    try {
        const response = await fetch(`/api/series/${seriesId}/ebdz-unmatch`, { method: 'POST' });
        const data = await response.json();

        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            return;
        }

        const containerId = context === 'modal' ? `ebdz-status-modal-${seriesId}` : `ebdz-status-card-${seriesId}`;
        const container = document.getElementById(containerId);
        if (container) {
            container.innerHTML = buildEbdzStatusHtml({
                seriesId, context, matchStatus: 'unmatched'
            });
            if (context === 'modal') refreshEbdzToolbarButton(seriesId, null);
        }
        updateCachedEbdzStatus(seriesId, { match_status: 'unmatched', ebdz_count: null, missing_volumes: [], ebdz_thread_url: null, matched_title: null });
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// ===== ÉDITION MANUELLE DES MÉTADONNÉES (série + tomes) =====
// Alternative à un match/MAJ Bédéthèque quand la série n'existe pas sur le site (ou que
// la fiche trouvée est incomplète/fausse): édite directement le résumé/genre/statut/
// auteur/année de la série (colonnes manual_*, jamais recalculées par un scan - voir
// _add_series_manual_metadata_columns côté Flask) et le ComicInfo.xml de chaque tome
// (même écrivain que la MAJ Bédéthèque, update_volume_comicinfo, donc cbz uniquement).
// Un seul modal pour les deux, contrairement au reste de l'app qui sépare "série" et
// "tome" en actions distinctes: ici l'utilisateur corrige typiquement plusieurs champs
// à la fois sur plusieurs tomes d'affilée, une modale par tome serait trop de
// va-et-vient pour ce cas d'usage précis.
// focusVolumeId (optionnel): ouvre la modale directement dépliée + scrollée sur ce tome
// précis (voir le bouton "📝 Éditer ce tome" du menu ⚙️ par tome) - sans ça l'utilisateur
// devait rouvrir la modale depuis "Actions" de la série puis chercher la bonne ligne dans
// une liste potentiellement longue.
// Liste fusionnée Bédéthèque (tomes possédés + connus mais pas encore téléchargés) de la
// série en cours d'édition - chargée une fois à l'ouverture de la modale, alimente le
// dropdown "Numéro / type de tome" (voir buildManualEditVolumeRenumberHtml). Module-level
// plutôt que passée en paramètre à travers renderManualEditModal(SingleVolume)/
// buildManualEditVolumeFieldsHtml: ces fonctions sont déjà appelées depuis plusieurs
// endroits (ex: après un renommage réussi) sans repasser systématiquement cette liste.
let _manualEditAllVolumes = [];

// File d'attente d'ids de tomes restant à revoir pour un "Éditer" groupé depuis la vue
// tableau (voir bulkEditSelectedVolumes/startBulkEditQueue/bulkEditNext plus haut) - la
// modale reste centrée sur un seul tome à la fois (voir commentaire ci-dessus), ce
// parcours "un par un avec bouton Suivant" est la seule façon d'appliquer "Éditer" à une
// sélection multiple sans fusionner des champs de tomes différents dans un seul formulaire.
let _bulkEditQueue = [];
let _bulkEditSeriesId = null;

function startBulkEditQueue(seriesId, volumeIds) {
    _bulkEditSeriesId = seriesId;
    _bulkEditQueue = [...volumeIds];
    bulkEditNext();
}

function bulkEditNext() {
    const nextId = _bulkEditQueue.shift();
    if (nextId == null) {
        _bulkEditSeriesId = null;
        closeManualEditModal();
        return;
    }
    openManualEditModal(_bulkEditSeriesId, nextId, true);
}

// _isBulkStep: true uniquement quand appelée par bulkEditNext ci-dessus - tout autre
// appel (menu ⚙️ d'un tome, "Éditer manuellement" de la toolbar série, "← Voir toute la
// série"...) n'est pas un parcours groupé et doit donc annuler une éventuelle file
// laissée en cours par un "Éditer" groupé précédent que l'utilisateur aurait interrompu.
async function openManualEditModal(seriesId, focusVolumeId, _isBulkStep = false) {
    if (!_isBulkStep) {
        _bulkEditQueue = [];
        _bulkEditSeriesId = null;
    }
    let modal = document.getElementById('manual-edit-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'manual-edit-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = '<div class="modal-content manual-edit-modal-content"><div class="loading"><div class="spinner"></div></div></div>';
    modal.classList.add('active');

    try {
        const [response, volumesResponse] = await Promise.all([
            fetch(`/api/series/${seriesId}`),
            fetch(`/api/series/${seriesId}/volumes`).catch(() => null)
        ]);
        const data = await response.json();
        try {
            _manualEditAllVolumes = volumesResponse ? await volumesResponse.json() : [];
        } catch (e) {
            _manualEditAllVolumes = [];
        }
        if (data.error) {
            modal.innerHTML = `<div class="modal-content"><span class="close-modal" onclick="closeManualEditModal()">×</span><div class="no-data"><p>❌ ${escapeHtml(data.error)}</p></div></div>`;
            return;
        }
        renderManualEditModal(data, focusVolumeId);
    } catch (error) {
        modal.innerHTML = `<div class="modal-content"><span class="close-modal" onclick="closeManualEditModal()">×</span><div class="no-data"><p>❌ Erreur de connexion: ${escapeHtml(error.message)}</p></div></div>`;
    }
}

function closeManualEditModal() {
    const modal = document.getElementById('manual-edit-modal');
    if (modal) modal.classList.remove('active');
    // Fermer la modale annule un "Éditer" groupé en cours (voir _bulkEditQueue) - repartir
    // de zéro plutôt que de reprendre une file à moitié oubliée à la prochaine ouverture.
    _bulkEditQueue = [];
    _bulkEditSeriesId = null;
}

function buildManualUniverseFieldHtml(universes, currentUniverseId) {
    const options = (universes || []).map(u => {
        const selected = currentUniverseId != null && String(u.id) === String(currentUniverseId) ? 'selected' : '';
        return `<option value="${u.id}" ${selected}>${escapeHtml(u.name)}</option>`;
    }).join('');
    const noneSelected = currentUniverseId == null ? 'selected' : '';
    return `
        <label class="manual-edit-field-block">Univers
            <select id="manual-series-universe">
                <option value="" ${noneSelected}>Aucun univers</option>
                ${options}
            </select>
        </label>
    `;
}

function _manualEditVolumeOptionLabel(v) {
    return buildVolumeOptionLabel(v, { preferBedethequeTitle: true });
}

function buildManualEditVolumeRenumberHtml(v, seriesId) {
    const others = (_manualEditAllVolumes || []).filter(ov => !(ov.id != null && ov.id === v.id));
    const currentType = v.is_integral ? 'integral' : v.is_hs ? 'hs' : v.is_episode ? 'episode' : 'volume';
    const currentNumber = v.is_integral ? v.integral_number : v.is_hs ? v.hs_number : v.is_episode ? v.episode_number : v.volume_number;
    // L'option "actuel" doit elle aussi afficher le vrai titre Bédéthèque de CE numéro
    // (pas le comicinfo.title local de `v`, potentiellement faux) - retrouvée dans
    // _manualEditAllVolumes (même id) qui porte déjà bedetheque_title (voir
    // get_series_volumes); repli sur `v` lui-même si absent de cette liste (série pas
    // matchée sur Bédéthèque, ou tome pas encore présent dans le cache des albums).
    const selfEntry = (_manualEditAllVolumes || []).find(ov => ov.id === v.id) || v;
    const optionsHtml = others.map(ov => {
        const type = ov.is_integral ? 'integral' : ov.is_hs ? 'hs' : ov.is_episode ? 'episode' : 'volume';
        const number = ov.is_integral ? ov.integral_number : ov.is_hs ? ov.hs_number : ov.is_episode ? ov.episode_number : ov.volume_number;
        const value = `${type}:${number != null ? number : ''}`;
        const owned = ov.filepath ? ' (en bibliothèque)' : '';
        return `<option value="${escapeHtml(value)}">${escapeHtml(_manualEditVolumeOptionLabel(ov))}${owned}</option>`;
    }).join('');

    return `
        <div class="manual-edit-renumber-section">
            <label>Numéro / type de tome
                <select id="manual-vol-renumber-slot-${v.id}" onchange="updateManualVolRenumberFields(${v.id})">
                    <option value="current:${currentNumber != null ? currentNumber : ''}" data-current-type="${currentType}" disabled selected style="color:#999;">${escapeHtml(_manualEditVolumeOptionLabel(selfEntry))} (actuel)</option>
                    ${optionsHtml}
                    <option value="manual">Autre (préciser)</option>
                </select>
            </label>
            <div id="manual-vol-renumber-manual-${v.id}" style="display:none; margin-top:6px;">
                <select id="manual-vol-renumber-type-${v.id}">
                    <option value="volume">Tome numéroté</option>
                    <option value="integral">Intégrale</option>
                    <option value="hs">Hors-série</option>
                    <option value="episode">Épisode</option>
                </select>
                <input type="number" id="manual-vol-renumber-number-${v.id}" placeholder="Numéro (laisser vide si non numéroté)">
            </div>
            <div class="manual-edit-actions-row" style="margin-top:6px;">
                <button class="btn-neutral-sm" id="manual-vol-renumber-btn-${v.id}" disabled onclick="changeVolumeNumber(${v.id}, ${seriesId})">${svgIcon('tag')} Changer le numéro</button>
                <span class="manual-edit-save-feedback" id="manual-vol-renumber-feedback-${v.id}"></span>
            </div>
        </div>
    `;
}

// L'option "(actuel)" du dropdown est desactivee (voir buildManualEditVolumeRenumberHtml)
// - on ne peut pas la re-selectionner une fois qu'on a choisi autre chose, donc pas besoin
// de renvoyer au serveur une soumission "meme numero qu'avant" (elle etait auparavant
// rejetee avec une erreur cote backend - inutile si le dropdown ne permet plus ce choix).
// Le bouton reste desactive tant que la selection est encore sur cette option "actuel".
function updateManualVolRenumberFields(volumeId) {
    const slot = document.getElementById(`manual-vol-renumber-slot-${volumeId}`).value;
    document.getElementById(`manual-vol-renumber-manual-${volumeId}`).style.display = slot === 'manual' ? 'block' : 'none';
    document.getElementById(`manual-vol-renumber-btn-${volumeId}`).disabled = slot.startsWith('current:');
}

async function changeVolumeNumber(volumeId, seriesId) {
    const slotEl = document.getElementById(`manual-vol-renumber-slot-${volumeId}`);
    const feedback = document.getElementById(`manual-vol-renumber-feedback-${volumeId}`);
    const slot = slotEl.value;

    let type, number;
    if (slot === 'manual') {
        type = document.getElementById(`manual-vol-renumber-type-${volumeId}`).value;
        const raw = document.getElementById(`manual-vol-renumber-number-${volumeId}`).value.trim();
        number = raw === '' ? null : parseInt(raw, 10);
    } else {
        const sepIndex = slot.indexOf(':');
        let t = sepIndex === -1 ? slot : slot.slice(0, sepIndex);
        const n = sepIndex === -1 ? '' : slot.slice(sepIndex + 1);
        if (t === 'current') t = slotEl.options[slotEl.selectedIndex].getAttribute('data-current-type');
        type = t;
        number = n === '' ? null : parseInt(n, 10);
    }

    feedback.textContent = '⏳ Enregistrement...';
    try {
        const response = await fetch(`/api/volumes/${volumeId}/renumber`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type, number })
        });
        const data = await response.json();

        if (data.success) {
            feedback.innerHTML = data.duplicate_filename
                ? `✅ Numéro changé - ⚠️ « ${escapeHtml(data.duplicate_filename)} » a aussi ce numéro, supprimez celui qui est en trop`
                : '✅ Numéro changé';
            if (currentSeriesDetail) await renderSeriesDetail(currentSeriesDetail.id);
            setTimeout(closeManualEditModal, data.duplicate_filename ? 2500 : 900);
            return;
        }

        feedback.innerHTML = `<span style="color:#dc2626;">❌ ${escapeHtml(data.error || 'Erreur inconnue')}</span>`;
    } catch (error) {
        feedback.innerHTML = `<span style="color:#dc2626;">❌ Erreur de connexion: ${escapeHtml(error.message)}</span>`;
    }
}

function buildManualEditVolumeFieldsHtml(v, seriesId, seriesTitle, renameModalVolumeLabel) {
    const ci = v.comicinfo || {};
    const isPlaceholder = !v.filepath;
    const writable = !isPlaceholder && (v.format || '').toLowerCase() === 'cbz';
    const volumeLabel = v.filename || `Tome ${v.volume_number != null ? v.volume_number : '?'}`;
    const deleteButtonHtml = `<button class="btn-danger-sm" onclick="deleteVolume(${v.id}, ${seriesId}, '${escapeForAttribute(volumeLabel)}', ${isPlaceholder})" title="${isPlaceholder ? 'Retire ce tome de la liste de suivi' : 'Supprime le fichier - le tome reste suivi comme manquant'}">${svgIcon('trash-2')} ${isPlaceholder ? 'Retirer ce tome' : 'Supprimer le fichier'}</button>`;
    // "can you change edit to be able to move albums to another serie" - un tome mal
    // rattaché (ex: un tome qui appartient en réalité à une autre série Bédéthèque) doit
    // pouvoir changer de série sans passer par Fusionner (qui déplace TOUTE la série).
    // Disponible même sur un placeholder/format non convertible: rien dans move-to-series
    // (Flask) n'exige un cbz, contrairement à l'édition du ComicInfo ci-dessous.
    const moveButtonHtml = `<button class="btn-neutral-sm" onclick="openMoveVolumeModal(${seriesId}, ${v.id}, '${escapeForAttribute(volumeLabel)}')" title="Déplace ce tome vers une autre série existante (même bibliothèque)">${svgIcon('git-merge')} Déplacer vers une autre série</button>`;

    if (isPlaceholder) {
        // Tome "placeholder" (ajouté depuis Bédéthèque, sans fichier - voir
        // add_series_from_bedetheque): rien à éditer, mais on peut toujours le retirer
        // de la liste des tomes à suivre
        return `
            <p class="help-text">Tome non possédé (ajouté depuis Bédéthèque) - rien à éditer tant qu'aucun fichier n'a été importé pour ce numéro.</p>
            <div class="manual-edit-actions-row">${moveButtonHtml}${deleteButtonHtml}</div>
        `;
    }
    // Le numéro/type peut être corrigé même sur un format non convertible en cbz (rien
    // à écrire dans un ComicInfo.xml pour ça, ce sont des colonnes DB séparées - voir
    // renumber_volume côté Flask) - affiché avant le repli "convertissez en cbz" plutôt
    // que bloqué par lui.
    const renumberHtml = buildManualEditVolumeRenumberHtml(v, seriesId);
    if (!writable) {
        return `
            ${renumberHtml}
            <p class="help-text">Convertissez ce tome en cbz (menu du tome) pour pouvoir éditer son ComicInfo.xml.</p>
            <div class="manual-edit-actions-row">${moveButtonHtml}${deleteButtonHtml}</div>
        `;
    }
    return `
        ${renumberHtml}
        <div class="manual-edit-field-row" style="align-items:flex-end;">
            <label style="flex:2;">Titre <input type="text" id="manual-vol-title-${v.id}" value="${escapeHtml(ci.title || '')}"></label>
            <label>Année <input type="text" id="manual-vol-year-${v.id}" value="${escapeHtml(ci.year || '')}" style="max-width:100px;"></label>
            <button class="btn-neutral-sm" id="manual-vol-load-bd-btn-${v.id}" onclick="loadBedethequeIntoVolumeForm(${v.id}, this)" style="margin-bottom:10px;">
                <img src="/static/img/bedetheque-logo.png" alt="" style="width:14px;height:14px;">Métadonnées
            </button>
        </div>
        <div class="manual-edit-field-row">
            <label>Scénariste <input type="text" id="manual-vol-writer-${v.id}" value="${escapeHtml(ci.writer || '')}"></label>
            <label>Dessinateur <input type="text" id="manual-vol-penciller-${v.id}" value="${escapeHtml(ci.penciller || '')}"></label>
        </div>
        <div class="manual-edit-field-row">
            <label>Coloriste <input type="text" id="manual-vol-colorist-${v.id}" value="${escapeHtml(ci.colorist || '')}"></label>
            <label>Éditeur <input type="text" id="manual-vol-publisher-${v.id}" value="${escapeHtml(ci.publisher || '')}"></label>
            <label>Genre <input type="text" id="manual-vol-genre-${v.id}" value="${escapeHtml(ci.genre || '')}"></label>
        </div>
        <!-- "add a way in editer de modify la qualite et releaser" - resolution/release_group
        sont des colonnes volumes brutes (pas du ComicInfo, voir v.resolution/v.release_group
        déjà affichés partout ailleurs en lecture seule via des badges), donc lues sur v
        et non sur ci comme les champs ci-dessus. -->
        <div class="manual-edit-field-row">
            <label>Qualité <input type="text" id="manual-vol-resolution-${v.id}" value="${escapeHtml(v.resolution || '')}" placeholder="ex: 1400x2150"></label>
            <label>Releaser <input type="text" id="manual-vol-release-group-${v.id}" value="${escapeHtml(v.release_group || '')}"></label>
        </div>
        <label class="manual-edit-field-block">Résumé <textarea id="manual-vol-summary-${v.id}" rows="6">${escapeHtml(ci.summary || '')}</textarea></label>
        <div class="manual-edit-actions-row">
            <button class="btn-neutral-sm" onclick="closeManualEditModal(); openRenameModal(${seriesId}, '${escapeForAttribute(seriesTitle)}', ${v.id}, false, {seriesId: ${seriesId}, volumeId: ${v.id}}, '${escapeForAttribute(renameModalVolumeLabel || '')}')" title="Renomme ce fichier au format standard configuré (Paramètres > Format)">${svgIcon('pencil')} Renommer ce fichier</button>
            <button class="btn" onclick="saveManualVolumeMetadata(${v.id}, this)">${svgIcon('save')} Enregistrer ce tome</button>
            ${moveButtonHtml}
            ${deleteButtonHtml}
            <span class="manual-edit-save-feedback" id="manual-vol-feedback-${v.id}"></span>
        </div>
    `;
}

function renderManualEditModal(data, focusVolumeId) {
    if (focusVolumeId) {
        renderManualEditModalSingleVolume(data, focusVolumeId);
        return;
    }

    const modal = document.getElementById('manual-edit-modal');
    const isOneshot = data.is_oneshot;

    // Pré-remplissage: manual_* est vide tant que personne n'a encore rien édité à la
    // main, mais la série a souvent déjà un résumé/genre/auteur connu via Bédéthèque (ou,
    // à défaut, extrait localement des fichiers) - sans ce repli, ouvrir "Éditer
    // manuellement" sur une série déjà documentée affichait des champs vides, comme si
    // rien n'était connu, alors que la fiche en affiche déjà la moitié juste au-dessus.
    const bd = data.bedetheque || {};
    // Bédéthèque prime sur local_summary (voir le même repli dans renderSeriesDetail
    // ci-dessus pour le pourquoi - local_summary vient du <Summary> ComicInfo des tomes,
    // pas toujours un vrai synopsis)
    const effectiveSummary = data.manual_summary || bd.description || data.local_summary || '';
    const effectiveGenre = data.manual_genre || bd.genre || data.local_genre || '';
    const effectiveStatus = data.manual_status || bd.status || '';
    const effectiveAuthor = data.manual_author || data.local_author
        || [bd.scenaristes, bd.dessinateurs].filter(Boolean).join(', ');
    const effectiveYearStart = data.manual_year_start || bd.year_start || data.local_year || '';
    const effectiveYearEnd = data.manual_year_end || bd.year_end || '';

    const volumesHtml = data.volumes.map(v => {
        const ci = v.comicinfo || {};
        const rawLabel = `${volumeNumberLabel(v, isOneshot)}${_volumeTitleSuffix(ci.title, data.title)}`;
        return `
            <div class="manual-edit-volume-row" id="manual-edit-volume-${v.id}">
                <div class="manual-edit-volume-header" onclick="toggleManualEditVolume(${v.id})">
                    <span class="manual-edit-volume-toggle" id="manual-edit-volume-toggle-${v.id}">▸</span>
                    <span class="manual-edit-volume-label">${escapeHtml(rawLabel)}</span>
                    <span class="help-text">${escapeHtml(v.filename || '')}</span>
                </div>
                <div class="manual-edit-volume-fields" id="manual-edit-volume-fields-${v.id}" style="display:none;">
                    ${buildManualEditVolumeFieldsHtml(v, data.id, data.title, rawLabel)}
                </div>
            </div>
        `;
    }).join('');

    // Statut de matching + accès direct au matching manuel pour chaque source, absent
    // du reste de cette modale (voir demande utilisateur) - repris du menu "🗂️
    // Métadonnées" du header de la fiche série mais en version compacte, alignée avec
    // les boutons Renommer sur une seule rangée (plutôt que deux rangées pleine taille,
    // qui prenaient trop de place pour des actions consultées une fois par visite).
    // Toujours la modale de matching manuel dédiée à la source (jamais un simple lien
    // "ouvrir", même déjà matché: on veut pouvoir CHANGER le match depuis ici) - la
    // modale d'édition se ferme d'abord (même logique que les boutons Renommer) et
    // chaque modale de matching reçoit returnToEdit pour afficher son propre bouton
    // "← Retour à l'édition" (voir _matchModalBackButtonHtml/backToManualEditFromMatch).
    const bdLogo = '/static/img/bedetheque-logo.png';
    const editReturnCtx = `{seriesId: ${data.id}, volumeId: null}`;
    // "dans renommer tous les tomes pour one-shot il faudrait afficher le nom du fichier
    // en dessous de la série" - "tous les tomes" d'un one-shot n'est jamais qu'UN seul
    // fichier ("tomes" au plural est trompeur ici), montrer LEQUEL avant même de lire
    // l'aperçu plus bas a le même intérêt que pour un renommage de tome unique (voir
    // volumeLabel/openRenameModal) - vide (pas de sous-titre supplémentaire) pour une
    // série à plusieurs tomes, où "tous les tomes" est déjà sans ambiguïté.
    const oneshotRenameVolume = data.is_oneshot ? (data.volumes || []).find(v => v.filepath) : null;
    const oneshotRenameFileLabel = oneshotRenameVolume ? (oneshotRenameVolume.filename || '') : '';
    const manualUniverseFieldHtml = buildManualUniverseFieldHtml(data.universes || [], data.universe ? data.universe.id : null);
    const topActionsHtml = `
        <div class="manual-edit-top-actions">
            <button class="btn-neutral-sm" onclick="closeManualEditModal(); openBedethequeMatchModal(${data.id}, () => renderSeriesDetail(${data.id}), ${editReturnCtx})">
                <img src="${bdLogo}" alt="" style="width:14px;height:14px;">Bédéthèque ${bd.url ? '✅' : '❌'}
            </button>
            <button class="btn-neutral-sm" onclick="closeManualEditModal(); openEbdzMatchModal(${data.id}, 'modal', ${editReturnCtx})">
                <img src="/static/img/ebdz-logo.png" alt="" style="width:14px;height:14px;">EBDZ ${data.ebdz.thread_url ? '✅' : '❌'}
            </button>
            ${data.ebdz.thread_url ? `<button class="btn-neutral-sm" onclick="rescrapeEbdzThread(${data.id}, this)" title="Relit ce thread EBDZ et met à jour ses liens"><img src="/static/img/ebdz-logo.png" alt="" style="width:14px;height:14px;">Rescraper</button>` : ''}
            <button class="btn-neutral-sm" onclick="closeManualEditModal(); openKomgaMatchModal(${data.id}, 'modal', ${editReturnCtx})">
                <img src="/static/img/komga-logo.svg" alt="" style="width:14px;height:14px;">Komga ${data.komga.url ? '✅' : '❌'}
            </button>
            <span style="width:1px; align-self:stretch; background:#e5e7eb;"></span>
            <button class="btn-neutral-sm" onclick="closeManualEditModal(); openRenameModal(${data.id}, '${escapeForAttribute(data.title)}', null, false, {seriesId: ${data.id}, volumeId: null})" title="Renomme le dossier de la série au format standard configuré (Paramètres > Format)">${svgIcon('pencil')} Renommer dossier</button>
            <button class="btn-neutral-sm" onclick="closeManualEditModal(); openRenameModal(${data.id}, '${escapeForAttribute(data.title)}', null, true, {seriesId: ${data.id}, volumeId: null}, '${escapeForAttribute(oneshotRenameFileLabel)}')" title="Renomme tous les fichiers de tomes au format standard configuré (jamais le dossier)">${svgIcon('pencil')} Renommer tomes</button>
        </div>
    `;

    modal.innerHTML = `
        <div class="modal-content rename-modal-content manual-edit-modal-content">
            <span class="close-modal" onclick="closeManualEditModal()">×</span>
            <div class="rename-modal-header">
                <h2>📝 Éditer manuellement</h2>
                <p class="rename-modal-subtitle">
                    Série: <strong>${escapeHtml(data.title)}</strong>
                    <button class="btn-neutral-sm" id="manual-series-load-bd-btn" data-url="${escapeHtml(bd.url || '')}" onclick="loadBedethequeIntoSeriesForm(this)" style="margin-left:8px;" ${bd.url ? '' : 'disabled title="Matchez d\'abord la série sur Bédéthèque (bouton ci-dessus)"'}>
                        <img src="${bdLogo}" alt="" style="width:14px;height:14px;">Métadonnées
                    </button>
                </p>
                ${data.path ? `<code class="manual-edit-path">${escapeHtml(data.path)}</code>` : ''}
            </div>

            <div class="rename-modal-body">
                ${topActionsHtml}
                <div class="rename-section">
                    <h3>Série</h3>
                    <div class="manual-edit-field-row">
                        <label style="flex:2;">Titre <input type="text" id="manual-series-title" value="${escapeHtml(data.title || '')}"></label>
                    </div>
                    <div class="manual-edit-field-row">
                        <label>Genre <input type="text" id="manual-series-genre" value="${escapeHtml(effectiveGenre)}"></label>
                        <label>Statut <input type="text" id="manual-series-status" value="${escapeHtml(effectiveStatus)}"></label>
                    </div>
                    <div class="manual-edit-field-row">
                        <label>Auteur(s) <input type="text" id="manual-series-author" value="${escapeHtml(effectiveAuthor)}"></label>
                        <label>Année début <input type="text" id="manual-series-year-start" value="${escapeHtml(effectiveYearStart)}" style="max-width:100px;"></label>
                        <label>Année fin <input type="text" id="manual-series-year-end" value="${escapeHtml(effectiveYearEnd)}" style="max-width:100px;"></label>
                    </div>
                    <label class="manual-edit-field-block">Résumé <textarea id="manual-series-summary" rows="6">${escapeHtml(effectiveSummary)}</textarea></label>
                    ${manualUniverseFieldHtml}
                    <div class="manual-edit-actions-row">
                        <button class="btn" onclick="saveManualSeriesMetadata(${data.id}, this)">${svgIcon('save')} Enregistrer la série</button>
                        <span class="manual-edit-save-feedback" id="manual-series-feedback"></span>
                    </div>
                </div>

                <div class="rename-section">
                    <h3>Tomes (${data.volumes.length})</h3>
                    <div class="manual-edit-volumes-list">
                        ${volumesHtml || '<p class="help-text">Aucun tome.</p>'}
                    </div>
                </div>
            </div>
        </div>
    `;
}

// Vue "un seul tome" (voir le bouton "📝 Éditer ce tome" du menu ⚙️ par tome): pas de
// champs série ici, seulement ce tome - éditer un tome précis n'a pas besoin d'exposer
// les réglages de toute la série au passage (voir demande utilisateur). Un lien "Voir
// toute la série" reste disponible pour rebasculer sur la vue complète si besoin.
function renderManualEditModalSingleVolume(data, focusVolumeId) {
    const modal = document.getElementById('manual-edit-modal');
    const v = data.volumes.find(vol => vol.id === focusVolumeId);

    if (!v) {
        modal.innerHTML = `<div class="modal-content"><span class="close-modal" onclick="closeManualEditModal()">×</span><div class="no-data"><p>❌ Tome introuvable.</p></div></div>`;
        return;
    }

    const label = volumeNumberLabel(v, data.is_oneshot);
    const rawLabel = `${label}${_volumeTitleSuffix((v.comicinfo || {}).title, data.title)}`;

    // "Suivant" du parcours "Éditer" groupé (voir bulkEditSelectedVolumes/
    // startBulkEditQueue tout en haut de ce fichier) - uniquement affiché quand cette
    // ouverture fait partie d'une telle file, jamais pour une édition d'un seul tome.
    const bulkNextHtml = _bulkEditQueue.length > 0
        ? `<button class="btn-neutral-sm" onclick="bulkEditNext()" style="margin-bottom:12px; float:right;">Suivant (${_bulkEditQueue.length} restant${_bulkEditQueue.length > 1 ? 's' : ''}) →</button>`
        : '';

    modal.innerHTML = `
        <div class="modal-content rename-modal-content manual-edit-modal-content">
            <span class="close-modal" onclick="closeManualEditModal()">×</span>
            <div class="rename-modal-header">
                ${bulkNextHtml}
                <button class="btn-neutral-sm" onclick="openManualEditModal(${data.id})" style="margin-bottom:12px;">← Voir toute la série</button>
                <h2>📝 Éditer ${label}</h2>
                <p class="rename-modal-subtitle">Série: <strong>${escapeHtml(data.title)}</strong></p>
                ${v.filename ? `<code class="manual-edit-path">${escapeHtml(v.filename)}</code>` : ''}
            </div>

            <div class="rename-modal-body">
                <div class="rename-section">
                    ${buildManualEditVolumeFieldsHtml(v, data.id, data.title, rawLabel)}
                </div>
            </div>
        </div>
    `;
}

function toggleManualEditVolume(volumeId) {
    const fields = document.getElementById(`manual-edit-volume-fields-${volumeId}`);
    const toggle = document.getElementById(`manual-edit-volume-toggle-${volumeId}`);
    const expanded = fields.style.display !== 'none';
    fields.style.display = expanded ? 'none' : 'block';
    toggle.textContent = expanded ? '▸' : '▾';
}

// Remplit les champs SÉRIE du formulaire avec les valeurs actuelles de Bédéthèque
// (résumé/genre/statut/auteur/année), sans rien enregistrer - juste GET /bedetheque/info
// sur l'URL déjà matchée (voir data-url posé sur ce bouton), pour partir de données
// fraîches plutôt que de celles chargées à l'ouverture de la modale (qui peuvent dater
// si la série vient d'être rematchée). L'utilisateur ajuste puis clique "Enregistrer
// la série" séparément.
async function loadBedethequeIntoSeriesForm(buttonEl) {
    const button = buttonEl;
    const url = button.dataset.url;
    if (!url) return;

    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳ Chargement...';

    try {
        const response = await fetch(`/api/bedetheque/info?url=${encodeURIComponent(url)}`);
        const data = await response.json();

        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            return;
        }

        const info = data.info;
        document.getElementById('manual-series-genre').value = info.genre || '';
        document.getElementById('manual-series-status').value = info.status || '';
        document.getElementById('manual-series-author').value = [info.scenaristes, info.dessinateurs].filter(Boolean).join(', ');
        document.getElementById('manual-series-year-start').value = info.year_start || '';
        document.getElementById('manual-series-year-end').value = info.year_end || '';
        document.getElementById('manual-series-summary').value = info.description || '';
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    } finally {
        button.disabled = false;
        button.innerHTML = originalHtml;
    }
}

// Même principe pour un tome précis: GET /bedetheque/volume-preview/<id> (lecture seule,
// n'écrit rien - voir preview_volume_bedetheque_metadata côté Flask) puis remplit les
// champs de CE tome sans les enregistrer.
async function loadBedethequeIntoVolumeForm(volumeId, buttonEl) {
    const button = buttonEl;
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '⏳ Chargement...';

    try {
        const response = await fetch(`/api/bedetheque/volume-preview/${volumeId}`);
        const data = await response.json();

        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            return;
        }

        const f = data.fields;
        if ('Title' in f) document.getElementById(`manual-vol-title-${volumeId}`).value = f.Title || '';
        if ('Year' in f) document.getElementById(`manual-vol-year-${volumeId}`).value = f.Year || '';
        if ('Writer' in f) document.getElementById(`manual-vol-writer-${volumeId}`).value = f.Writer || '';
        if ('Penciller' in f) document.getElementById(`manual-vol-penciller-${volumeId}`).value = f.Penciller || '';
        if ('Colorist' in f) document.getElementById(`manual-vol-colorist-${volumeId}`).value = f.Colorist || '';
        if ('Publisher' in f) document.getElementById(`manual-vol-publisher-${volumeId}`).value = f.Publisher || '';
        if ('Genre' in f) document.getElementById(`manual-vol-genre-${volumeId}`).value = f.Genre || '';
        if ('Summary' in f) document.getElementById(`manual-vol-summary-${volumeId}`).value = f.Summary || '';
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    } finally {
        button.disabled = false;
        button.innerHTML = originalHtml;
    }
}

// Lit les champs de la section "Série" de la modale "Éditer manuellement" et construit
// le body PUT /api/series/<id>/manual-metadata. Univers séparé des autres champs
// (isolé pour être testable indépendamment, voir tests/test_manual_universe_ui.py):
// select vide ("Aucun univers") -> null explicite (retire l'appartenance), jamais omis
// (voir buildManualUniverseFieldHtml pourquoi ce champ n'a pas d'état "inchangé").
function buildManualSeriesMetadataBody() {
    const universeSelect = document.getElementById('manual-series-universe');
    const universeValue = universeSelect ? universeSelect.value : '';
    return {
        title: document.getElementById('manual-series-title').value,
        summary: document.getElementById('manual-series-summary').value,
        genre: document.getElementById('manual-series-genre').value,
        status: document.getElementById('manual-series-status').value,
        author: document.getElementById('manual-series-author').value,
        year_start: document.getElementById('manual-series-year-start').value,
        year_end: document.getElementById('manual-series-year-end').value,
        universe_id: universeValue ? parseInt(universeValue, 10) : null,
    };
}

async function saveManualSeriesMetadata(seriesId, buttonEl) {
    const button = buttonEl;
    // innerHTML (pas textContent): le bouton contient une icône SVG inline (voir
    // svgIcon), dont le texte serait perdu si on capturait/restaurait via textContent
    // (les <svg> n'ont pas de noeud texte)
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.textContent = '⏳ Enregistrement...';
    const feedback = document.getElementById('manual-series-feedback');
    feedback.textContent = '';

    const body = buildManualSeriesMetadataBody();

    try {
        const response = await fetch(`/api/series/${seriesId}/manual-metadata`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();

        if (data.success) {
            feedback.textContent = '✅ Enregistré';
            await renderSeriesDetail(seriesId);
            // Petit délai pour laisser voir le "✅ Enregistré" avant la fermeture
            // automatique (voir aussi saveManualVolumeMetadata) plutôt que de fermer
            // instantanément sans retour visuel
            setTimeout(closeManualEditModal, 700);
        } else {
            feedback.innerHTML = `<span style="color:#dc2626;">❌ ${escapeHtml(data.error || 'Erreur inconnue')}</span>`;
        }
    } catch (error) {
        feedback.innerHTML = `<span style="color:#dc2626;">❌ Erreur de connexion: ${escapeHtml(error.message)}</span>`;
    } finally {
        button.disabled = false;
        button.innerHTML = originalHtml;
    }
}

async function saveManualVolumeMetadata(volumeId, buttonEl) {
    const button = buttonEl;
    // innerHTML (pas textContent): voir commentaire équivalent dans
    // saveManualSeriesMetadata ci-dessus
    const originalHtml = button.innerHTML;
    button.disabled = true;
    button.textContent = '⏳ Enregistrement...';
    const feedback = document.getElementById(`manual-vol-feedback-${volumeId}`);
    feedback.textContent = '';

    const body = {
        title: document.getElementById(`manual-vol-title-${volumeId}`).value,
        summary: document.getElementById(`manual-vol-summary-${volumeId}`).value,
        writer: document.getElementById(`manual-vol-writer-${volumeId}`).value,
        penciller: document.getElementById(`manual-vol-penciller-${volumeId}`).value,
        colorist: document.getElementById(`manual-vol-colorist-${volumeId}`).value,
        publisher: document.getElementById(`manual-vol-publisher-${volumeId}`).value,
        genre: document.getElementById(`manual-vol-genre-${volumeId}`).value,
        year: document.getElementById(`manual-vol-year-${volumeId}`).value,
        resolution: document.getElementById(`manual-vol-resolution-${volumeId}`).value,
        release_group: document.getElementById(`manual-vol-release-group-${volumeId}`).value,
    };

    try {
        // Le sélecteur type/numéro fait partie de cette même fiche. Auparavant, le bouton
        // principal « Enregistrer ce tome » ignorait silencieusement sa valeur: il fallait
        // deviner qu'un second bouton « Changer le numéro » devait être cliqué séparément.
        // Appliquer d'abord la renumérotation éventuelle, puis les métadonnées saisies.
        const renumberSlotEl = document.getElementById(`manual-vol-renumber-slot-${volumeId}`);
        let renumberDuplicateFilename = renumberSlotEl?.dataset.appliedDuplicateFilename || null;
        if (renumberSlotEl && !renumberSlotEl.value.startsWith('current:')) {
            let renumberType;
            let renumberNumber;
            if (renumberSlotEl.value === 'manual') {
                renumberType = document.getElementById(`manual-vol-renumber-type-${volumeId}`).value;
                const raw = document.getElementById(`manual-vol-renumber-number-${volumeId}`).value.trim();
                renumberNumber = raw === '' ? null : parseInt(raw, 10);
            } else {
                const sepIndex = renumberSlotEl.value.indexOf(':');
                renumberType = sepIndex === -1 ? renumberSlotEl.value : renumberSlotEl.value.slice(0, sepIndex);
                const raw = sepIndex === -1 ? '' : renumberSlotEl.value.slice(sepIndex + 1);
                renumberNumber = raw === '' ? null : parseInt(raw, 10);
            }
            const renumberKey = `${renumberType}:${renumberNumber ?? ''}`;
            // Une renumérotation peut réussir puis l'écriture ComicInfo échouer. Garder
            // cette réussite dans la modale afin qu'un clic Réessayer ne renvoie pas la
            // même mutation (que le backend rejetterait comme « déjà ce numéro »).
            if (renumberSlotEl.dataset.appliedRenumberKey !== renumberKey) {
                const renumberResponse = await fetch(`/api/volumes/${volumeId}/renumber`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ type: renumberType, number: renumberNumber })
                });
                const renumberData = await renumberResponse.json();
                if (!renumberData.success) {
                    feedback.innerHTML = `<span style="color:#dc2626;">❌ ${escapeHtml(renumberData.error || 'Erreur de changement de type')}</span>`;
                    return;
                }
                renumberSlotEl.dataset.appliedRenumberKey = renumberKey;
                renumberDuplicateFilename = renumberData.duplicate_filename || null;
                renumberSlotEl.dataset.appliedDuplicateFilename = renumberDuplicateFilename || '';
            }
        }

        const response = await fetch(`/api/volumes/${volumeId}/manual-metadata`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();

        if (data.success) {
            if (renumberDuplicateFilename) {
                feedback.innerHTML = `✅ Enregistré - ⚠️ « ${escapeHtml(renumberDuplicateFilename)} » a aussi ce numéro, supprimez celui qui est en trop`;
            } else {
                feedback.textContent = '✅ Enregistré';
            }
            if (currentSeriesDetail) await renderSeriesDetail(currentSeriesDetail.id);
            // L'avertissement de doublon exige une action manuelle: le laisser visible
            // plus longtemps que le simple accusé de sauvegarde.
            setTimeout(closeManualEditModal, renumberDuplicateFilename ? 2500 : 700);
        } else {
            feedback.innerHTML = `<span style="color:#dc2626;">❌ ${escapeHtml(data.error || 'Erreur inconnue')}</span>`;
        }
    } catch (error) {
        feedback.innerHTML = `<span style="color:#dc2626;">❌ Erreur de connexion: ${escapeHtml(error.message)}</span>`;
    } finally {
        button.disabled = false;
        button.innerHTML = originalHtml;
    }
}

// Supprime définitivement un tome (fichier + fiche, voir DELETE /api/volumes/<id> côté
// Flask) - un tome "placeholder" (voir buildManualEditVolumeFieldsHtml) n'a pas de
// fichier à supprimer, seule sa ligne part. Ferme la modale d'édition et rafraîchit la
// fiche série ensuite (le tome supprimé ne doit plus y apparaître) ainsi que la grille
// bibliothèque si elle est chargée (même pattern que toggleOneshot).
// "quand je delete un volume ca retire le fichier... pas retiré la fiche du volume. tu
// peux retirer la fiche du volume quand je ne le possède pas" - un tome possédé perd son
// fichier mais reste suivi comme manquant (voir delete_volume côté Flask, réduit à un
// placeholder plutôt que retiré) ; un placeholder déjà vide, lui, voit sa fiche
// réellement retirée - message de confirmation différent selon le cas.
async function deleteVolume(volumeId, seriesId, label, isPlaceholder = false) {
    const confirmMessage = isPlaceholder
        ? `Retirer "${label}" de la liste de suivi ?`
        : `Supprimer le fichier de "${label}" ?\n\nLe tome restera suivi comme manquant (sa fiche n'est pas retirée).`;
    if (!confirm(confirmMessage)) return;

    try {
        const response = await fetch(`/api/volumes/${volumeId}`, { method: 'DELETE' });
        const data = await response.json();

        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            return;
        }

        closeManualEditModal();
        if (currentSeriesDetail && currentSeriesDetail.id === seriesId) {
            await renderSeriesDetail(seriesId);
        }
        setTimeout(() => {
            if (typeof loadLibraryData === 'function') {
                loadLibraryData();
            }
        }, 500);
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}


async function rescrapeEbdzThread(seriesId, button) {
    const original = button.innerHTML;
    button.disabled = true; button.textContent = '⏳ Rescrape…';
    try {
        const response = await fetch(`/api/series/${seriesId}/ebdz-rescrape`, {method: 'POST'});
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Erreur inconnue');
        button.textContent = `✓ ${data.links_inserted || 0} lien ajouté`;
        await renderSeriesDetail(seriesId);
    } catch (error) {
        button.innerHTML = original; button.disabled = false; alert('❌ ' + error.message);
    }
}

// ===== FICHIERS EBDZ D'UNE SÉRIE MATCHÉE =====
// Liste des fichiers ed2k trouvés sur le thread EBDZ matché à la série, affichée dans la
// modale ouverte en cliquant sur le badge ratio possédé/EBDZ (📊 x/y)
async function openEbdzFilesModal(seriesId, threadId) {
    const modal = document.getElementById('ebdz-files-modal');
    const body = document.getElementById('ebdz-files-modal-body');
    modal.classList.add('active');
    modal.dataset.seriesId = seriesId;

    body.innerHTML = `
        <h2 class="modal-title modal-title-with-logo" style="margin-bottom: 15px;"><img src="/static/img/ebdz-logo.png" alt="" class="modal-title-logo"> Fichiers trouvés sur EBDZ</h2>
        <div id="ebdz-files-results"><div class="loading"><div class="spinner"></div></div></div>
    `;

    // Récupère les tomes manquants déjà calculés pour cette série (comparaison
    // possédés/EBDZ), pour surligner dans le tableau les fichiers non possédés
    let missingVolumes = [];
    try {
        const seriesResponse = await fetch(`/api/series/${seriesId}`);
        const seriesData = await seriesResponse.json();
        missingVolumes = seriesData.ebdz?.missing_volumes || [];
    } catch (error) {
        // Best-effort: en cas d'échec, le tableau s'affiche simplement sans surlignage
    }

    await loadEbdzThreadFiles(threadId, missingVolumes);
}

function closeEbdzFilesModal() {
    document.getElementById('ebdz-files-modal').classList.remove('active');
}

// "créer une modale quand je clique sur review" - avis de lecteurs Bédéthèque pour ce
// tome (texte complet, pas juste la note déjà affichée en permanence). L'URL d'album
// utilisée côté serveur est celle figée dans le ComicInfo <Web> du tome, pas de re-match
// à la volée - voir GET /api/bedetheque/reviews/volume/<id>.
async function openBedethequeReviewsModal(volumeId) {
    const modal = document.getElementById('bedetheque-reviews-modal');
    const body = document.getElementById('bedetheque-reviews-modal-body');
    modal.classList.add('active');
    body.innerHTML = `
        <h2 class="modal-title" style="margin-bottom: 15px;">⭐ Avis des lecteurs (Bédéthèque)</h2>
        <div id="bedetheque-reviews-results"><div class="loading"><div class="spinner"></div></div></div>
    `;

    try {
        const response = await fetch(`/api/bedetheque/reviews/volume/${volumeId}`);
        const data = await response.json();
        const resultsEl = document.getElementById('bedetheque-reviews-results');
        if (!resultsEl) return; // modale refermée entre-temps

        if (!data.success) {
            resultsEl.innerHTML = `<div class="no-data"><p>😕 ${escapeHtml(data.error || 'Avis introuvables')}</p></div>`;
            return;
        }
        if (!data.reviews || data.reviews.length === 0) {
            resultsEl.innerHTML = `
                <div class="no-data"><p>Aucun avis publié pour ce tome.</p></div>
                <p style="text-align: center;"><a href="${escapeHtml(data.album_url)}" target="_blank" rel="noopener">Voir la page sur Bédéthèque ↗</a></p>
            `;
            return;
        }

        const reviewsHtml = data.reviews.map(r => `
            <div style="border-bottom: 1px solid #e5e7eb; padding: 12px 0;">
                <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 10px;">
                    <strong>${escapeHtml(r.author || 'Anonyme')}</strong>
                    <span style="color: #6b7280; font-size: 0.85em; white-space: nowrap;">
                        ${r.rating != null ? `${'★'.repeat(r.rating)}${'☆'.repeat(5 - r.rating)} • ` : ''}${r.date ? escapeHtml(r.date) : ''}
                    </span>
                </div>
                <p style="white-space: pre-wrap; margin: 6px 0 0;">${escapeHtml(r.text || '')}</p>
            </div>
        `).join('');

        resultsEl.innerHTML = `
            <div style="max-height: 60vh; overflow-y: auto;">${reviewsHtml}</div>
            <p style="text-align: center; margin-top: 12px;"><a href="${escapeHtml(data.album_url)}" target="_blank" rel="noopener">Voir la page sur Bédéthèque ↗</a></p>
        `;
    } catch (error) {
        const resultsEl = document.getElementById('bedetheque-reviews-results');
        if (resultsEl) resultsEl.innerHTML = `<div class="no-data"><p>😕 Erreur lors du chargement des avis</p></div>`;
    }
}

function closeBedethequeReviewsModal() {
    document.getElementById('bedetheque-reviews-modal').classList.remove('active');
}

// Construit le libellé de la colonne "Volume" à partir des champs parsés côté serveur
// (voir LibraryScanner.parse_filename): numéro de tome si trouvé, sinon tag intégrale/HS/épisode
// (avec son propre numéro s'il existe pour HS/INT/épisode), sinon "?" si rien n'a pu être déterminé
function buildEbdzVolumeLabel(f) {
    const volume = f.volume || f.parsed_volume;
    if (volume) return String(volume);
    if (f.is_integral) return f.integral_number ? `📦 INT ${f.integral_number}` : '📦 INT';
    if (f.is_hs) return f.hs_number ? `✨ HS ${f.hs_number}` : '✨ HS';
    if (f.is_episode) return f.episode_number ? `🎬 Ép ${f.episode_number}` : '🎬 Ép';
    return '?';
}

// Récupère tous les fichiers ed2k scrapés pour ce thread EBDZ (pas de filtre "volume":
// on veut la liste complète) et les affiche sous forme de tableau (une ligne par fichier,
// une colonne par métadonnée parsée) pour une lecture rapide, avec un bouton pour ajouter
// chaque fichier à eMule
async function loadEbdzThreadFiles(threadId, missingVolumes) {
    const resultsEl = document.getElementById('ebdz-files-results');
    resultsEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    const missingSet = new Set(missingVolumes || []);

    try {
        const response = await fetch(`/api/search?thread_id=${encodeURIComponent(threadId)}`);
        const data = await response.json();
        const files = (data.results || []).slice().sort(
            (a, b) => (a.volume || a.parsed_volume || 0) - (b.volume || b.parsed_volume || 0)
        );

        if (files.length === 0) {
            resultsEl.innerHTML = `<div class="no-data"><p>😕 Aucun fichier trouvé sur EBDZ pour ce thread</p></div>`;
            return;
        }

        const rowsHtml = files.map(f => {
            const decodedFilename = decodeFilename(f.filename);
            const volume = f.volume || f.parsed_volume;
            const isMissing = volume && missingSet.has(volume);
            const rowStyle = isMissing
                ? 'border-bottom: 1px solid var(--color-border);'
                : 'border-bottom: 1px solid var(--color-border);';
            return `
                <tr class="${isMissing ? 'ebdz-modal-missing-row' : ''}" style="${rowStyle}">
                    <td class="${isMissing ? 'ebdz-modal-missing-cell' : ''}" style="padding: 8px; text-align: center; white-space: nowrap; ${isMissing ? 'font-weight: 600;' : ''}">${escapeHtml(buildEbdzVolumeLabel(f))}${isMissing ? ' ❌' : ''}</td>
                    <td style="padding: 8px; word-break: break-word;">${escapeHtml(decodedFilename)}</td>
                    <td style="padding: 8px; white-space: nowrap;">${f.resolution ? escapeHtml(f.resolution) : '-'}</td>
                    <td style="padding: 8px; white-space: nowrap;">${f.year || '-'}</td>
                    <td style="padding: 8px; white-space: nowrap;">${f.format ? escapeHtml(f.format.toUpperCase()) : '-'}</td>
                    <td style="padding: 8px; white-space: nowrap;">${formatBytes(parseInt(f.filesize, 10) || 0)}</td>
                    <td style="padding: 8px; white-space: nowrap; text-align: center;">
                        <button class="btn" onclick="addToEmule('${escapeForAttribute(f.link)}', this, '${escapeForAttribute(decodedFilename)}')">${svgIcon('plus')} Ajouter</button>
                    </td>
                </tr>
            `;
        }).join('');

        resultsEl.innerHTML = `
            ${missingSet.size > 0 ? `<p class="ebdz-modal-missing-hint" style="margin: 0 0 10px 0; font-size: 0.9em;">🟡 Surligné = tome absent de votre collection</p>` : ''}
            <div style="overflow-x: auto;">
                <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                    <thead>
                        <tr class="ebdz-modal-header-row">
                            <th style="padding: 8px; text-align: center; font-weight: 600;">Vol.</th>
                            <th style="padding: 8px; text-align: left; font-weight: 600;">Fichier</th>
                            <th style="padding: 8px; text-align: left; font-weight: 600;">Résolution</th>
                            <th style="padding: 8px; text-align: left; font-weight: 600;">Année</th>
                            <th style="padding: 8px; text-align: left; font-weight: 600;">Format</th>
                            <th style="padding: 8px; text-align: left; font-weight: 600;">Taille</th>
                            <th style="padding: 8px; text-align: center; font-weight: 600;">Action</th>
                        </tr>
                    </thead>
                    <tbody>${rowsHtml}</tbody>
                </table>
            </div>
        `;
    } catch (error) {
        resultsEl.innerHTML = `<div class="no-data"><p>❌ ${escapeHtml(error.message)}</p></div>`;
    }
}

// ===== KOMGA =====
const KOMGA_STATUS_LABELS = { ONGOING: 'En cours', ENDED: 'Terminée', ABANDONED: 'Abandonnée', HIATUS: 'En pause' };

// Bouton Komga de la toolbar: même principe que buildEbdzToolbarButtonHtml (lien direct
// si matché, sinon déclenche une tentative de matching automatique)
function buildKomgaToolbarButtonHtml(seriesId, komgaUrl) {
    if (komgaUrl) {
        // Icône seule, pas de libellé: le lien "Ouvrir sur Komga" en toutes lettres
        // existe déjà dans le bloc "🔗 Liens" du header - doublon ici, mais l'icône reste
        // un repère visuel rapide (matché/pas matché) dans ce menu Métadonnées
        return `
            <a class="toolbar-btn toolbar-btn-icon-only" href="${escapeHtml(komgaUrl)}" target="_blank" rel="noopener" data-tooltip="Ouvrir sur Komga">
                <img src="/static/img/komga-logo.svg" alt="Komga" class="toolbar-btn-logo">
            </a>
        `;
    }
    return `
        <button class="toolbar-btn" onclick="checkKomgaMetadata(${seriesId}, 'modal')" data-tooltip="Matcher">
            <img src="/static/img/komga-logo.svg" alt="" class="toolbar-btn-logo"><span class="toolbar-btn-label">Komga</span>
        </button>
    `;
}

// Reconstruit le bouton Komga de la toolbar après un match/unmatch
function refreshKomgaToolbarButton(seriesId, komgaUrl) {
    const groupEl = document.getElementById(`komga-toolbar-group-${seriesId}`);
    if (!groupEl) return;
    const btnEl = groupEl.querySelector('.toolbar-btn');
    if (btnEl) btnEl.outerHTML = buildKomgaToolbarButtonHtml(seriesId, komgaUrl).trim();
}

// Construit le HTML du statut de matching Komga (icône matché/non matché + actions) -
// plus de rendu de métadonnées Komga (tomes/statut/auteurs), Bédéthèque est la seule
// source de métadonnées descriptives de l'appli
function buildKomgaStatusHtml({ seriesId, context, matchStatus, matchedTitle, komgaUrl, komgaStatus, totalVolumes, authors, localCoverPath, localSummary }) {
    if (matchStatus === 'unmatched') {
        return `
            <span class="badge badge-warning" title="Aucun match Komga non-ambigu trouvé automatiquement">❌</span>
            <span class="toolbar-status-actions">
                <button class="btn" onclick="checkKomgaMetadata(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em;" title="Réessayer un matching automatique">${svgIcon('refresh-cw')}</button>
                <button class="btn" onclick="openKomgaMatchModal(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em; background: #06b6d4;" title="Matcher manuellement sur Komga">${svgIcon('search')}</button>
            </span>
        `;
    }

    if (!matchStatus) {
        return '';
    }

    if (context === 'modal') {
        // Contrôle discret: pas d'infos Komga (tomes/statut/auteurs/résumé/couverture)
        // affichées ici, seulement le statut de matching et les actions pour le gérer
        // (la couverture/résumé locaux sont déjà affichés en haut de la page). Le bouton
        // Komga de la toolbar est maintenant le lien direct: le rafraîchissement du
        // matching se fait ici, dans les actions au survol
        return `
            <span class="badge badge-success" title="${matchedTitle ? escapeHtml(matchedTitle) : 'Série matchée sur Komga'}">✅</span>
            <span class="toolbar-status-actions">
                <button class="btn" onclick="checkKomgaMetadata(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em;" title="Rafraîchir les métadonnées Komga">${svgIcon('refresh-cw')}</button>
                <button class="btn" onclick="openKomgaMatchModal(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em;" title="Changer le match Komga">${svgIcon('pencil')}</button>
                <button class="btn" onclick="unmatchKomgaSeries(${seriesId}, '${context}')" style="padding: 4px 8px; font-size: 0.75em; background: #ef4444;" title="Retirer le match Komga">${svgIcon('ban')}</button>
            </span>
        `;
    }

    // context 'card': plus de rendu détaillé ici - le conteneur cible
    // (komga-status-card-*, voir buildHiddenStatusAnchorsHtml) est en permanence
    // display:none, ce HTML n'est donc jamais vu par personne.
    return '';
}

// Récupère les métadonnées Komga d'une série (match direct si déjà matchée, sinon
// tentative d'auto-match par titre), persiste le résultat et affiche le statut
async function checkKomgaMetadata(seriesId, context) {
    const containerId = context === 'modal' ? `komga-status-modal-${seriesId}` : `komga-status-card-${seriesId}`;
    const container = document.getElementById(containerId);
    if (!container) return;

    container.innerHTML = '<span class="badge">⏳ Récupération Komga...</span>';

    try {
        const response = await fetch(`/api/series/${seriesId}/komga-enrich`, { method: 'POST' });
        const data = await response.json();

        if (!data.success) {
            container.innerHTML = `<span class="badge badge-warning">❌ ${escapeHtml(data.error || 'Erreur inconnue')}</span>`;
            return;
        }

        container.innerHTML = buildKomgaStatusHtml({
            seriesId, context, matchStatus: data.match_status, matchedTitle: data.matched_title,
            komgaUrl: data.komga_url
        });
        if (context === 'modal') refreshKomgaToolbarButton(seriesId, data.komga_url);

        updateCachedKomgaStatus(seriesId, data);
    } catch (error) {
        container.innerHTML = `<span class="badge badge-warning">❌ ${escapeHtml(error.message)}</span>`;
    }
}

// Met à jour le cache local seriesData avec le dernier statut Komga connu pour une série
function updateCachedKomgaStatus(seriesId, data) {
    const cached = seriesData.find(item => item.id === seriesId);
    if (!cached) return;
    cached.komga_match_status = data.match_status ?? null;
    cached.komga_matched_title = data.matched_title ?? null;
    cached.komga_series_id = data.komga_series_id ?? null;
    cached.komga_url = data.komga_url ?? null;
    cached.komga_cover_path = data.komga_cover_path ?? null;
    refreshSeriesItem(seriesId);
}

// Ouvre la modale de matching manuel Komga pour une série, pré-remplie avec son titre
async function openKomgaMatchModal(seriesId, context, returnToEdit = null) {
    const s = resolveSeriesForModal(seriesId);
    const modal = document.getElementById('komga-match-modal');
    const body = document.getElementById('komga-match-modal-body');
    modal.classList.add('active');
    modal.dataset.seriesId = seriesId;
    modal.dataset.context = context;
    matchModalReturnToEdit = returnToEdit;

    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/komga-logo.svg',
        title: 'Matcher manuellement sur Komga',
        queryId: 'komga-match-query',
        queryPlaceholder: 'Titre à rechercher sur Komga, ou URL directe de la fiche série...',
        prefillValue: s ? s.title : '',
        resultsId: 'komga-match-results',
        searchOnclick: `searchKomgaMatchCandidates(${seriesId})`,
        autoSearch: true,
        backButtonHtml: _matchModalBackButtonHtml(),
    });

    wireMatchModalEnterKeys('komga-match-query', () => searchKomgaMatchCandidates(seriesId));
    initClearableSearchInputs(body);

    await searchKomgaMatchCandidates(seriesId);
}

function closeKomgaMatchModal() {
    document.getElementById('komga-match-modal').classList.remove('active');
    matchModalReturnToEdit = null;
}

// Recherche des séries Komga candidates pour la série en cours de matching et les affiche.
// Un seul champ pour titre ET URL directe ("je n'ai besoin que d'une seule entrée (nom et
// URL)") - même principe que searchEbdzMatchCandidates ci-dessus (et que le champ
// Bédéthèque, qui n'a jamais eu de second champ séparé): si la saisie ressemble à une
// URL, on confirme directement le match plutôt que de la chercher comme un titre
// (remplace l'ancien champ urlFallback + submitKomgaMatchUrl).
async function searchKomgaMatchCandidates(seriesId) {
    const resultsEl = document.getElementById('komga-match-results');
    const query = document.getElementById('komga-match-query').value.trim();

    if (/^https?:\/\//i.test(query)) {
        await confirmKomgaMatch(seriesId, { komga_url: query });
        return;
    }

    resultsEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    try {
        const params = query ? `?q=${encodeURIComponent(query)}` : '';
        const response = await fetch(`/api/series/${seriesId}/komga-candidates${params}`);
        const data = await response.json();

        if (!data.success) {
            resultsEl.innerHTML = matchModalErrorHtml(data.error || 'Erreur inconnue');
            return;
        }

        if (data.candidates.length === 0) {
            resultsEl.innerHTML = matchModalNoResultsHtml('Aucune série Komga trouvée pour cette recherche');
            return;
        }

        resultsEl.innerHTML = data.candidates.map(c => matchCandidateCardHtml(
            `selectKomgaCandidate(${seriesId}, '${c.komga_series_id}')`,
            c.title || '(sans titre)',
            `📖 ${c.total_volumes ?? '?'} ${pluralize(c.total_volumes, 'album')}${c.status ? ` • ${escapeHtml(KOMGA_STATUS_LABELS[c.status] || c.status)}` : ''}`
        )).join('');
    } catch (error) {
        resultsEl.innerHTML = matchModalErrorHtml(error.message);
    }
}

// Confirme le matching manuel d'une série avec une série Komga précise choisie par
// l'utilisateur dans les résultats de recherche
function selectKomgaCandidate(seriesId, komgaSeriesId) {
    return confirmKomgaMatch(seriesId, { komga_series_id: komgaSeriesId });
}

// Confirme le matching manuel d'une série avec une série Komga précise, identifiée soit
// par komga_series_id (candidat cliqué) soit par komga_url (URL collée, voir
// submitKomgaMatchUrl)
async function confirmKomgaMatch(seriesId, payload) {
    const modal = document.getElementById('komga-match-modal');
    const context = modal.dataset.context || 'card';

    try {
        const response = await fetch(`/api/series/${seriesId}/komga-match`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();

        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            return;
        }

        closeKomgaMatchModal();
        updateCachedKomgaStatus(seriesId, data);

        // context 'modal' = fiche série (soit via son propre menu Métadonnées, soit via
        // les boutons Matcher de "Éditer manuellement") - rafraîchissement complet
        // plutôt qu'un patch ciblé du seul badge de statut (voir confirmEbdzMatch)
        if (context === 'modal') {
            await renderSeriesDetail(seriesId);
            return;
        }

        const container = document.getElementById(`komga-status-card-${seriesId}`);
        if (container) {
            container.innerHTML = buildKomgaStatusHtml({
                seriesId, context, matchStatus: data.match_status, matchedTitle: data.matched_title,
                komgaUrl: data.komga_url
            });
        }
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Retire le matching Komga d'une série (repasse en état "non matché")
async function unmatchKomgaSeries(seriesId, context) {
    if (!confirm('Retirer le match Komga de cette série ?')) return;

    try {
        const response = await fetch(`/api/series/${seriesId}/komga-unmatch`, { method: 'POST' });
        const data = await response.json();

        if (!data.success) {
            alert('❌ Erreur: ' + (data.error || 'Erreur inconnue'));
            return;
        }

        const containerId = context === 'modal' ? `komga-status-modal-${seriesId}` : `komga-status-card-${seriesId}`;
        const container = document.getElementById(containerId);
        if (container) {
            container.innerHTML = buildKomgaStatusHtml({ seriesId, context, matchStatus: 'unmatched' });
            if (context === 'modal') refreshKomgaToolbarButton(seriesId, null);
        }
        updateCachedKomgaStatus(seriesId, {
            match_status: 'unmatched', komga_series_id: null, komga_url: null,
            komga_cover_path: null, matched_title: null
        });
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
    }
}

// Scoring/tri/rendu des lignes du tableau de résultats (detectResultFormat,
// detectResultResolution, compareSearchResults, buildSearchResultRowHtml,
// buildSearchResultsTableHtml) vivent maintenant dans static/js/search-results-table.js,
// partagé avec la page Recherche (search.js) qui affiche le même tableau - voir ce fichier
// pour l'implémentation, series-detail.html doit charger ce script avant celui-ci.

// Modale de résultats de recherche (tome manquant ou remplacement d'un tome possédé, voir
// searchMissingVolume) - tableau compact unique trié par pertinence (voir
// compareSearchResults). Remplace l'ancien affichage en
// grosses cartes séparées EBDZ/Prowlarr (bannières dégradées, grilles espacées) qui
// prenait beaucoup de place à l'écran pour peu d'information utile par résultat.
// Contexte de la recherche actuellement affichée dans la modale (search-ed2k-modal) -
// alimente la recherche manuelle en haut à droite ("met en haut à droite une recherche
// manuelle donc je peux changer le titre comme je veux"): le titre suivi en base
// (seriesTitle) ne correspond pas toujours à la façon dont une release est réellement
// nommée chez les indexeurs, ce champ permet de relancer la recherche avec un texte
// différent sans quitter la modale, à tout moment (pas seulement quand elle ne renvoie
// rien - voir aussi _renderNoSearchResults pour le même besoin côté "aucun résultat").
let _searchModalRetryContext = null;

function _searchModalManualRetry() {
    const input = document.getElementById('search-modal-manual-title');
    const customTitle = input.value.trim();
    if (!customTitle || !_searchModalRetryContext) return;
    const ctx = _searchModalRetryContext;
    searchMissingVolume(customTitle, ctx.volumeNumber, {
        seriesId: ctx.seriesId, isIntegral: ctx.isIntegral, isHs: ctx.isHs, isEpisode: ctx.isEpisode,
        currentVolumeId: ctx.volumeId
    });
}

function displaySearchResults(seriesTitle, volumeNumber, results, displayLabel, currentFile, prowlarrPending = false, seriesId = null, volumeId = null, searchOptions = {}) {
    const searchModalBody = document.getElementById('search-modal-body');
    _searchModalRetryContext = {
        volumeNumber, seriesId, volumeId,
        isIntegral: !!searchOptions.isIntegral, isHs: !!searchOptions.isHs, isEpisode: !!searchOptions.isEpisode
    };

    // Pas de .search-header ici volontairement (grosse bannière dégradée avec un h2 à
    // 1.8em) - juste un titre compact, pour rester cohérent avec l'objectif "tableau
    // synthétique" plutôt que de garder une grosse en-tête au-dessus d'un tableau serré
    //
    // prowlarrPending: EBDZ (base locale) répond quasi instantanément, Prowlarr
    // (recherche web) est plus lent - les résultats EBDZ s'affichent dès qu'ils arrivent
    // plutôt que d'attendre les deux sources (voir searchMissingVolume), ce indicateur
    // signale que Prowlarr est encore en train de charger et complètera la liste
    // "Formats de recherche" n'exclut plus aucun résultat, seulement un ordre de
    // préférence (voir search-results-table.js) - le compte affiché est donc le compte
    // brut, identique au nombre de lignes réellement affichées dans le tableau.
    const visibleCount = results.length;
    let html = `
        <div style="margin-bottom:12px; display:flex; align-items:flex-start; justify-content:space-between; gap:16px; flex-wrap:wrap;">
            <div>
                <h3 style="margin:0;">🔍 ${escapeHtml(seriesTitle)} - ${escapeHtml(displayLabel || `Volume ${volumeNumber}`)}</h3>
                <p style="color:#666; margin-top:4px; font-size:0.85em;">${visibleCount} ${pluralize(visibleCount, 'résultat')}${visibleCount > 0 ? ' - triés par pertinence' : ''}${prowlarrPending ? ' · 🔄 Recherche en cours...' : ''}</p>
            </div>
            <div style="display:flex; gap:6px; align-items:center; margin-right:64px;">
                <input type="text" id="search-modal-manual-title" value="${escapeHtml(seriesTitle)}" placeholder="Autre terme de recherche" style="max-width:220px;" onkeydown="if(event.key==='Enter') _searchModalManualRetry();">
                <button class="btn-neutral-sm" onclick="_searchModalManualRetry()">${svgIcon('search')} Rechercher</button>
            </div>
        </div>
    `;

    // Tome actuellement possédé (recherche de remplacement uniquement, voir
    // searchMissingVolume) - même ligne que le tableau de résultats juste en dessous
    // (mêmes colonnes Format/Résolution/Taille) pour comparer directement au lieu de
    // devoir rouvrir la fiche série pour se souvenir de ce qu'on a déjà.
    if (currentFile) {
        const currentFormat = (currentFile.format || '?').toUpperCase();
        const currentResolution = currentFile.resolution ? String(currentFile.resolution).match(/\d{3,4}/) : null;
        html += `
            <div class="replace-current-file">
                <div class="replace-current-file-label">📁 Actuellement possédé</div>
                <table class="replace-results-table">
                    <tbody>
                        <tr>
                            <td class="replace-results-filename" title="${escapeHtml(currentFile.filename || '')}">${escapeHtml(currentFile.filename || '')}</td>
                            <td>${escapeHtml(currentFormat)}</td>
                            <td>${currentResolution ? `${currentResolution[0]}px` : (currentFile.resolution ? escapeHtml(String(currentFile.resolution)) : '—')}</td>
                            <td style="white-space:nowrap;">${formatBytes(currentFile.file_size)}</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        `;
    }

    // Le nom de fichier réel du résultat sert de titre suivi (voir trackingTitle côté
    // search-results-table.js) - seul volumeNumber (le tome recherché) est transmis ici en
    // plus de seriesId/volumeId, "get the volume number and album name not from a matching
    // but from when the file was added".
    // "Remplacer quand même" - currentFile n'est non-null QUE pour une recherche de
    // remplacement explicite (voir searchMissingVolume/currentVolumeId) ; filepath en plus
    // par prudence (un tome jamais réellement possédé ne devrait normalement jamais
    // atteindre ici avec un currentFile non-null, mais force_replace n'a de sens que face
    // à un fichier réellement existant à comparer).
    const isReplacementSearch = !!(currentFile && currentFile.filepath);
    const tableHtml = buildSearchResultsTableHtml(results, volumeNumber, seriesId, volumeId, !!searchOptions.preserveFilters, isReplacementSearch);
    if (!tableHtml) {
        html += `
            <div class="no-data">
                <h3>😕 Aucun résultat</h3>
            <p>Aucun lien trouvé. Vérifiez que vos sources sont configurées et activées dans Configuration → Indexeurs (EBDZ, Prowlarr, Telegram ou sources web).</p>
            </div>
        `;
    } else {
        html += tableHtml;
    }

    html += `
        <div style="text-align: center; margin-top: 20px;">
            <button class="btn" onclick="closeSearchModal()">Fermer</button>
        </div>
    `;

    searchModalBody.innerHTML = html;
    initClearableSearchInputs(searchModalBody);

    // Vérifier si aMule est activé pour afficher/cacher les boutons "Ajouter à eMule"
    checkEmuleStatus();
}

// Voir la copie commentée côté static/js/history-shared.js (même logique, gardée en
// synchro manuellement - ce fichier n'est jamais chargé avec history-shared.js dans un
// ordre garanti côté /history et /import, voir templates concernés).
function decodeFilename(filename) {
    if (!filename) return filename;
    let decoded = filename;
    try {
        decoded = decodeURIComponent(decoded);
    } catch (e) {
        // %-encoding invalide - on garde la chaîne telle quelle
    }
    return decoded.replace(/&amp;|&lt;|&gt;|&quot;|&#0?39;|&apos;/g, m => ({
        '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#039;': "'", '&apos;': "'"
    }[m]));
}

function escapeForAttribute(text) {
    return String(text ?? '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

async function copyLink(link, button) {
    try {
        await navigator.clipboard.writeText(link);
        button.textContent = '✓ Copié!';
        button.classList.add('copied');
        setTimeout(() => {
            button.textContent = '📋 Copier';
            button.classList.remove('copied');
        }, 2000);
    } catch (error) {
        alert('Erreur lors de la copie: ' + error);
    }
}

async function addToEmule(link, button, title, seriesId = null, volumeId = null, volumeNumber = null, source = 'ebdz', sourceLink = null, forceReplace = false) {
    // "laisse l'icone comme c'est mais met ajouté. je veux que ce soit la meme chose pour
    // les 3 clients" - icône capturée telle quelle (innerHTML, pas juste textContent) pour
    // pouvoir la remettre à l'identique + "Ajouté" à côté, plutôt que de la remplacer.
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳ Envoi...';
    button.disabled = true;

    try {
        const response = await fetch('/api/emule/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({link: link, title: title, series_id: seriesId, volume_id: volumeId, volume_number: volumeNumber, source, source_link: sourceLink, force_replace: forceReplace})
        });

        const data = await response.json();

        if (data.success) {
            // Reste affiché en permanence (pas de retour à l'état initial): dans une
            // longue liste (ex: tableau de fichiers EBDZ), c'est ce qui permet de
            // retrouver au scroll ce qui a déjà été ajouté. Le bouton reste désactivé
            // volontairement, un ré-ajout n'ayant pas d'intérêt une fois l'envoi réussi.
            // Persisté aussi sur le résultat (voir _markSearchResultAdded,
            // search-results-table.js) - "quand je rajoute un fichier a télécharger et
            // appuie sur l'ordre je perds le bouton just added": un tri de colonne
            // reconstruit tout le tableau depuis les résultats, cet état purement DOM ne
            // survivrait pas sinon.
            if (typeof _markSearchResultAdded === 'function') _markSearchResultAdded(link, 'amule');
            button.innerHTML = `${originalHtml} <span style="font-size:0.85em;">Ajouté</span>`;
            button.classList.add('add-button-added');
        } else {
            throw new Error(data.error || 'Erreur inconnue');
        }
    } catch (error) {
        button.innerHTML = `${originalHtml} <span style="font-size:0.85em;">Erreur</span>`;
        button.classList.add('add-button-error');
        alert('Erreur: ' + error.message);
        setTimeout(() => {
            button.innerHTML = originalHtml;
            button.disabled = false;
            button.classList.remove('add-button-error');
        }, 3000);
    }
}

async function checkEmuleStatus() {
    try {
        const response = await fetch('/api/emule/config');
        const config = await response.json();

        // .result-action-emule uniquement (voir buildSearchResultRowHtml) - ne touche pas
        // aux boutons qBittorrent qui partageaient auparavant la même classe .add-button,
        // ce qui les cachait aussi à tort quand aMule était désactivé
        const addButtons = document.querySelectorAll('.result-action-emule');
        addButtons.forEach(button => {
            button.style.display = config.enabled ? 'inline-block' : 'none';
        });
    } catch (error) {
        console.error('Erreur lors de la vérification du statut aMule:', error);
    }
}

function closeModal() {
    document.getElementById('series-modal').classList.remove('active');
}

function closeSearchModal() {
    document.getElementById('search-ed2k-modal').classList.remove('active');
}

function formatBytes(bytes) {
    if (!bytes) return 'N/A';
    const b = parseInt(bytes);
    if (b === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(b) / Math.log(k));
    return Math.round(b / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

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

// Construit un paragraphe tronqué avec un bouton "Lire plus"/"Réduire" quand le texte
// dépasse maxLength, pour éviter d'afficher de longs résumés en entier d'un coup
function buildTruncatedHtml(text, maxLength, className) {
    if (!text) return '';
    if (text.length <= maxLength) {
        return `<p class="${className}">${escapeHtml(text)}</p>`;
    }
    const truncated = text.slice(0, maxLength).trim() + '…';
    return `
        <p class="${className} truncated-text" data-full="${escapeHtml(text)}" data-truncated="${escapeHtml(truncated)}">
            <span class="truncated-text-content">${escapeHtml(truncated)}</span>
            <button type="button" class="truncated-text-toggle" onclick="event.stopPropagation(); toggleTruncatedText(this)">Lire plus</button>
        </p>
    `;
}

function toggleTruncatedText(btn) {
    const container = btn.closest('.truncated-text');
    const contentEl = container.querySelector('.truncated-text-content');
    const expanded = container.classList.toggle('expanded');
    contentEl.textContent = expanded ? container.dataset.full : container.dataset.truncated;
    btn.textContent = expanded ? 'Réduire' : 'Lire plus';
}

// ===== qBITTORRENT =====
// Ajouter un torrent à qBittorrent avec la catégorie par défaut
async function addTorrentToQbittorrent(torrentUrl, button, title, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    // "laisse l'icone comme c'est mais met ajouté" - même traitement que addToEmule ci-dessus.
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳ Envoi...';
    button.disabled = true;

    try {
        // Charger la config pour obtenir la catégorie par défaut
        const configResponse = await fetch('/api/qbittorrent/config');
        const config = await configResponse.json();

        const payload = {
            torrent_url: torrentUrl,
            title: title,
            series_id: seriesId,
            volume_id: volumeId,
            volume_number: volumeNumber,
            // "dans historique il faudrait voir quelle est la source du téléchargement
            // et cliquable aussi" - source fixe 'prowlarr': ce bouton n'est jamais rendu
            // pour un résultat EBDZ/Telegram/fourtoutici (voir search-results-table.js).
            source: 'prowlarr',
            source_link: sourceLink,
            // "Remplacer quand même" - voir mark_download_pending (downloader.py).
            force_replace: forceReplace
        };

        // Ajouter la catégorie par défaut si elle est configurée
        if (config.default_category) {
            payload.category = config.default_category;
        }

        const response = await fetch('/api/qbittorrent/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });

        const data = await response.json();

        if (data.success) {
            // Reste affiché en permanence (pas de retour à l'état initial): permet de
            // retrouver au scroll ce qui a déjà été ajouté dans une longue liste. Persisté
            // aussi sur le résultat (voir _markSearchResultAdded), même raison que addToEmule.
            if (typeof _markSearchResultAdded === 'function') _markSearchResultAdded(torrentUrl, 'qbittorrent');
            button.innerHTML = `${originalHtml} <span style="font-size:0.85em;">Ajouté</span>`;
            button.classList.add('add-button-added');
        } else {
            throw new Error(data.error || 'Erreur inconnue');
        }
    } catch (error) {
        button.innerHTML = `${originalHtml} <span style="font-size:0.85em;">Erreur</span>`;
        button.classList.add('add-button-error');
        alert('Erreur: ' + error.message);
        setTimeout(() => {
            button.innerHTML = originalHtml;
            button.disabled = false;
            button.classList.remove('add-button-error');
        }, 3000);
    }
}

// rTorrent/Deluge - même comportement que addTorrentToQbittorrent ci-dessus, sans la
// logique de catégorie (pas de notion équivalente câblée côté rTorrent/Deluge pour
// l'instant)
async function addTorrentToClient(clientName, clientLabel, torrentUrl, button, title, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    // "laisse l'icone comme c'est mais met ajouté" - même traitement que addToEmule ci-dessus.
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳ Envoi...';
    button.disabled = true;

    try {
        const response = await fetch(`/api/${clientName}/add`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            // source fixe 'prowlarr': rTorrent/Deluge ne sont proposés que pour un
            // résultat Prowlarr (voir search-results-table.js).
            body: JSON.stringify({ torrent_url: torrentUrl, title: title, series_id: seriesId, volume_id: volumeId, volume_number: volumeNumber, source: 'prowlarr', source_link: sourceLink, force_replace: forceReplace })
        });

        const data = await response.json();

        if (data.success) {
            // Persisté sur le résultat (voir _markSearchResultAdded) - clientName
            // ('rtorrent'/'deluge') correspond exactement à la clé attendue par
            // buildSearchResultRowHtml (added.rtorrent/added.deluge).
            if (typeof _markSearchResultAdded === 'function') _markSearchResultAdded(torrentUrl, clientName);
            button.innerHTML = `${originalHtml} <span style="font-size:0.85em;">Ajouté</span>`;
            button.classList.add('add-button-added');
        } else {
            throw new Error(data.error || 'Erreur inconnue');
        }
    } catch (error) {
        button.innerHTML = `${originalHtml} <span style="font-size:0.85em;">Erreur</span>`;
        button.classList.add('add-button-error');
        alert('Erreur: ' + error.message);
        setTimeout(() => {
            button.innerHTML = originalHtml;
            button.disabled = false;
            button.classList.remove('add-button-error');
        }, 3000);
    }
}

function addTorrentToRtorrent(torrentUrl, button, title, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    return addTorrentToClient('rtorrent', 'rTorrent', torrentUrl, button, title, seriesId, volumeId, volumeNumber, sourceLink, forceReplace);
}

function addTorrentToDeluge(torrentUrl, button, title, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    return addTorrentToClient('deluge', 'Deluge', torrentUrl, button, title, seriesId, volumeId, volumeNumber, sourceLink, forceReplace);
}

// Télécharge un résultat Telegram via la session connectée directement dans un répertoire
// d'import surveillé (voir /api/telegram-channels/download côté serveur) - MANQUAIT
// jusqu'ici sur cette page: buildSearchResultRowHtml (search-results-table.js, partagé
// avec cette page) appelle déjà downloadTelegramFile() pour un résultat Telegram depuis la
// fiche série, mais seul static/js/search.js la définissait - un clic sur ce bouton depuis
// la modale de recherche d'une fiche série levait donc une ReferenceError silencieuse
// (bouton visuellement présent mais totalement inopérant). Même comportement que
// search.js, plus seriesId/volumeId (voir addToEmule ci-dessus).
async function downloadTelegramFile(channel, messageId, button, channelTitle, filename, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳';
    button.disabled = true;

    try {
        const response = await fetch('/api/telegram-channels/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // source fixe 'telegram': ce bouton n'est jamais rendu pour une autre source.
            body: JSON.stringify({
                channel, message_id: messageId, channel_title: channelTitle, filename,
                series_id: seriesId, volume_id: volumeId, volume_number: volumeNumber,
                source: 'telegram', source_link: sourceLink, force_replace: forceReplace
            })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');

        // "laisse l'icone comme c'est mais met ajouté. je veux que ce soit la meme chose
        // pour les 3 clients" - icône conservée + "Ajouté" à côté, même traitement que
        // addToEmule/addTorrentToQbittorrent/addTorrentToClient ci-dessus. Persisté sur le
        // résultat (voir _markTelegramResultAdded), même raison que les autres clients.
        if (typeof _markTelegramResultAdded === 'function') _markTelegramResultAdded(channel, messageId);
        button.innerHTML = `${svgIcon('download')} <span style="font-size:0.85em;">Ajouté</span>`;
        button.classList.add('add-button-added');
        button.setAttribute('data-tooltip', 'Téléchargement démarré - voir sa progression sur la page Import');
        showToast('telegram-dl-' + messageId, `📥 Téléchargement démarré - voir la page Import`, { icon: 'download', autoHideMs: 5000 });
    } catch (error) {
        button.innerHTML = `${svgIcon('download')} <span style="font-size:0.85em;">Erreur</span>`;
        button.classList.add('add-button-error');
        alert('Erreur: ' + error.message);
        setTimeout(() => {
            button.innerHTML = originalHtml;
            button.disabled = false;
            button.classList.remove('add-button-error');
        }, 3000);
    }
}

// Voir downloadFourtoutici dans search.js pour le raisonnement complet - même
// implémentation, dupliquée ici comme downloadTelegramFile juste au-dessus (search.js
// n'est pas chargé sur la fiche série).
async function downloadFourtoutici(fileId, button, filename, seriesId = null, volumeId = null, volumeNumber = null, downloadUrl = null, forceReplace = false) {
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳';
    button.disabled = true;

    try {
        const response = await fetch('/api/fourtoutici/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // source fixe 'fourtoutici': ce bouton n'est jamais rendu pour une autre source.
            body: JSON.stringify({
                file_id: fileId, filename, series_id: seriesId, volume_id: volumeId, volume_number: volumeNumber,
                source: 'fourtoutici', force_replace: forceReplace
            })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');

        if (downloadUrl && typeof _markSearchResultAdded === 'function') _markSearchResultAdded(downloadUrl, 'fourtoutici');
        button.innerHTML = `${svgIcon('download')} <span style="font-size:0.85em;">Ajouté</span>`;
        button.classList.add('add-button-added');
        button.setAttribute('data-tooltip', 'Téléchargement démarré - voir sa progression sur la page Import');
        showToast('fourtoutici-dl-' + fileId, `📥 Téléchargement démarré - voir la page Import`, { icon: 'download', autoHideMs: 5000 });
    } catch (error) {
        button.innerHTML = `${svgIcon('download')} <span style="font-size:0.85em;">Erreur</span>`;
        button.classList.add('add-button-error');
        alert('Erreur: ' + error.message);
        setTimeout(() => {
            button.innerHTML = originalHtml;
            button.disabled = false;
            button.classList.remove('add-button-error');
        }, 3000);
    }
}

window.onclick = function(event) {
    const modal = document.getElementById('series-modal');
    const searchModal = document.getElementById('search-ed2k-modal');
    if (event.target == modal) {
        closeModal();
    }
    if (event.target == searchModal) {
        closeSearchModal();
    }
}

// ========== RENOMMAGE AU FORMAT CONFIGURABLE ==========
// Format personnalisable dans Paramètres > Bibliothèque (voir
// blueprints/settings/rename_config_store.py côté serveur, chargé ici juste pour
// l'affichage d'aide). Chaque bloc entre accolades est omis en entier (texte littéral
// compris) si le tag qu'il contient n'a pas de valeur pour ce fichier - ex: pas d'année
// ComicInfo -> pas de " - (2020)" du tout, plutôt qu'un " - ()" orphelin.

let currentRenameSeriesId = null;
let currentRenameVolumeId = null;
let currentRenameAllVolumes = false;
let customNameDebounceTimer = null;
// Renseigné par les boutons "Renommer..." de la modale "Éditer manuellement" (voir
// renderManualEditModal): permet au bouton "← Retour" de la modale de renommage de
// rouvrir la modale d'édition exactement là où l'utilisateur l'avait laissée (même tome
// déplié) plutôt que de simplement se fermer - null quand la modale de renommage est
// ouverte depuis ailleurs (ex: le hub "Éditer" de la grille bibliothèque), auquel cas
// aucun bouton retour n'a de sens.
let renameModalReturnToEdit = null;

// Ouvre le modal d'aperçu/confirmation de renommage. Sans volumeId ni allVolumes: le
// dossier de la série uniquement. Avec volumeId: un seul tome, jamais le dossier
// (accessible via la modale "Éditer manuellement", pas de menu ⚙️ par tome dédié - voir
// renderManualEditModal). Avec allVolumes: tous les tomes de la série en une seule action,
// toujours pas le dossier (bouton "Renommer les tomes" de la toolbar) - trois actions
// distinctes, jamais combinées
async function openRenameModal(seriesId, seriesTitle, volumeId = null, allVolumes = false, returnToEdit = null, volumeLabel = null) {
    currentRenameSeriesId = seriesId;
    currentRenameVolumeId = volumeId;
    currentRenameAllVolumes = allVolumes;
    renameModalReturnToEdit = returnToEdit;

    let renameModal = document.getElementById('rename-modal');
    if (!renameModal) {
        renameModal = document.createElement('div');
        renameModal.id = 'rename-modal';
        renameModal.className = 'modal rename-modal';
        document.body.appendChild(renameModal);
    }

    // Un nom personnalisé n'a de sens que pour une cible unique (dossier de série ou un
    // seul fichier de tome): pour "tous les tomes", chaque fichier a son propre nom
    // calculé depuis le template, il n'y a rien d'unique à saisir ici
    // "ca met deja au format standard (donc je peux pas) mais j'aimerais quand meme le
    // renommer manuellement. je suis dans un one-shot" - allVolumes=true masquait ce
    // champ inconditionnellement, y compris pour un one-shot (qui n'a qu'UN tome, envoyé
    // via "Renommer tomes"/allVolumes=true faute de bouton dédié "un seul tome" pour ce
    // cas - voir oneshotRenameFileLabel plus haut). volumeLabel n'est renseigné QUE dans
    // ce cas précis (un seul fichier concerné malgré allVolumes) - _apply_custom_volume_name
    // côté serveur a le même garde (len(file_plan) == 1), donc jamais de risque d'appliquer
    // un nom unique à plusieurs tomes d'une vraie série multi-tomes.
    const customNameHtml = (allVolumes && !volumeLabel) ? '' : `
                <div class="rename-section">
                    <h3>Nom personnalisé ${(volumeId || allVolumes) ? 'du fichier' : 'du dossier'}</h3>
                    <input type="text" id="rename-custom-name" class="form-control" placeholder="Laisser vide pour utiliser le format automatique (voir ci-dessus)" oninput="onCustomNameInput()">
                </div>
    `;

    // Case à cocher uniquement pour le renommage du dossier de série (pas pour un tome
    // seul, ni pour "tous les tomes" qui la propose déjà implicitement): permet de
    // renommer aussi tous les tomes dans la même action, sans avoir à rouvrir le menu
    // Actions séparément - reste décoché par défaut, ce sont deux actions distinctes
    const alsoRenameVolumesHtml = (!volumeId && !allVolumes) ? `
                <div class="rename-section">
                    <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                        <input type="checkbox" id="rename-also-volumes" checked onchange="updateRenamePreview()">
                        <span>Renommer aussi tous les tomes de cette série</span>
                    </label>
                </div>
    ` : '';

    const modalTitle = allVolumes ? 'tous les tomes' : (volumeId ? 'ce tome' : 'la série');

    const backButtonHtml = returnToEdit
        ? `<button class="btn-neutral" onclick="backToManualEditFromRename()" style="margin-bottom:12px;">← Retour à l'édition</button>`
        : '';

    renameModal.innerHTML = `
        <div class="modal-content rename-modal-content">
            <span class="close-modal" onclick="closeRenameModal()">×</span>
            <div class="rename-modal-header">
                ${backButtonHtml}
                <h2>✏️ Renommer ${modalTitle}</h2>
                <p class="rename-modal-subtitle">Série: <strong>${escapeHtml(seriesTitle)}</strong></p>
                <!-- "dans renommer afficher le nom du tome sous le nom série" - jusqu'ici,
                     renommer UN tome précis affichait le même en-tête générique ("Renommer
                     ce tome" + le nom de la série) que renommer tout/le dossier, sans jamais
                     dire LEQUEL des tomes est concerné avant de lire l'aperçu plus bas. -->
                ${volumeLabel ? `<p class="rename-modal-subtitle">Tome: <strong>${escapeHtml(volumeLabel)}</strong></p>` : ''}
                <p class="rename-help-text" id="rename-format-help">Format: <em>chargement...</em></p>
            </div>

            <div class="rename-modal-body">
                ${customNameHtml}
                ${alsoRenameVolumesHtml}
                <div class="rename-section">
                    <h3>Aperçu du renommage</h3>
                    <div id="rename-preview-container" class="rename-preview-container">
                        <div class="loading" style="padding: 20px;">
                            <div class="spinner"></div>
                            <p>Calcul de l'aperçu...</p>
                        </div>
                    </div>
                </div>

                <div style="display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px;">
                    <button onclick="closeRenameModal()" class="btn-neutral">Annuler</button>
                    <button id="rename-execute-btn" onclick="executeRename()" class="btn" style="background: #10b981;" disabled>${svgIcon('check')} Appliquer le renommage</button>
                </div>
            </div>
        </div>
    `;

    renameModal.classList.add('active');
    loadRenameFormatHelp(volumeId, allVolumes);
    await updateRenamePreview();
}

// Debounce: on ne relance l'aperçu (appel serveur) qu'une fois l'utilisateur arrêté de
// taper, sinon chaque frappe déclencherait un aller-retour réseau
function onCustomNameInput() {
    clearTimeout(customNameDebounceTimer);
    customNameDebounceTimer = setTimeout(() => updateRenamePreview(), 400);
}

// Affiche le format actuellement configuré (Paramètres > Bibliothèque) plutôt qu'un
// texte figé, pour que le modal reflète toujours le format réellement appliqué
async function loadRenameFormatHelp(volumeId, allVolumes = false) {
    const help = document.getElementById('rename-format-help');
    if (!help) return;
    try {
        const response = await fetch('/api/settings/rename');
        const config = await response.json();
        // Actions distinctes avec leurs propres paramètres (voir Paramètres >
        // Bibliothèque): renommer un/tous les tome(s) ne touche que ce(s) fichier(s),
        // renommer la série ne touche que son dossier - jamais combiné - donc une seule
        // ligne pertinente à la fois ici, pas les deux.
        const line = (volumeId || allVolumes)
            ? `📄 Nom des fichiers (tomes): <code>${escapeHtml(config.volume_template)}</code>`
            : `📁 Nom du dossier (série): <code>${escapeHtml(config.series_template)}</code>`;
        help.innerHTML = `<div>${line}</div>`;
    } catch (error) {
        help.textContent = '';
    }
}

function closeRenameModal() {
    const modal = document.getElementById('rename-modal');
    if (modal) {
        modal.classList.remove('active');
    }
    currentRenameAllVolumes = false;
    currentRenameSeriesId = null;
    currentRenameVolumeId = null;
    renameModalReturnToEdit = null;
}

// Bouton "← Retour à l'édition" (voir backButtonHtml dans openRenameModal): ne rouvre la
// modale d'édition manuelle que si elle a bien été quittée pour arriver ici (voir
// renameModalReturnToEdit) - une nouvelle requête GET plutôt qu'un état restauré, pour
// repartir sur des données à jour si un renommage vient d'être appliqué entre-temps.
function backToManualEditFromRename() {
    const returnTo = renameModalReturnToEdit;
    if (!returnTo) return;
    closeRenameModal();
    openManualEditModal(returnTo.seriesId, returnTo.volumeId);
}

// Appelle l'aperçu de renommage et retourne le JSON, ou { error } sur échec HTTP/réseau -
// factorisé car updateRenamePreview doit parfois faire deux appels (dossier + tomes,
// voir la case "Renommer aussi tous les tomes")
async function fetchRenamePreview(requestBody) {
    try {
        const response = await fetch(`/api/series/${currentRenameSeriesId}/rename/preview`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody)
        });
        if (!response.ok) {
            const text = await response.text();
            return { error: `Erreur serveur ${response.status}: ${text.substring(0, 200)}` };
        }
        return await response.json();
    } catch (error) {
        return { error: `Erreur de connexion: ${error.message}` };
    }
}

async function updateRenamePreview() {
    const previewContainer = document.getElementById('rename-preview-container');
    const executeBtn = document.getElementById('rename-execute-btn');

    try {
        const customNameInput = document.getElementById('rename-custom-name');
        const customName = customNameInput ? customNameInput.value.trim() : '';
        const alsoVolumesCheckbox = document.getElementById('rename-also-volumes');
        const alsoVolumes = alsoVolumesCheckbox ? alsoVolumesCheckbox.checked : false;

        const requestBody = {
            ...(currentRenameAllVolumes ? { all_volumes: true } : (currentRenameVolumeId ? { volume_id: currentRenameVolumeId } : {})),
            ...(customName ? { custom_name: customName } : {})
        };

        const data = await fetchRenamePreview(requestBody);

        if (data.error) {
            previewContainer.innerHTML = `<div class="error-message">❌ ${escapeHtml(data.error)}</div>`;
            executeBtn.disabled = true;
            return;
        }

        // Case "Renommer aussi tous les tomes" cochée (dossier de série uniquement, voir
        // openRenameModal): deuxième appel pour prévisualiser aussi le renommage des
        // tomes, fusionné avec l'aperçu du dossier ci-dessus dans la même liste
        if (alsoVolumes) {
            const volumesData = await fetchRenamePreview({ all_volumes: true });
            if (!volumesData.error) {
                data.files = volumesData.files || [];
            }
        }

        // Le champ "Nom personnalisé" (dossier ou fichier) reste vide par défaut, jamais
        // pré-rempli avec le nom actuel: l'utilisateur ne veut pas voir l'ancien nom
        // recopié dedans, que ce soit pour un dossier ou un tome.

        const changedFiles = (data.files || []).filter(f => f.changed);
        const folderChanged = data.folder && data.folder.changed;

        const folderHtml = folderChanged ? `
            <div class="rename-preview-item rename-preview-folder">
                <div class="rename-preview-old">
                    <span class="rename-preview-label">📁 Dossier avant:</span>
                    <code>${escapeHtml(data.folder.old_path)}</code>
                </div>
                <div class="rename-preview-arrow">→</div>
                <div class="rename-preview-new">
                    <span class="rename-preview-label">📁 Dossier après:</span>
                    <code>${escapeHtml(data.folder.new_path)}</code>
                </div>
            </div>
        ` : (data.folder && data.folder.error) ? `
            <div class="error-message">⚠️ Dossier non renommable: ${escapeHtml(data.folder.error)}</div>
        ` : '';

        const filesHtml = (data.files || []).map(item => `
            <div class="rename-preview-item${item.changed ? '' : ' rename-preview-unchanged'}">
                <div class="rename-preview-old">
                    <span class="rename-preview-label">Avant:</span>
                    <code>${escapeHtml(item.old_name)}</code>
                </div>
                <div class="rename-preview-arrow">${item.changed ? '→' : '='}</div>
                <div class="rename-preview-new">
                    <span class="rename-preview-label">Après:</span>
                    <code>${escapeHtml(item.new_name)}</code>
                </div>
            </div>
        `).join('');

        if (changedFiles.length === 0 && !folderChanged) {
            previewContainer.innerHTML = `
                ${folderHtml}
                <p style="color: #999; text-align: center; padding: 20px;">
                    Déjà au format standard, rien à renommer
                </p>
            `;
            executeBtn.disabled = true;
        } else {
            previewContainer.innerHTML = `<div class="rename-preview-list">${folderHtml}${filesHtml}</div>`;
            executeBtn.disabled = false;
        }
    } catch (error) {
        previewContainer.innerHTML = `<div class="error-message">❌ Erreur: ${escapeHtml(error.message)}</div>`;
        executeBtn.disabled = true;
    }
}

// Appelle l'exécution du renommage et retourne le JSON, ou lève une erreur sur échec
// HTTP/réseau - factorisé car executeRename doit parfois faire deux appels (tomes puis
// dossier, voir la case "Renommer aussi tous les tomes")
async function executeRenameRequest(requestBody) {
    const response = await fetch(`/api/series/${currentRenameSeriesId}/rename/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody)
    });
    if (!response.ok) {
        const text = await response.text();
        throw new Error(`Erreur serveur ${response.status}: ${text.substring(0, 200)}`);
    }
    return await response.json();
}

async function executeRename() {
    const alsoVolumesCheckbox = document.getElementById('rename-also-volumes');
    const alsoVolumes = alsoVolumesCheckbox ? alsoVolumesCheckbox.checked : false;

    const confirmMessage = currentRenameAllVolumes
        ? 'Êtes-vous sûr de vouloir renommer tous les tomes de cette série ?\n\nCette action ne peut pas être annulée.'
        : (currentRenameVolumeId
            ? 'Êtes-vous sûr de vouloir renommer ce fichier ?\n\nCette action ne peut pas être annulée.'
            : (alsoVolumes
                ? 'Êtes-vous sûr de vouloir renommer le dossier de cette série ET tous ses tomes ?\n\nCette action ne peut pas être annulée.'
                : 'Êtes-vous sûr de vouloir renommer le dossier de cette série ?\n\nCette action ne peut pas être annulée.'));
    if (!confirm(confirmMessage)) {
        return;
    }

    const btn = document.getElementById('rename-execute-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Renommage en cours...';
    showToast('rename', 'Renommage en cours...');

    try {
        const customNameInput = document.getElementById('rename-custom-name');
        const customName = customNameInput ? customNameInput.value.trim() : '';

        const requestBody = {
            ...(currentRenameAllVolumes ? { all_volumes: true } : (currentRenameVolumeId ? { volume_id: currentRenameVolumeId } : {})),
            ...(customName ? { custom_name: customName } : {})
        };

        // Renomme d'abord les tomes (nouveaux noms de fichiers), puis le dossier: le
        // dossier utilise déjà les noms de fichiers à jour en base pour recalculer
        // volumes.filepath, donc cet ordre garantit une base cohérente à chaque étape
        let data;
        if (alsoVolumes) {
            const volumesData = await executeRenameRequest({ all_volumes: true });
            if (volumesData.error) {
                alert(`Erreur: ${volumesData.error}`);
                btn.disabled = false;
                btn.innerHTML = `${svgIcon('check')} Appliquer le renommage`;
                return;
            }
            const folderData = await executeRenameRequest(requestBody);
            data = { ...folderData, files: volumesData.files };
        } else {
            data = await executeRenameRequest(requestBody);
        }

        if (data.error) {
            alert(`Erreur: ${data.error}`);
            btn.disabled = false;
            btn.innerHTML = `${svgIcon('check')} Appliquer le renommage`;
            return;
        }

        const renamedFiles = (data.files || []).filter(f => f.success && !f.skipped).length;
        const failedFiles = (data.files || []).filter(f => !f.success).length;
        const movedFiles = Number(data.folder?.moved_files || 0);

        // Any file or folder error makes the whole operation a failure in the
        // result dialog. Do not show the green validation when only part succeeded.
        const folderFailed = Boolean(data.folder && !data.folder.success);
        const renameFailed = failedFiles > 0 || folderFailed;
        let resultMessage = renameFailed ? '❌ Renommage échoué !' : '✅ Renommage terminé !';
        if (renamedFiles > 0) {
            resultMessage += `

${renamedFiles} ${pluralize(renamedFiles, 'fichier')} ${pluralize(renamedFiles, 'renommé')}`;
        }
        if (failedFiles > 0) {
            resultMessage += `
❌ ${failedFiles} ${pluralize(failedFiles, 'erreur')} sur des fichiers`;
        }
        if (data.folder) {
            if (data.folder.success && data.folder.merged_into_existing) {
                resultMessage += `
📁 ${movedFiles} fichier(s) déplacé(s) dans le dossier existant`;
            } else if (data.folder.success && data.folder.changed) {
                resultMessage += `
📁 Dossier de la série renommé`;
            } else if (!data.folder.success) {
                resultMessage += `
❌ Dossier non renommé: ${data.folder.error}`;
            }
        }

        showToast('komga-scan', 'Scan Komga demandé', { icon: 'radio', autoHideMs: 4000 });
        alert(resultMessage);

        // Fermer le modal de renommage et recharger la vue de détail pour voir les
        // nouveaux noms (et le nouveau chemin si le dossier a été renommé)
        const seriesId = currentRenameSeriesId;
        closeRenameModal();
        renderSeriesDetail(seriesId);

    } catch (error) {
        alert(`Erreur: ${error.message}`);
        btn.disabled = false;
        btn.innerHTML = `${svgIcon('check')} Appliquer le renommage`;
    } finally {
        dismissToast('rename');
    }
}

// Fermer le modal de renommage quand on clique en dehors
document.addEventListener('click', function(event) {
    const renameModal = document.getElementById('rename-modal');
    if (renameModal && event.target == renameModal) {
        closeRenameModal();
    }
    const coverModal = document.getElementById('cover-image-modal');
    if (coverModal && event.target === coverModal) {
        closeCoverModal();
    }
});

document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
        const coverModal = document.getElementById('cover-image-modal');
        if (coverModal && coverModal.classList.contains('active')) {
            closeCoverModal();
        }
    }
});

// Ne charger les données que si on est sur la page de détails d'une bibliothèque
if (libraryId) {
    window.addEventListener('load', async function() {
        // "make sure that if komga or ebdz is not configured they dont show up in the
        // table" - attendu ici (pas juste laissé tourner en fond côté nav.js) car
        // renderTableColumnsMenu (juste en dessous) doit déjà filtrer les colonnes
        // "Matching EBDZ"/"Matching Komga" dès le tout premier rendu, pas seulement à
        // partir du rechargement suivant une fois le fetch de nav.js arrivé.
        await refreshEnabledIntegrations();
        restoreFilterState();
        updateViewSwitcherButtons();
        renderTableColumnsMenu();
        loadLibraryInfo();
        loadLibraryData();
        startLibraryFreshnessPolling();
    });
}
// Navigation clavier ←/→ entre séries adjacentes (fiche série uniquement, voir
// adjacentSeriesNav rempli par renderSeriesDetail) - même destination que les boutons
// ⬅️/➡️ du bandeau d'actions, juste un raccourci en plus. Ignoré si un modal est ouvert
// (les flèches doivent alors se comporter normalement, ex: déplacer le curseur dans un
// champ) ou si le focus est déjà dans un champ de saisie.
document.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    if (!currentSeriesDetail) return; // pas sur une fiche série

    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA'
                   || active.tagName === 'SELECT' || active.isContentEditable)) return;
    if (document.querySelector('.modal.active, .modal.show')) return;

    const target = e.key === 'ArrowLeft' ? adjacentSeriesNav.prev : adjacentSeriesNav.next;
    if (target) navigateToSeriesInPage(target.id);
});
