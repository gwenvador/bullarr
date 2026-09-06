function reviewEscape(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}

function reviewAttr(value) { return reviewEscape(value).replace(/'/g, "\\'").replace(/`/g, '&#96;'); }
function reviewSourceUrl(candidate) { return candidate.thread_url || candidate.info_url || candidate.source_link || candidate.link || ''; }
function reviewDecodeFilename(filename) {
    if (!filename) return filename;
    let decoded = filename;
    try { decoded = decodeURIComponent(decoded); }
    catch (e) { /* %-encoding invalide - on garde la chaîne telle quelle */ }
    // Entités HTML restantes sur un nom scrapé avant la correction du scraper EBDZ
    // (extract_ed2k_links, blueprints/ebdz/scraper.py) - même correction que
    // decodeFilename côté search.js/library.js/history-shared.js.
    return decoded.replace(/&amp;|&lt;|&gt;|&quot;|&#0?39;|&apos;/g, m => ({
        '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#039;': "'", '&apos;': "'"
    }[m]));
}
function reviewDateHtml(createdAt) {
    if (!createdAt) return '';
    const date = parseDbUtcDate(createdAt);
    if (!date) return '';
    const short = date.toLocaleDateString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ });
    const full = date.toLocaleString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ });
    return `<span class="review-date" data-tooltip="${reviewAttr(full)}">${svgIcon('calendar')} ${reviewEscape(short)}</span>`;
}
function reviewSourceIcon(candidate) {
    const source = candidate.source || '';
    const icons = { ebdz:'/static/img/ebdz-logo.png', prowlarr:'/static/img/prowlarr-logo.svg', telegram:'/static/img/telegram-logo.svg', fourtoutici:'/static/img/web-logo.svg', annas_archive:'/static/img/web-logo.svg' };
    return icons[source] ? `<img class="review-source-icon" src="${icons[source]}" alt="" title="${reviewEscape(source)}">` : '';
}

function _reviewCandidateAvailabilityHtml(candidate, reviewId, index) {
    if (candidate.source === 'prowlarr') {
        const seeders = candidate.seeders ?? 'N/A';
        const peers = candidate.peers ?? 'N/A';
        return `<span class="review-candidate-availability" data-tooltip="Seeds / Peers">${reviewEscape(seeders)} / ${reviewEscape(peers)}</span>`;
    }
    if (candidate.source === 'ebdz') {
        const value = candidate._ed2kSources !== undefined
            ? `${candidate._ed2kSources} source${candidate._ed2kSources > 1 ? 's' : ''}` : '…';
        return `<span class="review-candidate-availability" id="review-ed2k-${reviewId}-${index}" data-tooltip="Sources ed2k disponibles">${reviewEscape(value)}</span>`;
    }
    return '';
}

// Requête groupée (un seul POST pour tous les candidats EBDZ de toutes les cartes),
// déclenchée après le rendu initial de loadAutoAcquireReviews - ne touche que les cellules
// concernées via leur id plutôt que de tout re-rendre.
async function _loadReviewEd2kAvailability(reviews) {
    const targets = [];
    for (const review of reviews) {
        (review.candidates || []).slice(0, 20).forEach((candidate, index) => {
            if (candidate.source === 'ebdz' && candidate.link) targets.push({ reviewId: review.id, index, link: candidate.link });
        });
    }
    if (!targets.length) return;
    try {
        const response = await fetch('/api/emule/ed2k-availability', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ links: [...new Set(targets.map(t => t.link))] })
        });
        const data = await response.json();
        if (!data.success) return;
        const availability = data.availability || {};
        targets.forEach(({ reviewId, index, link }) => {
            if (availability[link] === undefined) return;
            const el = document.getElementById(`review-ed2k-${reviewId}-${index}`);
            if (el) el.textContent = `${availability[link]} source${availability[link] > 1 ? 's' : ''}`;
        });
    } catch (error) {
        console.warn('Erreur récupération disponibilité ed2k:', error);
    }
}

// match-modal.js (buildMatchModalBodyHtml) attend un escapeHtml global défini par la page
// appelante, comme sur toutes les autres pages utilisant ce module - manquant ici jusqu'ici,
// ce qui faisait échouer silencieusement (ReferenceError) l'ouverture de la modale de
// matching Bédéthèque : la modale s'affichait vide (classe .active déjà posée) sans que le
// formulaire de recherche n'apparaisse jamais. Même corps que les autres copies de l'app.
function escapeHtml(text) {
    if (!text) return '';
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return text.replace(/[&<>"']/g, m => map[m]);
}

function closeReviewBedethequeModal() {
    document.getElementById('review-bedetheque-modal')?.classList.remove('active');
}

// Mémorise la ligne de validation concernée pendant que la modale est ouverte -
// selectReviewBedethequeCandidate (déclenché par le clic sur un résultat Bédéthèque) en a
// besoin pour savoir QUELLE ligne rattacher, sans avoir à le repasser en argument à travers
// matchCandidateCardHtml (searchReviewBedethequeCandidates, générique, partagé avec les
// autres modales de matching).
let _reviewModalReviewId = null;

let expandedReviewCandidates = new Set();

function toggleReviewCandidates(reviewId) {
    if (expandedReviewCandidates.has(reviewId)) expandedReviewCandidates.delete(reviewId);
    else expandedReviewCandidates.add(reviewId);
    const isExpanded = expandedReviewCandidates.has(reviewId);
    const list = document.getElementById(`review-candidates-${reviewId}`);
    if (list) list.style.display = isExpanded ? 'grid' : 'none';
    document.getElementById(`review-candidates-toggle-${reviewId}`)?.classList.toggle('review-candidates-toggle-open', isExpanded);
}

async function openReviewBedethequeModal(reviewId, title) {
    _reviewModalReviewId = reviewId;
    const modal = document.getElementById('review-bedetheque-modal');
    const body = document.getElementById('review-bedetheque-modal-body');
    modal.classList.add('active');
    body.innerHTML = buildMatchModalBodyHtml({
        logoSrc: '/static/img/bedetheque-logo.png', title: 'Matcher sur Bédéthèque',
        helpText: 'Sélectionnez la série Bédéthèque correspondant au fichier.',
        queryId: 'review-bedetheque-query', queryPlaceholder: 'Nom de la série…',
        prefillValue: title, resultsId: 'review-bedetheque-results',
        searchOnclick: 'searchReviewBedethequeCandidates()', autoSearch: true,
    });
    wireMatchModalEnterKeys('review-bedetheque-query', searchReviewBedethequeCandidates);
    await searchReviewBedethequeCandidates();
}

async function searchReviewBedethequeCandidates() {
    const query = document.getElementById('review-bedetheque-query')?.value.trim();
    const root = document.getElementById('review-bedetheque-results');
    if (!query || !root) return;
    root.innerHTML = '<div class="loading">Recherche…</div>';
    try {
        const data = await (await fetch(`/api/bedetheque/search?q=${encodeURIComponent(query)}`)).json();
        if (data.results?.length) {
            root.innerHTML = data.results.map((r, index) => bedethequeMatchCandidateCardHtml(`selectReviewBedethequeCandidate('${reviewAttr(r.url)}','${reviewAttr(r.title)}')`, r.title, r.genre || '', index, 'review-bedetheque-match')).join('');
            loadBedethequeMatchCandidateCovers(data.results, 'review-bedetheque-match');
        } else {
            root.innerHTML = matchModalNoResultsHtml('Aucune série trouvée sur Bédéthèque');
        }
    } catch (e) { root.innerHTML = matchModalErrorHtml(e.message); }
}

async function selectReviewBedethequeCandidate(url, title) {
    const reviewId = _reviewModalReviewId;
    closeReviewBedethequeModal();
    if (!reviewId) return;
    showToast('review-bedetheque-match', `Rattachement à « ${title} »…`, {icon:'loader-circle'});
    try {
        const response = await fetch(`/api/auto-acquire/reviews/${reviewId}/match-series`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url})
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Rattachement impossible');
        showToast('review-bedetheque-match', `Série rattachée à « ${title} ».`, {icon:'check', autoHideMs:5000});
        await loadAutoAcquireReviews();
    } catch (error) {
        showToast('review-bedetheque-match', `Échec du rattachement : ${error.message}`, {icon:'circle-x', autoHideMs:6000});
    }
}

// Un tome (une ligne de validation) au sein de sa carte de série - voir
// loadAutoAcquireReviews pour le regroupement. Dismiss/téléchargement restent par tome
// (chaque candidat garde son propre review.id), seuls le titre/auteur de série remontent
// au niveau du groupe pour ne plus se répéter à chaque tome.
function _reviewTomeHtml(review) {
    const volume = review.volume_label || (review.volume_number == null ? 'Album' : `Tome ${review.volume_number}`);
    const seriesMatchAction = !review.series_id
        ? `<button class="review-icon-btn review-download-btn" type="button" onclick="openReviewBedethequeModal(${review.id}, '${reviewAttr(review.series_title || '')}')" title="Matcher cette série sur Bédéthèque"><img src="/static/img/bedetheque-logo.png" alt="" style="width:18px;height:18px;object-fit:contain;"></button>`
        : '';
    const candidateList = (review.candidates || []).slice(0, 20);
    const candidatesHtml = candidateList.map((candidate, index) => {
        const name = reviewDecodeFilename(candidate.filename || candidate.title || 'Résultat sans nom');
        const url = reviewSourceUrl(candidate);
        const action = review.series_id
            ? `<button class="review-icon-btn review-download-btn" type="button" title="Ajouter ce fichier au téléchargement" onclick="downloadReviewCandidate(${review.id}, ${index}, this)">${svgIcon('plus')}</button>`
            : seriesMatchAction;
        return `<div class="review-candidate"><span class="review-source">${reviewSourceIcon(candidate)}</span><span class="review-candidate-name">${reviewEscape(name)}</span>${_reviewCandidateAvailabilityHtml(candidate, review.id, index)}${url ? `<a class="review-icon-btn" href="${reviewAttr(url)}" target="_blank" rel="noopener" title="Ouvrir la source">${svgIcon('link')}</a>` : ''}${action}</div>`;
    }).join('');
    const isExpanded = expandedReviewCandidates.has(review.id);
    const count = candidateList.length;
    return `<div class="review-tome" data-review-id="${review.id}">
        <button class="review-dismiss-btn" type="button" title="Supprimer cette validation" onclick="resolveAutoAcquireReview(${review.id})">${svgIcon('x')}</button>
        <div class="review-tome-header">
            <span class="review-volume">${reviewEscape(volume)} ${reviewDateHtml(review.created_at)}</span>
            <button class="review-candidates-toggle${isExpanded ? ' review-candidates-toggle-open' : ''}" type="button" id="review-candidates-toggle-${review.id}" onclick="toggleReviewCandidates(${review.id})">${count} candidat${count > 1 ? 's' : ''} ${svgIcon('chevron-down')}</button>
        </div>
        <div class="review-candidates" id="review-candidates-${review.id}" style="display:${isExpanded ? 'grid' : 'none'}">${candidatesHtml || '<div class="review-meta">Aucun candidat détaillé.</div>'}</div>
    </div>`;
}

async function loadAutoAcquireReviews() {
    const root = document.getElementById('review-list');
    try {
        const response = await fetch('/api/auto-acquire/reviews');
        const reviews = await response.json();
        if (!reviews.length) {
            root.innerHTML = `<div class="review-empty">${svgIcon('check')} Aucune action en attente de validation.</div>`;
            return;
        }
        const groups = [];
        const groupByKey = new Map();
        for (const review of reviews) {
            const key = review.series_id != null ? `id:${review.series_id}` : `title:${review.series_title}`;
            let group = groupByKey.get(key);
            if (!group) {
                group = { seriesId: review.series_id, seriesTitle: review.series_title,
                    scenaristes: review.bedetheque_scenaristes, dessinateurs: review.bedetheque_dessinateurs, reviews: [] };
                groupByKey.set(key, group);
                groups.push(group);
            }
            group.reviews.push(review);
        }
        root.innerHTML = groups.map(group => {
            const authorNames = [group.scenaristes, group.dessinateurs]
                .filter(Boolean).filter((name, i, arr) => arr.indexOf(name) === i).join(', ');
            const seriesHeading = group.seriesId
                ? `<a href="/series/${group.seriesId}">${reviewEscape(group.seriesTitle)}</a>`
                : reviewEscape(group.seriesTitle);
            const countBadge = group.reviews.length > 1 ? `<span class="review-group-count">${group.reviews.length} tomes</span>` : '';
            const closeAllBtn = group.reviews.length > 1
                ? `<button class="review-icon-btn review-group-close-btn" type="button" title="Supprimer les ${group.reviews.length} validations de cette série" onclick="resolveReviewGroup([${group.reviews.map(r => r.id).join(',')}], this)">${svgIcon('x')}</button>`
                : '';
            const tomesHtml = group.reviews.map(review => _reviewTomeHtml(review)).join('');
            return `<article class="review-card">
                <div class="review-card-header">
                    <div><h2>${seriesHeading}</h2>${authorNames ? `<div class="review-group-author">${reviewEscape(authorNames)}</div>` : ''}</div>
                    <div class="review-group-header-actions">${countBadge}${closeAllBtn}</div>
                </div>
                <div class="review-group-items">${tomesHtml}</div>
            </article>`;
        }).join('');
        _loadReviewEd2kAvailability(reviews);
    } catch (error) {
        root.innerHTML = `<div class="review-empty">${svgIcon('circle-x')} Impossible de charger la file de validation : ${reviewEscape(error.message)}</div>`;
    }
}

// Retire un tome résolu (téléchargé ou écarté) de sa carte de groupe - retire aussi le
// groupe entier s'il ne lui reste plus aucun tome, et met à jour le badge "N tomes" du
// groupe restant plutôt que de tout recharger depuis le serveur pour un seul changement.
function _removeReviewTome(reviewId) {
    expandedReviewCandidates.delete(reviewId);
    const tomeEl = document.querySelector(`.review-tome[data-review-id="${reviewId}"]`);
    if (!tomeEl) return;
    const groupEl = tomeEl.closest('.review-card');
    tomeEl.remove();
    if (groupEl) {
        const remaining = groupEl.querySelectorAll('.review-tome').length;
        if (remaining === 0) {
            groupEl.remove();
        } else if (remaining > 1) {
            const badge = groupEl.querySelector('.review-group-count');
            if (badge) badge.textContent = `${remaining} tomes`;
            const closeAllBtn = groupEl.querySelector('.review-group-close-btn');
            if (closeAllBtn) closeAllBtn.setAttribute('onclick', `resolveReviewGroup([${[...groupEl.querySelectorAll('.review-tome')].map(el => el.dataset.reviewId).join(',')}], this)`);
        } else {
            // Il ne reste qu'un seul tome: la croix "fermer tout le groupe" n'a plus de
            // sens (celle du tome lui-même, juste en dessous, fait déjà la même chose).
            groupEl.querySelector('.review-group-count')?.remove();
            groupEl.querySelector('.review-group-close-btn')?.remove();
        }
    }
    if (!document.querySelector('.review-card')) {
        const root = document.getElementById('review-list');
        if (root) root.innerHTML = `<div class="review-empty">${svgIcon('check')} Aucune action en attente de validation.</div>`;
    }
}

async function resolveReviewGroup(reviewIds, button) {
    if (!reviewIds.length) return;
    if (!confirm(`Supprimer les ${reviewIds.length} validations de cette série ?`)) return;
    button.disabled = true;
    const toastId = 'auto-review-group-resolve';
    let failed = 0;
    for (let i = 0; i < reviewIds.length; i++) {
        showToast(toastId, `Suppression... (${i}/${reviewIds.length})`, {icon: 'loader-circle'});
        try {
            const response = await fetch(`/api/auto-acquire/reviews/${reviewIds[i]}/resolve`, {method: 'POST'});
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'échec');
            _removeReviewTome(reviewIds[i]);
        } catch (e) {
            failed++;
        }
    }
    showToast(toastId, failed ? `Terminé avec ${failed} ${pluralize(failed, 'échec')}` : 'Terminé',
        {icon: failed ? 'triangle-alert' : 'check', autoHideMs: 5000});
}

async function downloadReviewCandidate(reviewId, candidateIndex, button) {
    button.disabled = true;
    try {
        const response = await fetch(`/api/auto-acquire/reviews/${reviewId}/download`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({candidate_index:candidateIndex}) });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.message || 'Le téléchargement n’a pas pu être lancé.');
        _removeReviewTome(reviewId);
        if (typeof showToast === 'function') showToast('auto-review-download', `Fichier ajouté au téléchargement : ${data.message || 'candidat sélectionné'}.`, {icon:'check', autoHideMs:5000});
    } catch (error) {
        button.disabled = false;
        if (typeof showToast === 'function') showToast('auto-review-download-error', `Le fichier n’a pas été ajouté : ${error.message}`, {icon:'circle-x', autoHideMs:6000});
    }
}

async function resolveAutoAcquireReview(reviewId) {
    const response = await fetch(`/api/auto-acquire/reviews/${reviewId}/resolve`, {method:'POST'});
    const data = await response.json();
    if (!response.ok || !data.success) {
        if (typeof showToast === 'function') showToast('auto-review-error', data.error || 'La validation n’a pas pu être supprimée.', {icon:'circle-x', autoHideMs:5000});
        return;
    }
    _removeReviewTome(reviewId);
}

document.addEventListener('DOMContentLoaded', loadAutoAcquireReviews);
