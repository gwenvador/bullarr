function reviewEscape(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}

// lgtm [js/incomplete-sanitization] values are escaped for the exact HTML/JavaScript context before this fixed template is inserted.
function reviewAttr(value) { return reviewEscape(value).replace(/'/g, "\\'").replace(/`/g, '&#96;'); }
function reviewSourceUrl(candidate) { return candidate.thread_url || candidate.info_url || candidate.source_link || candidate.link || ''; }
function reviewFileSize(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return '—';
    const units = ['o', 'Ko', 'Mo', 'Go', 'To'];
    let amount = bytes;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
    const digits = unit === 0 || amount >= 100 ? 0 : 1;
    return `${amount.toLocaleString('fr-FR', { maximumFractionDigits: digits })} ${units[unit]}`;
}
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
    const icons = { ebdz:'/static/img/ebdz-logo.png', prowlarr:'/static/img/prowlarr-logo.svg', telegram:'/static/img/telegram-logo.svg', fourtoutici:'/static/img/web-logo.svg', annas_archive:'/static/img/web-logo.svg', rss:'/static/img/rss-logo.svg', vk:'/static/img/vk-logo.svg' };
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
const reviewKnownSeriesUrls = new Map();

async function retryReviewSeriesAddition(reviewId, button) {
    const url = reviewKnownSeriesUrls.get(reviewId);
    if (!url) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    try {
        const librariesResponse = await fetch('/api/libraries');
        const libraries = await librariesResponse.json();
        const libraryId = Array.isArray(libraries) ? libraries[0]?.id : null;
        if (!libraryId) throw new Error('Aucune bibliothèque disponible');
        const addResponse = await fetch('/api/bedetheque/add-series', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, library_id: libraryId, skip_auto_acquire: true })
        });
        const added = await addResponse.json();
        if (!addResponse.ok || !added.success) throw new Error(added.error || 'Ajout de la série impossible');
        const searchResponse = await fetch('/api/bedetheque/auto-acquire/run', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_id: added.series_id })
        });
        const search = await searchResponse.json();
        if (!searchResponse.ok || !search.success) throw new Error(search.error || 'Recherche automatique impossible');
        const resolveResponse = await fetch(`/api/auto-acquire/reviews/${reviewId}/resolve`, { method: 'POST' });
        if (!resolveResponse.ok) throw new Error('Série ajoutée, mais validation non clôturée');
        showToast('review-series-retry', 'Série ajoutée ; recherche automatique lancée.', {
            icon: 'check', autoHideMs: 6000, href: `/series/${added.series_id}`
        });
        await loadAutoAcquireReviews();
    } catch (error) {
        showToast('review-series-retry', error.message, { icon: 'circle-x', autoHideMs: 7000 });
    } finally {
        button.disabled = false;
        button.innerHTML = original;
    }
}

// Fichiers proposés : dépliés d'office quand il y en a peu (≤ 3), repliés au-delà ;
// expandedReviewCandidates garde les tomes que l'utilisateur a basculés à la main.
function _reviewCandidatesOpen(reviewId, count) {
    return (count <= 3) !== expandedReviewCandidates.has(reviewId);
}

function toggleReviewCandidates(reviewId) {
    if (expandedReviewCandidates.has(reviewId)) expandedReviewCandidates.delete(reviewId);
    else expandedReviewCandidates.add(reviewId);
    const list = document.getElementById(`review-candidates-${reviewId}`);
    const toggle = document.getElementById(`review-candidates-toggle-${reviewId}`);
    const count = Number(toggle?.dataset.count || 0);
    const isExpanded = _reviewCandidatesOpen(reviewId, count);
    if (list) list.hidden = !isExpanded;
    toggle?.classList.toggle('review-candidates-toggle-open', isExpanded);
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
        showToast('review-bedetheque-match', `Série et album liés : « ${title} ». Vous pouvez maintenant télécharger le candidat.`, {icon:'check', autoHideMs:5000});
        await loadAutoAcquireReviews();
    } catch (error) {
        showToast('review-bedetheque-match', `Échec du rattachement : ${error.message}`, {icon:'circle-x', autoHideMs:6000});
    }
}

const REVIEW_SOURCE_NAMES = { ebdz: 'EBDZ', prowlarr: 'Prowlarr', telegram: 'Telegram', fourtoutici: 'fourtoutici', annas_archive: "Anna's Archive", vk: 'VK', rss: 'Flux RSS' };

// Raison technique enregistrée par le serveur → phrase qui dit quoi vérifier.
function _reviewReasonHtml(reason) {
    if (!reason) return '';
    const near = reason.match(/Titre détecté « (.+?) » proche mais pas identique à « (.+?) »/);
    const text = near
        ? `Le fichier parle de <strong>« ${reviewEscape(near[1])} »</strong> et la série de la bibliothèque s'appelle <strong>« ${reviewEscape(near[2])} »</strong>. Les noms se ressemblent sans être identiques : vérifiez que c'est bien le bon album avant de le télécharger.`
        : reviewEscape(reason);
    return `<div class="review-why">${svgIcon('info')}<div><span class="review-why-title">À vérifier</span><p>${text}</p></div></div>`;
}

// Un tome (une ligne de validation) au sein de sa carte de série - voir
// loadAutoAcquireReviews pour le regroupement. Dismiss/téléchargement restent par tome
// (chaque candidat garde son propre review.id), seuls le titre/auteur de série remontent
// au niveau du groupe pour ne plus se répéter à chaque tome.
function _reviewTomeHtml(review) {
    const volume = review.volume_label || (review.volume_number == null ? 'Album' : `Tome ${review.volume_number}`);
    const knownUrl = !review.series_id && (review.candidates || []).find(candidate => candidate.bedetheque_url)?.bedetheque_url;
    if (knownUrl) reviewKnownSeriesUrls.set(review.id, knownUrl);
    const bdIcon = '<img src="/static/img/bedetheque-logo.png" alt="" class="review-btn-logo">';
    const retryAction = knownUrl
        ? `<div class="review-tome-note">Série repérée sur Bédéthèque mais pas encore dans la bibliothèque.
            <button class="review-btn" type="button" onclick="retryReviewSeriesAddition(${review.id}, this)">${svgIcon('refresh-cw')} Ajouter la série et relancer</button>
            <a class="review-btn review-btn-ghost" href="${reviewAttr(knownUrl)}" target="_blank" rel="noopener">${bdIcon} Fiche Bédéthèque</a></div>`
        : '';
    const candidateList = (review.candidates || []).slice(0, 20);
    const candidatesHtml = candidateList.map((candidate, index) => {
        const name = reviewDecodeFilename(candidate.filename || candidate.title || 'Résultat sans nom');
        const url = reviewSourceUrl(candidate);
        const size = reviewFileSize(candidate.size);
        const meta = [
            REVIEW_SOURCE_NAMES[candidate.source] || candidate.source || '',
            size !== '—' ? size : '',
        ].filter(Boolean).map(part => `<span>${reviewEscape(part)}</span>`).join('');
        const action = review.series_id
            ? `<button class="review-btn review-btn-primary" type="button" onclick="downloadReviewCandidate(${review.id}, ${candidate.candidate_index ?? index}, this)">${svgIcon('download')} Télécharger</button>`
            : knownUrl ? ''
            : `<button class="review-btn review-btn-primary" type="button" onclick="openReviewBedethequeModal(${review.id}, '${reviewAttr(review.series_title || '')}')">${bdIcon} Associer à une série</button>`;
        return `<div class="review-candidate">
            <span class="review-source">${reviewSourceIcon(candidate) || svgIcon('file-check')}</span>
            <div class="review-candidate-text"><span class="review-candidate-name" title="${reviewAttr(name)}">${reviewEscape(name)}</span>
                <span class="review-candidate-meta">${meta}${_reviewCandidateAvailabilityHtml(candidate, review.id, index)}</span></div>
            <div class="review-candidate-actions">${url ? `<a class="review-btn review-btn-ghost" href="${reviewAttr(url)}" target="_blank" rel="noopener" title="Ouvrir la page d'origine du fichier">${svgIcon('external-link')} Source</a>` : ''}${action}</div>
        </div>`;
    }).join('');
    const count = candidateList.length;
    const isExpanded = _reviewCandidatesOpen(review.id, count);
    const listLabel = count ? `${count} fichier${count > 1 ? 's' : ''} trouvé${count > 1 ? 's' : ''}` : 'Aucun fichier';
    const toggle = count > 0
        ? `<button class="review-candidates-toggle${isExpanded ? ' review-candidates-toggle-open' : ''}" type="button" id="review-candidates-toggle-${review.id}" data-count="${count}" onclick="toggleReviewCandidates(${review.id})">${listLabel} ${svgIcon('chevron-down')}</button>`
        : `<span class="review-candidates-label">${listLabel}</span>`;
    return `<div class="review-tome" data-review-id="${review.id}">
        <div class="review-tome-header">
            <span class="review-volume">${reviewEscape(volume)}</span>
            ${reviewDateHtml(review.created_at)}
            <button class="review-btn review-btn-ghost review-dismiss-btn" type="button" title="Retirer de la liste sans rien télécharger" onclick="resolveAutoAcquireReview(${review.id})">${svgIcon('x')} Ignorer</button>
        </div>
        ${_reviewReasonHtml(review.reason)}
        ${retryAction}
        ${toggle}
        <div class="review-candidates" id="review-candidates-${review.id}"${isExpanded ? '' : ' hidden'}>${candidatesHtml || '<div class="review-meta">Aucun candidat détaillé.</div>'}</div>
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
            // Un même auteur peut être scénariste et dessinateur : chaque nom une seule fois.
            const authorNames = [...new Set([group.scenaristes, group.dessinateurs].filter(Boolean)
                .flatMap(names => String(names).split(/\s*,\s*/)).filter(Boolean))].join(', ');
            const seriesHeading = group.seriesId
                ? `<a href="/series/${group.seriesId}">${reviewEscape(group.seriesTitle)}</a>`
                : reviewEscape(group.seriesTitle);
            const countBadge = group.reviews.length > 1 ? `<span class="review-group-count">${group.reviews.length} albums</span>` : '';
            const closeAllBtn = group.reviews.length > 1
                ? `<button class="review-btn review-btn-ghost review-group-close-btn" type="button" title="Retirer les ${group.reviews.length} albums de cette série" onclick="resolveReviewGroup([${group.reviews.map(r => r.id).join(',')}], this)">${svgIcon('x')} Tout ignorer</button>`
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
            if (badge) badge.textContent = `${remaining} albums`;
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
