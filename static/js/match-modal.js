// ===== COQUILLE COMMUNE AUX MODALES "MATCHER MANUELLEMENT SUR <SOURCE EXTERNE>" =====
// Bédéthèque (fiche série + import), EBDZ et Komga ont chacune une modale "rechercher un
// titre / coller une URL directe / choisir un résultat" avec la même structure visuelle.
// Elles avaient dérivé indépendamment (une modale sans recherche automatique à
// l'ouverture, une sans fallback URL, deux regex de validation d'URL différentes pour la
// même source...) car le markup + câblage étaient dupliqués à chaque endroit plutôt que
// factorisés. Ce module ne factorise QUE ça (markup + wiring Entrée + rendu des cartes/
// messages d'erreur) - chaque source garde sa propre logique de recherche/confirmation
// (endpoints et formats de candidats différents d'une source à l'autre).

// Un seul champ titre/URL (pas de second champ URL séparé) - "je n'ai besoin que d'une
// seule entrée (nom et URL)" pour les 3 sources (Bédéthèque, EBDZ, Komga qui avaient
// encore un urlFallback séparé jusqu'ici) : chaque appelant détecte lui-même si la saisie
// ressemble à une URL (voir searchEbdzMatchCandidates/searchKomgaMatchCandidates dans
// library.js) plutôt que de dupliquer un second champ+bouton ici.
function buildMatchModalBodyHtml({ logoSrc, title, helpText, queryId, queryPlaceholder,
                                    prefillValue, resultsId, searchOnclick,
                                    autoSearch = false, backButtonHtml = '' } = {}) {
    return `
        ${backButtonHtml}
        <h2 class="modal-title modal-title-with-logo" style="margin-bottom: 15px;">${logoSrc ? `<img src="${logoSrc}" alt="" class="modal-title-logo"> ` : ''}${title}</h2>
        ${helpText ? `<p style="color:#666; margin-top:-10px; margin-bottom:15px;">${helpText}</p>` : ''}
        <div style="display: flex; gap: 8px; margin-bottom: 15px;">
            <input type="text" id="${queryId}" class="search-box" style="flex: 1; margin: 0;"
                   value="${escapeHtml(prefillValue || '')}" placeholder="${escapeHtml(queryPlaceholder || '')}">
            <button class="btn" onclick="${searchOnclick}">Rechercher</button>
        </div>
        <div id="${resultsId}">${autoSearch ? '<div class="loading"><div class="spinner"></div></div>' : ''}</div>
    `;
}

// searchFn est une vraie fonction (pas une chaîne onclick, contrairement au bouton
// ci-dessus qui doit rester un attribut inline pour porter ses arguments spécifiques à
// l'appelant, ex: seriesId)
function wireMatchModalEnterKeys(queryId, searchFn) {
    document.getElementById(queryId).addEventListener('keyup', (e) => {
        if (e.key === 'Enter') searchFn();
    });
}

function matchCandidateCardHtml(onclickJs, titleText, infoHtml) {
    return `
        <div class="series-card" style="cursor: pointer; margin-bottom: 8px;" onclick="${onclickJs}">
            <div class="series-title">${escapeHtml(titleText)}</div>
            <div class="series-info">${infoHtml}</div>
        </div>`;
}

function bedethequeMatchCandidateCardHtml(onclickJs, titleText, infoHtml, index, idPrefix) {
    return `
        <div class="series-card match-candidate-card" style="cursor: pointer; margin-bottom: 8px;" onclick="${onclickJs}">
            <div class="match-candidate-cover-slot" id="${idPrefix}-cover-${index}"></div>
            <div class="match-candidate-card-body">
                <div class="series-title">${escapeHtml(titleText)}</div>
                <div class="series-info">${infoHtml} <span id="${idPrefix}-badges-${index}"></span></div>
                <div class="match-candidate-desc-slot" id="${idPrefix}-desc-${index}"></div>
            </div>
        </div>`;
}

// Mêmes infos que renderCandidateBadgesHtml (discover.js) - tomes/genre/statut/année.
// "remove the grey background in the details information. I would prefer a smaller and
// more light interface" - texte simple séparé par des points médians plutôt que des
// pastilles .badge (fond gris, style.css), même traitement que Découvrir.
function _matchCandidateBadgesHtml(info) {
    const parts = [];
    if (info.total_volumes) parts.push(`📚 ${info.total_volumes} tome${info.total_volumes > 1 ? 's' : ''}`);
    if (info.genre) parts.push(`🎭 ${escapeHtml(info.genre)}`);
    if (info.status) parts.push(escapeHtml(info.status));
    if (info.year_start) parts.push(`📅 ${info.year_start}${info.year_end ? '-' + info.year_end : ''}`);
    return parts.length ? `<span class="candidate-badges-text">${parts.join(' · ')}</span>` : '';
}

// À appeler juste après avoir injecté les cartes de bedethequeMatchCandidateCardHtml dans
// le DOM. candidates: tableau d'objets portant au moins `url`. Séquentiel plutôt qu'en
// parallèle (une recherche Bédéthèque déjà large + N requêtes /info simultanées
// risquerait le même bannissement anti-bot que documenté côté discover.js). Chaque étape
// vérifie que son slot existe encore (modale fermée/candidats reconstruits entre-temps),
// sans generation à suivre ici contrairement à Découvrir - ces modales n'ont qu'une seule
// recherche possible à la fois, jamais de "charger plus" concurrent.
async function loadBedethequeMatchCandidateCovers(candidates, idPrefix) {
    for (let index = 0; index < candidates.length; index++) {
        const url = candidates[index] && candidates[index].url;
        if (!url) continue;
        if (!document.getElementById(`${idPrefix}-cover-${index}`) && !document.getElementById(`${idPrefix}-desc-${index}`)) continue;

        try {
            const response = await fetch(`/api/bedetheque/info?url=${encodeURIComponent(url)}`);
            const data = await response.json();
            const coverSlot = document.getElementById(`${idPrefix}-cover-${index}`);
            const descSlot = document.getElementById(`${idPrefix}-desc-${index}`);
            const badgesSlot = document.getElementById(`${idPrefix}-badges-${index}`);
            if (data.success && data.info) {
                if (coverSlot && data.info.cover_path) {
                    coverSlot.innerHTML = `<img class="match-candidate-cover" src="/${escapeHtml(data.info.cover_path)}" alt="">`;
                }
                if (badgesSlot) badgesSlot.innerHTML = _matchCandidateBadgesHtml(data.info);
                if (descSlot) {
                    const authors = [];
                    if (data.info.scenaristes && data.info.scenaristes.length) authors.push(`<strong>Scénario:</strong> ${escapeHtml(data.info.scenaristes.join(', '))}`);
                    if (data.info.dessinateurs && data.info.dessinateurs.length) authors.push(`<strong>Dessin:</strong> ${escapeHtml(data.info.dessinateurs.join(', '))}`);
                    if (data.info.editeurs && data.info.editeurs.length) authors.push(`<strong>Éditeur:</strong> ${escapeHtml(data.info.editeurs.join(', '))}`);
                    descSlot.innerHTML = `
                        ${data.info.description ? `<p class="match-candidate-description">${escapeHtml(data.info.description)}</p>` : ''}
                        ${authors.length ? `<p class="match-candidate-authors">${authors.join(' · ')}</p>` : ''}
                    `;
                }
            }
        } catch (error) {
            // Silencieux: une carte reste parfaitement utilisable pour matcher sans
            // couverture/description, ce n'est qu'un confort visuel supplémentaire.
        }
    }
}

function matchModalNoResultsHtml(message) {
    return `<div class="no-data"><p>😕 ${escapeHtml(message)}</p></div>`;
}

function matchModalErrorHtml(message) {
    return `<div class="no-data"><p>❌ ${escapeHtml(message)}</p></div>`;
}
