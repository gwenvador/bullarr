/**
 * Script pour la page de découverte et d'ajout de séries
 */

// État global
let currentState = {
    selectedSeries: null,
    selectedLibrary: null,
    libraries: []
};

// Cache des infos détaillées Bedetheque déjà récupérées pour les candidats de l'étape 1
// (url -> info), pour éviter de refaire une requête si l'utilisateur sélectionne un
// résultat déjà chargé
let candidateDetailsCache = {};

// Incrémenté à chaque nouvelle recherche (voir displayBedethequeCandidates): les ids
// bedetheque-detail-N étant réutilisés d'une recherche à l'autre, une réponse lente
// d'une recherche précédente qui arrive après le lancement d'une nouvelle retrouvait un
// élément du même id toujours présent dans le DOM et écrasait les résultats de la
// recherche en cours avec les anciens. loadCandidateDetailsSequentially capture ce
// compteur à son lancement et abandonne dès qu'il ne correspond plus à la génération
// courante, plutôt que de se fier à la seule présence de l'élément dans le DOM.
let searchGeneration = 0;

// Résultats complets de la recherche en cours et nombre actuellement affiché (voir
// displayBedethequeCandidates/loadMoreDiscoverCandidates) - la recherche Bedetheque
// renvoie tout en un coup, seul l'affichage (et donc le chargement séquentiel des
// détails, coûteux car anti-bot) est paginé, pour ne pas noyer les résultats les plus
// pertinents (en tête de recherche) sous une longue liste de séries proches/dérivées.
let discoverCandidates = [];
let discoverDisplayedCount = 0;
const DISCOVER_PAGE_SIZE = 5;

// URL Bedetheque -> {seriesId, title, libraryName} pour les séries déjà en bibliothèque
// ("si je fais une recherche dans découvrir et que j'ai deja la série il ne me l'indique
// pas"), rechargé une fois par page (pas par recherche: la bibliothèque ne change pas
// pendant qu'on cherche une série à ajouter).
let ownedBedethequeUrls = null;

async function loadOwnedBedethequeUrls() {
    if (ownedBedethequeUrls) return ownedBedethequeUrls;
    const map = new Map();
    try {
        const libraries = await (await fetch('/api/libraries')).json();
        await Promise.all(libraries.map(async (lib) => {
            const series = await (await fetch(`/api/library/${lib.id}/series`)).json();
            series.forEach(s => {
                if (s.bedetheque_url) {
                    map.set(s.bedetheque_url, { seriesId: s.id, title: s.title, libraryName: lib.name });
                }
            });
        }));
    } catch (error) {
        console.error('Erreur chargement des séries déjà en bibliothèque:', error);
    }
    ownedBedethequeUrls = map;
    return ownedBedethequeUrls;
}

// ============================================
// ÉTAPE 1: Recherche sur Bedetheque
// ============================================

async function searchBedethequeSeries() {
    const seriesName = document.getElementById('seriesNameInput').value.trim();

    if (!seriesName) {
        showError('step-1', 'Veuillez entrer un nom de série');
        return;
    }

    // Incrémenté ici, au lancement de la recherche elle-même (pas seulement à l'affichage
    // des résultats, voir displayBedethequeCandidates): sans ça, une recherche A lente
    // peut renvoyer sa réponse APRÈS qu'une recherche B plus rapide ait déjà affiché les
    // siens, et écraser les résultats de B avec ceux, périmés, de A.
    searchGeneration++;
    const generation = searchGeneration;

    // Une nouvelle recherche invalide toute sélection précédente (série/bibliothèque déjà
    // choisies pour une autre recherche) - on referme les étapes suivantes
    currentState.selectedSeries = null;
    currentState.selectedLibrary = null;
    hideElement('step-2');
    hideElement('step-3');
    hideError('step-2');
    hideError('step-3');

    showLoading('step-1', true);
    hideError('step-1');
    hideElement('bedetheque-search-results');

    // "why search in historique is not showing... i searched chroniques" - seule
    // searchSources() (étape 2, recherche de SOURCES) journalisait jusqu'ici côté
    // Découvrir, pas cette recherche de SÉRIE (étape 1, Bédéthèque) - même angle mort que
    // searchBedetheque() côté /search (voir static/js/search.js).
    if (typeof logSearchHistoryEvent === 'function') logSearchHistoryEvent(seriesName, 'Recherche de série (Découvrir)');

    try {
        const [response] = await Promise.all([
            fetch(`/api/bedetheque/search?q=${encodeURIComponent(seriesName)}`),
            loadOwnedBedethequeUrls()
        ]);
        const data = await response.json();

        if (generation !== searchGeneration) return; // une recherche plus récente a démarré entre-temps

        if (!data.success) {
            showError('step-1', data.error || 'Erreur lors de la recherche');
            return;
        }

        const results = data.results || [];

        if (results.length === 0) {
            showError('step-1', 'Aucune série trouvée. Essayez d\'autres mots-clés.');
            return;
        }

        discoverCandidates = results;
        discoverDisplayedCount = Math.min(DISCOVER_PAGE_SIZE, results.length);
        displayBedethequeCandidates(generation);
        showElement('bedetheque-search-results');

    } catch (error) {
        if (generation !== searchGeneration) return;
        console.error('Erreur:', error);
        showError('step-1', 'Erreur lors de la recherche: ' + error.message);
    } finally {
        if (generation === searchGeneration) showLoading('step-1', false);
    }
}

// Affiche les séries Bedetheque candidates pour le nom recherché à l'étape 1 (choix de
// LA série à ajouter). À ne pas confondre avec searchSources plus bas, qui affiche les
// résultats EBDZ/Prowlarr trouvés à l'étape 3 pour une série déjà sélectionnée
function displayBedethequeCandidates(generation) {
    const resultsList = document.getElementById('results-list');
    const resultsCount = document.getElementById('results-count');

    resultsList.innerHTML = '';
    resultsCount.textContent = discoverCandidates.length;
    candidateDetailsCache = {};
    const filterInput = document.getElementById('results-filter');
    if (filterInput) filterInput.value = '';

    appendDiscoverCandidateCards(0, discoverDisplayedCount, generation);
    loadCandidateDetailsSequentially(0, discoverDisplayedCount, generation);
}

// Crée les cartes des candidats [fromIndex, toIndex) et les insère avant le bouton
// "Charger plus" (plutôt qu'à la fin de la liste, où elles atterriraient après lui) -
// les indices utilisés pour les ids de slot (bedetheque-detail-N) restent stables d'un
// "Charger plus" à l'autre car basés sur la position dans discoverCandidates, pas sur le
// nombre de cartes déjà rendues.
function appendDiscoverCandidateCards(fromIndex, toIndex, generation) {
    const resultsList = document.getElementById('results-list');
    const loadMoreBtn = document.getElementById('discover-load-more-btn');

    for (let index = fromIndex; index < toIndex; index++) {
        const result = discoverCandidates[index];
        // "si je fais une recherche dans découvrir et que j'ai deja la série il ne me
        // l'indique pas donc je vais de nouveau l'installer" - marque la carte + bloque
        // le clic direct derrière une confirmation plutôt que de foncer sur le flux de
        // création (qui créerait une série en double)
        const owned = ownedBedethequeUrls ? ownedBedethequeUrls.get(result.url) : null;
        const div = document.createElement('div');
        div.className = 'result-item' + (owned ? ' result-item-owned' : '');
        div.dataset.url = result.url;
        div.onclick = () => {
            if (owned && !confirm(
                `« ${result.title} » est déjà dans votre bibliothèque (${owned.libraryName}).\n\n` +
                `Continuer réutilisera la série existante et ne supprimera aucun fichier ni aucune donnée.`
            )) return;
            selectSeries(result.url, result.title, div);
        };
        div.innerHTML = `
            <div class="result-title-row">
                <p class="result-title">${escapeHtml(result.title)}</p>
                ${owned ? `<a href="/series/${owned.seriesId}" target="_blank" class="badge badge-owned" title="Déjà en bibliothèque - ouvrir la fiche" onclick="event.stopPropagation()">📚 Déjà en bibliothèque</a>` : ''}
                <span class="badges" id="bedetheque-badges-${index}"></span>
                <a href="${escapeAttr(result.url)}" target="_blank" class="series-link-icon" title="Voir sur Bedetheque" onclick="event.stopPropagation()">
                    <img src="/static/img/bedetheque-logo.png" alt="Bedetheque">
                </a>
            </div>
            <p class="result-url">${escapeHtml(result.genre || '')}</p>
            <div class="result-details" id="bedetheque-detail-${index}">
                <span class="result-detail-loading">⏳ Chargement des infos...</span>
            </div>
        `;
        if (loadMoreBtn) resultsList.insertBefore(div, loadMoreBtn);
        else resultsList.appendChild(div);
    }

    renderDiscoverLoadMoreButton(generation);
}

function renderDiscoverLoadMoreButton(generation) {
    const resultsList = document.getElementById('results-list');
    let btn = document.getElementById('discover-load-more-btn');
    const remaining = discoverCandidates.length - discoverDisplayedCount;

    if (remaining <= 0) {
        if (btn) btn.remove();
        return;
    }

    if (!btn) {
        btn = document.createElement('button');
        btn.id = 'discover-load-more-btn';
        btn.className = 'btn';
        btn.type = 'button';
        btn.style.marginTop = '10px';
        resultsList.appendChild(btn);
    }
    btn.innerHTML = `${svgIcon('chevron-down')} Charger plus (${remaining} restante${remaining > 1 ? 's' : ''})`;
    btn.onclick = () => loadMoreDiscoverCandidates(generation);
}

function loadMoreDiscoverCandidates(generation) {
    if (generation !== searchGeneration) return; // la recherche affichée a changé entre-temps
    const previousCount = discoverDisplayedCount;
    discoverDisplayedCount = Math.min(discoverDisplayedCount + DISCOVER_PAGE_SIZE, discoverCandidates.length);
    appendDiscoverCandidateCards(previousCount, discoverDisplayedCount, generation);
    loadCandidateDetailsSequentially(previousCount, discoverDisplayedCount, generation);
}

// Récupère les infos détaillées (couverture, tomes, description) de chaque candidat de
// l'intervalle [fromIndex, toIndex) les unes après les autres et les affiche dès qu'elles
// arrivent, sans attendre les autres - une recherche large sur Bedetheque étant lente par
// requête (anti-bot), tout charger en parallèle la ferait bannir ; ce séquencement montre
// au moins le premier résultat vite. Bornée à l'intervalle affiché (pas toute
// discoverCandidates): "Charger plus" rappelle cette fonction seulement sur la nouvelle
// tranche, les candidats déjà affichés ne sont pas rechargés.
async function loadCandidateDetailsSequentially(fromIndex, toIndex, generation) {
    for (let index = fromIndex; index < toIndex; index++) {
        if (generation !== searchGeneration) return; // une recherche plus récente a démarré entre-temps
        const result = discoverCandidates[index];
        const slot = document.getElementById(`bedetheque-detail-${index}`);
        if (!slot) return;

        try {
            const response = await fetch(`/api/bedetheque/info?url=${encodeURIComponent(result.url)}`);
            const data = await response.json();
            if (generation !== searchGeneration) return;

            const currentSlot = document.getElementById(`bedetheque-detail-${index}`);
            if (!currentSlot) return;

            if (data.success) {
                candidateDetailsCache[result.url] = data.info;
                currentSlot.innerHTML = renderCandidateDetailHtml(data.info);
                const badgesSlot = document.getElementById(`bedetheque-badges-${index}`);
                if (badgesSlot) badgesSlot.innerHTML = renderCandidateBadgesHtml(data.info);
            } else {
                currentSlot.innerHTML = _reloadCandidateDetailButtonHtml(index);
            }
        } catch (error) {
            console.error('Erreur récupération infos candidat Bedetheque:', error);
            const currentSlot = document.getElementById(`bedetheque-detail-${index}`);
            if (currentSlot) currentSlot.innerHTML = _reloadCandidateDetailButtonHtml(index);
        }
    }
}

// "ajoute un bouton recharger que je peux trigger manuellement sur la serie de
// bedetheque.com quand c'est stoppé" - un candidat dont le chargement des infos a été
// interrompu (sélection d'un autre candidat, échec réseau, ou pas de résultat Bedetheque)
// n'affichait plus rien du tout après le correctif du point #3 (voir selectSeries) - au
// lieu de laisser ce vide définitif, un bouton permet de redéclencher la récupération à
// la demande pour CE candidat précis, sans relancer toute la recherche.
function _reloadCandidateDetailButtonHtml(index) {
    return `<button type="button" class="btn-link result-detail-reload" onclick="event.stopPropagation(); reloadCandidateDetail(${index})">${svgIcon('refresh-cw')} Recharger</button>`;
}

async function reloadCandidateDetail(index) {
    const result = discoverCandidates[index];
    const slot = document.getElementById(`bedetheque-detail-${index}`);
    if (!result || !slot) return;

    const generation = searchGeneration;
    slot.innerHTML = '<span class="result-detail-loading">⏳ Chargement des infos...</span>';

    try {
        const response = await fetch(`/api/bedetheque/info?url=${encodeURIComponent(result.url)}`);
        const data = await response.json();
        if (generation !== searchGeneration) return; // une nouvelle recherche a démarré pendant le rechargement

        const currentSlot = document.getElementById(`bedetheque-detail-${index}`);
        if (!currentSlot) return;

        if (data.success) {
            candidateDetailsCache[result.url] = data.info;
            currentSlot.innerHTML = renderCandidateDetailHtml(data.info);
            const badgesSlot = document.getElementById(`bedetheque-badges-${index}`);
            if (badgesSlot) badgesSlot.innerHTML = renderCandidateBadgesHtml(data.info);
        } else {
            currentSlot.innerHTML = _reloadCandidateDetailButtonHtml(index);
        }
    } catch (error) {
        console.error('Erreur rechargement infos candidat Bedetheque:', error);
        if (generation !== searchGeneration) return;
        const currentSlot = document.getElementById(`bedetheque-detail-${index}`);
        if (currentSlot) currentSlot.innerHTML = _reloadCandidateDetailButtonHtml(index);
    }
}

// Tags affichés en haut de la carte, à côté du nom de la série (voir result-title-row)
// "remove the grey background in the details information. I would prefer a smaller and
// more light interface" - texte simple séparé par des points médians (même traitement que
// series-authors) plutôt que des pastilles .badge (fond gris #e0e0e0, style.css), trop
// lourd visuellement pour de simples métadonnées de candidat.
function renderCandidateBadgesHtml(info) {
    const parts = [];
    if (info.total_volumes) parts.push(`📚 ${info.total_volumes} tome${info.total_volumes > 1 ? 's' : ''}`);
    if (info.genre) parts.push(`🎭 ${escapeHtml(info.genre)}`);
    if (info.status) parts.push(escapeHtml(info.status));
    if (info.year_start) parts.push(`📅 ${info.year_start}${info.year_end ? '-' + info.year_end : ''}`);
    return parts.length ? `<span class="candidate-badges-text">${parts.join(' · ')}</span>` : '';
}

function renderCandidateDetailHtml(info) {
    const authors = [];
    if (info.scenaristes && info.scenaristes.length) authors.push(`<strong>Scénario:</strong> ${escapeHtml(info.scenaristes.join(', '))}`);
    if (info.dessinateurs && info.dessinateurs.length) authors.push(`<strong>Dessin:</strong> ${escapeHtml(info.dessinateurs.join(', '))}`);
    if (info.editeurs && info.editeurs.length) authors.push(`<strong>Éditeur:</strong> ${escapeHtml(info.editeurs.join(', '))}`);

    return `
        <div class="result-details-loaded">
            ${info.cover_path ? `<img class="result-cover-thumb" src="/${escapeAttr(info.cover_path)}" alt="">` : ''}
            <div class="result-detail-text">
                ${info.description ? `<p class="result-description">${escapeHtml(info.description)}</p>` : ''}
                ${authors.length ? `<p class="series-authors">${authors.join(' · ')}</p>` : ''}
            </div>
        </div>
    `;
}

// Filtre local (sans nouvelle requête) sur les cartes déjà chargées à l'étape 1: utile
// quand Bedetheque renvoie beaucoup de séries proches (spin-offs, tomes seuls, etc.)
function filterBedethequeResults() {
    const query = document.getElementById('results-filter').value.trim().toLowerCase();
    let visibleCount = 0;
    document.querySelectorAll('#results-list .result-item').forEach(el => {
        const title = (el.querySelector('.result-title')?.textContent || '').toLowerCase();
        const visible = !query || title.includes(query);
        el.style.display = visible ? '' : 'none';
        if (visible) visibleCount++;
    });
    document.getElementById('results-count').textContent = visibleCount;
}

async function selectSeries(url, title, cardEl) {
    // "quand j'ai selectionné une entrée dans découvrir arreter de charger bedetheque" -
    // une fois un candidat choisi, plus besoin des détails des autres candidats affichés:
    // incrémenter searchGeneration fait sortir loadCandidateDetailsSequentially à sa
    // prochaine vérification (même garde que pour une nouvelle recherche démarrée), au
    // lieu de continuer à consommer le quota anti-bot Bédéthèque pour rien.
    searchGeneration++;

    // "ça met toujours Chargement des infos... ça devrait juste rien mettre. on sait pas
    // si ça charge encore" - la garde ci-dessus arrête bien les requêtes futures, mais un
    // slot pas encore atteint par la boucle gardait son placeholder "⏳ Chargement des
    // infos..." affiché indéfiniment (plus aucun code n'allait jamais le vider, puisque
    // la boucle sort avant de l'atteindre). On sait déjà qu'aucun de ces slots ne
    // chargera plus rien tout seul - remplacé par un bouton "Recharger" (voir
    // _reloadCandidateDetailButtonHtml) plutôt qu'un vide définitif, pour pouvoir
    // récupérer ces infos à la demande si l'utilisateur revient à l'étape 1.
    //
    // "ca stoppe le chargement mais il faudrait quand même charger la série que j'ai
    // cliqué" - le candidat CLIQUÉ, lui, va justement être rechargé juste en dessous (voir
    // le fetch de secours dans le try/catch) puisque c'est sa fiche qui nourrit l'étape 2 -
    // son propre slot ne doit donc pas basculer sur "Recharger" comme les autres, sans quoi
    // la carte encore visible derrière l'étape 2 semble abandonnée alors que ses infos sont
    // en train d'arriver.
    const clickedIndex = discoverCandidates.findIndex(c => c.url === url);
    document.querySelectorAll('#results-list .result-detail-loading').forEach(span => {
        const detailsSlot = span.closest('.result-details');
        if (detailsSlot && detailsSlot.id.startsWith('bedetheque-detail-')) {
            const index = detailsSlot.id.replace('bedetheque-detail-', '');
            if (clickedIndex !== -1 && index === String(clickedIndex)) return;
            detailsSlot.innerHTML = _reloadCandidateDetailButtonHtml(index);
        }
    });

    currentState.selectedSeries = {
        title: title,
        url: url,
        details: candidateDetailsCache[url] || null
    };

    // Marquer la carte choisie dans la liste des résultats (reste visible, toutes ses
    // infos - couverture, tags, auteurs - y sont déjà affichées, pas besoin de les répéter)
    // + état "chargement" le temps de la suite ("il ne se passe rien en frontend pendant
    // que tu charges les données" - le clic ne montrait rien avant que step-2/step-3
    // n'apparaisse, perceptible comme un clic resté sans effet le temps du fetch).
    document.querySelectorAll('#results-list .result-item').forEach(el => {
        el.classList.toggle('selected', el.dataset.url === url);
        el.classList.remove('result-item-loading');
    });
    if (cardEl) cardEl.classList.add('result-item-loading');

    // "ca charge en backend la série. met un bouton loading (en place de l'etape 2) pour
    // voir que quelque chose se passe" - le résultat de loadLibraries() (liste à choisir,
    // ou saut direct à l'étape 3 s'il n'y a qu'une seule bibliothèque) ne montrait RIEN
    // dans le créneau visuel de l'étape 2 tant qu'il n'était pas connu - avec une seule
    // bibliothèque (cas courant), l'étape 2 restait carrément masquée pendant tout
    // l'appel réseau /api/bedetheque/add-series (scraping Bédéthèque, délai anti-bot),
    // laissant uniquement le survol discret de la carte comme indice. Affichée tout de
    // suite avec juste l'indicateur de chargement, avant même de savoir combien de
    // bibliothèques existent - loadLibraries()/selectLibrary() gèrent la bascule vers la
    // vraie liste ou l'étape 3 une fois la réponse connue.
    document.getElementById('libraries-list').innerHTML = '';
    hideElement('library-error');
    showElement('step-2');
    showElement('series-verification-loading');
    document.getElementById('step-2').scrollIntoView({ behavior: 'smooth', block: 'start' });

    try {
        // Si les infos n'ont pas encore fini de charger à l'étape 1 (résultat pas encore
        // atteint par loadCandidateDetailsSequentially), on les récupère ici
        if (!currentState.selectedSeries.details) {
            try {
                const response = await fetch(`/api/bedetheque/info?url=${encodeURIComponent(url)}`);
                const data = await response.json();

                if (data.success && currentState.selectedSeries && currentState.selectedSeries.url === url) {
                    currentState.selectedSeries.details = data.info;
                    // Le slot de la carte cliquée est resté sur son "⏳ Chargement des
                    // infos..." (épargné ci-dessus) - le remplir maintenant qu'on a la
                    // réponse, même rendu que loadCandidateDetailsSequentially/
                    // reloadCandidateDetail, pour ne pas le laisser bloqué indéfiniment.
                    if (clickedIndex !== -1) {
                        candidateDetailsCache[url] = data.info;
                        const clickedSlot = document.getElementById(`bedetheque-detail-${clickedIndex}`);
                        if (clickedSlot) clickedSlot.innerHTML = renderCandidateDetailHtml(data.info);
                        const clickedBadges = document.getElementById(`bedetheque-badges-${clickedIndex}`);
                        if (clickedBadges) clickedBadges.innerHTML = renderCandidateBadgesHtml(data.info);
                    }
                } else if (clickedIndex !== -1) {
                    // Échec (data.success faux) - même repli "Recharger" que les autres
                    // candidats plutôt qu'un "Chargement..." bloqué pour de bon.
                    const clickedSlot = document.getElementById(`bedetheque-detail-${clickedIndex}`);
                    if (clickedSlot) clickedSlot.innerHTML = _reloadCandidateDetailButtonHtml(clickedIndex);
                }
            } catch (error) {
                console.error('Erreur récupération infos Bedetheque:', error);
                if (clickedIndex !== -1) {
                    const clickedSlot = document.getElementById(`bedetheque-detail-${clickedIndex}`);
                    if (clickedSlot) clickedSlot.innerHTML = _reloadCandidateDetailButtonHtml(clickedIndex);
                }
            }
        }

        await loadLibraries();
    } finally {
        if (cardEl) cardEl.classList.remove('result-item-loading');
    }
}

function resetToStep1() {
    currentState.selectedSeries = null;
    currentState.selectedLibrary = null;
    
    // Nettoyer les erreurs et loadings
    hideError('step-1');
    hideError('step-2');
    hideError('step-3');
    const verificationLoading = document.getElementById('series-verification-loading');
    if (verificationLoading) {
        verificationLoading.style.display = 'none';
    }
    
    showElement('step-1');
    hideElement('step-2');
    hideElement('step-3');

    document.querySelectorAll('#results-list .result-item').forEach(el => {
        el.classList.remove('selected');
    });

    document.getElementById('seriesNameInput').focus();
}

// ============================================
// ÉTAPE 2: Sélection de la bibliothèque
// ============================================

async function loadLibraries() {
    // step-2 est déjà affichée avec l'indicateur de chargement depuis selectSeries() -
    // ne plus la masquer ici (ça effaçait justement ce feedback pendant cet appel réseau).
    hideError('step-2');

    try {
        const response = await fetch('/api/libraries');
        const libraries = await response.json();

        currentState.libraries = libraries;

        // Une seule bibliothèque : pas besoin de la faire choisir, on l'utilise directement
        // - l'indicateur de chargement reste affiché, selectLibrary() enchaîne son propre
        // appel réseau (add-series) sans creux visuel entre les deux.
        if (libraries.length === 1) {
            selectLibrary(libraries[0].id, libraries[0], null);
            return;
        }

        hideElement('series-verification-loading');
        displayLibraries(libraries);
        showElement('step-2');
        document.getElementById('step-2').scrollIntoView({ behavior: 'smooth', block: 'start' });

    } catch (error) {
        console.error('Erreur:', error);
        hideElement('series-verification-loading');
        showElement('step-2');
        showError('step-2', 'Erreur lors du chargement des bibliothèques');
    }
}

function displayLibraries(libraries) {
    const list = document.getElementById('libraries-list');
    list.innerHTML = '';

    if (libraries.length === 0) {
        list.innerHTML = '<p style="text-align: center; color: #9ca3af;">Aucune bibliothèque trouvée</p>';
        return;
    }

    libraries.forEach(lib => {
        const div = document.createElement('div');
        div.className = 'library-item';
        div.onclick = () => selectLibrary(lib.id, lib, div);
        
        div.innerHTML = `
            <p class="library-name">${escapeHtml(lib.name)}</p>
            <p class="library-path">📂 ${escapeHtml(lib.path)}</p>
            <div class="library-stats">
                <span>📖 ${lib.series_count} ${pluralize(lib.series_count, 'série')}</span>
                <span>📕 ${lib.volumes_count} ${pluralize(lib.volumes_count, 'volume')}</span>
            </div>
        `;
        list.appendChild(div);
    });
}

// Dès la bibliothèque choisie (ou auto-choisie s'il n'y en a qu'une), la série est créée
// en base tout de suite avec toutes les métadonnées Bédéthèque déjà récupérées à l'étape
// 1 (résumé, couverture, genre, auteurs, tomes attendus...) - via le même endpoint que le
// flux "Sonarr" (/api/bedetheque/add-series: crée aussi l'entrée de surveillance et
// tente un matching EBDZ automatique). Toujours SANS dossier physique, créé plus tard au
// premier téléchargement/import réel.
async function selectLibrary(libraryId, libraryData, element) {
    currentState.selectedLibrary = {
        id: libraryId,
        ...libraryData
    };

    // Mettre à jour la sélection visuelle
    document.querySelectorAll('.library-item').forEach(el => {
        el.classList.remove('selected');
    });

    if (element) {
        element.classList.add('selected');
    }

    hideError('step-2');
    const verificationLoading = document.getElementById('series-verification-loading');
    if (verificationLoading) verificationLoading.style.display = 'block';

    try {
        const response = await fetch('/api/bedetheque/add-series', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // from_discover: cette étape a sa PROPRE recherche dédiée juste après
            // (étape 3 "Chercher les sources" -> POST /auto-acquire/run) - sans ce
            // flag, add-series lancerait maintenant AUSSI sa propre recherche selon le
            // réglage global "Téléchargement automatique à l'ajout", en double.
            body: JSON.stringify({ url: currentState.selectedSeries.url, library_id: libraryId, from_discover: true })
        });
        const data = await response.json();

        if (!data.success) {
            showElement('step-2');
            showError('step-2', 'Erreur lors de la création de la série : ' + (data.error || 'Erreur inconnue'));
            return;
        }

        currentState.selectedSeries.seriesId = data.series_id;

        hideElement('step-2');
        showElement('step-3');
        displaySourceSelection();
        document.getElementById('step-3').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
        console.error('Erreur:', error);
        showElement('step-2');
        showError('step-2', 'Erreur lors de la création de la série : ' + error.message);
    } finally {
        if (verificationLoading) verificationLoading.style.display = 'none';
    }
}

function displaySourceSelection() {
    const summary = document.getElementById('selection-summary');

    // Repartir d'un état propre à chaque arrivée sur cette étape (nouvelle série, ou
    // retour depuis "Changer de bibliothèque") - sans ça, les résultats de la RECHERCHE
    // PRÉCÉDENTE (pour une série différente) restaient affichés tant que l'utilisateur
    // n'avait pas re-cliqué "Rechercher" lui-même ("il y a toujours les anciens
    // recherche de la série d'avant si je fais une nouvelle recherche"). Le nettoyage
    // au clic sur "Rechercher" (voir searchSources) ne couvrait que ce cas précis, pas
    // celui-ci où on arrive sur l'étape avec d'anciens résultats déjà visibles.
    hideElement('sources-results');
    hideElement('no-sources-panel');
    document.getElementById('sources-results-list').innerHTML = '';

    if (currentState.selectedSeries && currentState.selectedLibrary) {
        // La série est déjà créée en base à ce stade (voir selectLibrary), son seriesId
        // est donc toujours connu ici - lien direct vers sa fiche plutôt que de forcer un
        // aller-retour par la bibliothèque pour la retrouver
        const seriesId = currentState.selectedSeries.seriesId;
        summary.innerHTML = `
            <div class="summary-item">
                <div class="summary-label">📖 Série</div>
                <div class="summary-value">
                    ${escapeHtml(currentState.selectedSeries.title)}
                    ${seriesId ? `<a href="/series/${seriesId}" target="_blank" class="summary-series-link" title="Ouvrir la fiche de cette série">${svgIcon('link')} Voir la fiche</a>` : ''}
                </div>
            </div>
            <div class="summary-item">
                <div class="summary-label">📚 Bibliothèque</div>
                <div class="summary-value">${escapeHtml(currentState.selectedLibrary.name)}</div>
            </div>
        `;

        // Pré-rempli avec le titre suivi en base, modifiable avant de lancer la
        // recherche (voir searchSources) - le titre suivi ne correspond pas toujours
        // à la façon dont une release est nommée chez les indexeurs.
        const searchQueryInput = document.getElementById('sources-search-query');
        if (searchQueryInput) {
            searchQueryInput.value = currentState.selectedSeries.title;
        }
    }
}

function resetToStep2() {
    currentState.selectedLibrary = null;

    // La série déjà créée en base (s'il y en a une) appartenait à l'ancienne
    // bibliothèque - on oublie son id pour qu'une nouvelle soit créée dans celle qui
    // sera choisie ensuite (voir selectLibrary)
    if (currentState.selectedSeries) {
        currentState.selectedSeries.seriesId = null;
    }

    // Cacher les messages d'erreur et chargement de l'étape 2
    const verificationLoading = document.getElementById('series-verification-loading');
    if (verificationLoading) {
        verificationLoading.style.display = 'none';
    }
    hideError('step-2');
    hideElement('no-sources-panel');
    hideElement('sources-results');

    hideElement('step-3');

    // La liste peut ne pas avoir été affichée si une seule bibliothèque avait été
    // auto-sélectionnée sans passer par l'étape 2 - on (re)génère donc son contenu
    displayLibraries(currentState.libraries);
    showElement('step-2');
    document.getElementById('step-2').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// "met une option pour recherche automatique dans étape 2 et donc ne pas faire de
// recherche manuelle" - alternative à searchSources() ci-dessous: pas de requête/sources
// à choisir, pas de résultats à revoir un par un - cherche et télécharge directement
// chaque tome manquant trouvé avec confiance. runSeriesAutoAcquire (search-results-table.js)
// est le point d'entrée UNIQUE, partagé avec la fiche série (library.js) - "recherche et
// recherche automatique de discover et de la page album doit être la même... pas de code
// en double pour rien". Seule la garde "pas de série sélectionnée" (propre à cette étape
// de Découvrir, currentState n'existe pas ailleurs) reste ici.
async function runAutoAcquireNow() {
    if (!currentState.selectedSeries || !currentState.selectedSeries.seriesId) {
        showError('step-3', 'Pas de série sélectionnée');
        return;
    }
    await runSeriesAutoAcquire(currentState.selectedSeries.seriesId, {
        seriesTitle: currentState.selectedSeries.title,
        buttonEl: document.getElementById('auto-acquire-now-btn')
    });
}

// ============================================
// ÉTAPE 3: Recherche dans les sources
// ============================================

async function searchSources() {
    if (!currentState.selectedSeries) {
        showError('step-3', 'Pas de série sélectionnée');
        return;
    }

    const searchEbdz = document.getElementById('search-ebdz').checked && enabledIntegrations.ebdz;
    // "j'ai désactivé prowlarr mais il s'affiche toujours dans découvrir et rechercher" -
    // en plus de masquer la case à cocher (voir le listener DOMContentLoaded plus bas),
    // vérifié ici aussi: un état "cochée" resté en mémoire (ex: décoché puis Prowlarr
    // désactivé entre-temps dans un autre onglet) ne doit jamais suffire à relancer une
    // requête vers une source délibérément désactivée.
    const searchProwlarr = document.getElementById('search-prowlarr').checked && enabledIntegrations.prowlarr;
    const searchTelegram = document.getElementById('search-telegram').checked && enabledIntegrations.telegram;
    const searchWebArchive = document.getElementById('search-web-archive').checked;
    const searchFourtoutici = searchWebArchive && enabledIntegrations.fourtoutici;
    const searchAnnasArchive = searchWebArchive && enabledIntegrations.annas_archive;

    if (!searchEbdz && !searchProwlarr && !searchTelegram && !searchFourtoutici && !searchAnnasArchive) {
        showError('step-3', 'Sélectionnez au moins une source');
        return;
    }

    if (typeof logSearchHistoryEvent === 'function') {
        logSearchHistoryEvent(currentState.selectedSeries.title, 'Recherche de sources (Découvrir)', currentState.selectedSeries.seriesId);
    }

    showLoading('step-3', true);
    hideError('step-3');
    hideElement('sources-results');
    hideElement('no-sources-panel');

    // Repartir d'un état propre à chaque recherche: sans ça, les résultats d'une
    // source affichés lors d'une recherche précédente (ex: Prowlarr) restent visibles
    // même si cette source est décochée ou si la nouvelle recherche ne les retrouve pas
    document.getElementById('sources-results-list').innerHTML = '';

    try {
        // "dans chercher les sources il faudrait aussi que je puisse modifier la
        // recherche exacte" - valeur modifiable dans le champ (voir displaySourceSelection),
        // retombe sur le titre de la série si l'utilisateur l'a vidé.
        const searchQueryInput = document.getElementById('sources-search-query');
        const seriesTitle = (searchQueryInput && searchQueryInput.value.trim()) || currentState.selectedSeries.title;
        // "ca devrait passer... j'ai fait la recherche d'une série que j'ajoute dans
        // decouvrir. donc tu sais la serie et donc dans l'import ca devrait l'assigner
        // correctement" - la série est déjà créée en base à ce stade (voir selectLibrary).
        const seriesId = currentState.selectedSeries?.seriesId ?? null;

        // "do the same search between la serie et decouvrir. same function same code...
        // check that the search is the same for every function that do a search in the
        // app" - searchMissingVolumeSource (search-results-table.js) est le SEUL endroit
        // qui interroge EBDZ/Prowlarr/Telegram pour une série connue, aussi utilisé par
        // la fiche série (searchMissingVolume, library.js). volume_num=null: recherche
        // "série entière", pas un tome précis.
        //
        // "tu peux charger les recherches instantanément et ne pas attendre que tous les
        // résultats s'affichent?" - même pattern que searchMissingVolume (library.js):
        // chaque source affiche ses résultats dès qu'elle répond, au lieu d'un
        // Promise.all qui bloquait tout l'affichage jusqu'à la plus lente des trois.
        let combined = [];
        let ebdzDone = !searchEbdz;
        let prowlarrDone = !searchProwlarr;
        let telegramDone = !searchTelegram;
        let fourtouticiDone = !searchFourtoutici;
        let annasArchiveDone = !searchAnnasArchive;
        // "quand prowlarr est mis à jour dans la recherche ça reset tous mes changements"
        // - Prowlarr (le plus lent) rappelle render() après EBDZ/Telegram déjà affichés;
        // sans ce drapeau, buildSearchResultsTableHtml recommençait filtres/tri/checkbox
        // à zéro à chaque rappel au lieu de seulement les compléter avec les nouveaux
        // résultats.
        let renderedOnce = false;
        const allDone = () => ebdzDone && prowlarrDone && telegramDone && fourtouticiDone && annasArchiveDone;
        const render = () => {
            if (combined.length > 0) {
                document.getElementById('sources-results-list').innerHTML = buildSearchResultsTableHtml(combined, null, seriesId, null, renderedOnce);
                // "je veux celui la partout" (loupe carrée sur les filtres texte, voir
                // bedetheque-indispensables.js) - tableau injecté après coup, hors de
                // portée du scan une-fois-au-chargement de nav.js.
                initClearableSearchInputs(document.getElementById('sources-results-list'));
                renderedOnce = true;
                showElement('sources-results');
                hideElement('no-sources-panel');
                hideError('step-3');
                checkEmuleStatus();
            } else if (allDone()) {
                showError('step-3', 'Aucun résultat trouvé dans les sources');
                showElement('no-sources-panel');
            }
            // sinon: rien trouvé pour l'instant mais des sources n'ont pas encore répondu
            // - pas de "aucun résultat" prématuré tant qu'elles n'ont pas fini.
        };

        const ebdzPromise = (searchEbdz ? searchMissingVolumeSource('ebdz', seriesTitle, null, seriesId, false, false, false) : Promise.resolve([]))
            .then(results => { combined = [...combined, ...results]; ebdzDone = true; render(); });
        const prowlarrPromise = (searchProwlarr ? searchMissingVolumeSource('prowlarr', seriesTitle, null, seriesId, false, false, false) : Promise.resolve([]))
            .then(results => { combined = [...combined, ...results]; prowlarrDone = true; render(); });
        const telegramPromise = (searchTelegram ? searchMissingVolumeSource('telegram', seriesTitle, null, seriesId, false, false, false) : Promise.resolve([]))
            .then(results => { combined = [...combined, ...results]; telegramDone = true; render(); });
        const fourtouticiPromise = (searchFourtoutici ? searchMissingVolumeSource('fourtoutici', seriesTitle, null, seriesId, false, false, false) : Promise.resolve([]))
            .then(results => { combined = [...combined, ...results]; fourtouticiDone = true; render(); });
        const annasArchivePromise = (searchAnnasArchive ? searchMissingVolumeSource('annas_archive', seriesTitle, null, seriesId, false, false, false) : Promise.resolve([]))
            .then(results => { combined = [...combined, ...results]; annasArchiveDone = true; render(); });

        await Promise.all([ebdzPromise, prowlarrPromise, telegramPromise, fourtouticiPromise, annasArchivePromise]);

    } catch (error) {
        console.error('Erreur:', error);
        showError('step-3', 'Erreur lors de la recherche: ' + error.message);
    } finally {
        showLoading('step-3', false);
    }
}

// ============================================
// Utilitaires d'interface
// ============================================

function showLoading(stepNum, show) {
    let loadingId;
    if (stepNum === 'step-1' || stepNum === 1) {
        loadingId = 'bedetheque-search-loading';
    } else if (stepNum === 'step-2' || stepNum === 2) {
        loadingId = 'library-loading';
    } else if (stepNum === 'step-3' || stepNum === 3) {
        loadingId = 'sources-loading';
    }
    
    if (loadingId) {
        const el = document.getElementById(loadingId);
        if (el) {
            el.style.display = show ? 'block' : 'none';
        }
    }
}

function showError(step, message) {
    let errorId;
    if (step === 'step-1' || step === 1) {
        errorId = 'bedetheque-search-error';
    } else if (step === 'step-2' || step === 2) {
        errorId = 'library-error';
    } else if (step === 'step-3' || step === 3) {
        errorId = 'sources-error';
    }
    
    if (errorId) {
        const el = document.getElementById(errorId);
        if (el) {
            el.textContent = '❌ ' + message;
            el.style.display = 'block';
        }
    }
}

function hideError(step) {
    let errorId;
    if (step === 'step-1' || step === 1) {
        errorId = 'bedetheque-search-error';
    } else if (step === 'step-2' || step === 2) {
        errorId = 'library-error';
    } else if (step === 'step-3' || step === 3) {
        errorId = 'sources-error';
    }
    
    if (errorId) {
        const el = document.getElementById(errorId);
        if (el) {
            el.style.display = 'none';
        }
    }
}

function showElement(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'block';
}

function hideElement(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
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
    return text.replace(/[&<>"']/g, m => map[m]);
}

function escapeAttr(text) {
    if (!text) return '';
    return text.replace(/'/g, "\\'").replace(/"/g, '\\"');
}

// Si la page est ouverte avec ?q=..., préremplir et lancer la recherche automatiquement
// (utilisé par la recherche rapide de l'en-tête, nav.js, quand la série tapée n'existe
// pas encore dans la bibliothèque - voir handleHeaderSearchInput) - même pattern que
// runSearchFromQueryParam dans search.js
(function runDiscoverFromQueryParam() {
    const urlParams = new URLSearchParams(window.location.search);
    const query = urlParams.get('q');
    if (query) {
        document.getElementById('seriesNameInput').value = query;
        searchBedethequeSeries();
    }
})();

// "j'ai désactivé prowlarr mais il s'affiche toujours dans découvrir et rechercher" - la
// case à cocher "Prowlarr" de l'étape 3 (recherche dans les sources) n'a aucune raison
// d'être proposée si Prowlarr n'est pas configuré (voir aussi searchSources ci-dessus,
// qui vérifie enabledIntegrations.prowlarr en plus de l'état de la case, en défense
// supplémentaire). Sur DOMContentLoaded (pas un appel immédiat): refreshEnabledIntegrations
// vit dans nav.js, chargé APRÈS discover.js dans cette page.
document.addEventListener('DOMContentLoaded', async () => {
    await refreshEnabledIntegrations();
    if (!enabledIntegrations.prowlarr) {
        const toggle = document.getElementById('search-prowlarr');
        if (toggle) {
            toggle.checked = false;
            const label = toggle.closest('.source-toggle');
            if (label) label.style.display = 'none';
        }
    }
    for (const [id, integration] of [['search-ebdz', 'ebdz'], ['search-telegram', 'telegram']]) {
        if (enabledIntegrations[integration]) continue;
        const toggle = document.getElementById(id);
        if (toggle) {
            toggle.checked = false;
            const label = toggle.closest('.source-toggle');
            if (label) label.style.display = 'none';
        }
    }
    // Recherche archive web: une seule option regroupe Fourtoutici et Anna's Archive.
    if (!enabledIntegrations.fourtoutici && !enabledIntegrations.annas_archive) {
        const toggle = document.getElementById('search-web-archive');
        if (toggle) { toggle.checked = false; const label = toggle.closest('.source-toggle'); if (label) label.style.display = 'none'; }
    }
});

