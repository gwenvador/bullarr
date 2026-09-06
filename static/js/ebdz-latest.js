// Page "Nouveautés": EBDZ + Telegram fusionnés dans UNE SEULE timeline triée par date,
// même style/structure que la page Historique (voir history.js/history.html -
// "nouveautés utilise le même style que historique") plutôt que deux tableaux séparés
// avec leur propre habillage. Un sujet EBDZ reste dépliable (clic -> liste de ses
// fichiers, données déjà chargées côté client, pas de requête supplémentaire contrairement
// à Historique qui charge le détail d'un import à la demande) ; une ligne Telegram
// représente un seul fichier, non dépliable, avec son action de téléchargement directement
// dans la ligne.

let _nouveautesAutoAddLibraryId = null;
async function _nouveautesResolveAutoAddLibraryId() {
    if (_nouveautesAutoAddLibraryId) return _nouveautesAutoAddLibraryId;
    try {
        const libs = await (await fetch('/api/libraries')).json();
        if (Array.isArray(libs) && libs.length) _nouveautesAutoAddLibraryId = libs[0].id;
    } catch (e) { /* pas de bibliothèque résolue - _resolveSeriesForAutoAdd renverra null */ }
    return _nouveautesAutoAddLibraryId;
}

// Renvoie {seriesId, alreadyExists} si une correspondance suffisamment confiante a été
// trouvée ET associée/créée en bibliothèque, sinon null (incertain - à indiquer dans le
// toast par l'appelant, jamais deviné).
// rawHint: "L'autre_T03_Jaalab_Lilian_Martin_Glénat_2024@... matche Autre (Manù) au lieu
// de Autre (Lylian/Martín)... does not make sense" - `title` est déjà le titre NETTOYÉ
// (parse_filename a jeté auteur/éditeur/année), le seul texte qui reste avec cette
// information est le nom de fichier/titre ORIGINAL - transmis ici en plus pour départager
// une série homonyme sur Bédéthèque (voir search_and_get_best_match/raw_hint,
// scraper.py) plutôt que de perdre ce signal avant même la recherche.
async function _resolveSeriesForAutoAdd(title, rawHint) {
    try {
        const infoData = await (await fetch('/api/bedetheque/info?title=' + encodeURIComponent(title) + (rawHint ? '&raw=' + encodeURIComponent(rawHint) : ''))).json();
        if (!infoData.success || !infoData.info?.url) return null;
        const libraryId = await _nouveautesResolveAutoAddLibraryId();
        if (!libraryId) return null;
        const addResponse = await fetch('/api/bedetheque/add-series', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // "tu telecharge le fichier, tu ajoutes la série, tu ne fait pas de recherche
            // auto puisque le fichier a deja ete telecharge" - sans skip_auto_acquire,
            // add-series lance en plus sa PROPRE recherche+téléchargement automatique pour
            // toute la série (auto_acquire_on_add_enabled) - téléchargeant une SECONDE fois
            // le même fichier déjà envoyé juste avant par autoAddNouveautesEbdzThread/
            // autoAddNouveautesTelegramFile (voir _autoAddResultToast). Constaté en réel :
            // "Le Marche-Lune" téléchargé deux fois, chaque tentative épuisant un peu plus
            // le flood-wait Telegram de ce fichier. Même correctif que
            // match_manual_review_series (auto_acquire.py) pour exactement la même raison.
            body: JSON.stringify({ url: infoData.info.url, library_id: libraryId, skip_auto_acquire: true })
        });
        const addData = await addResponse.json();
        if (!addResponse.ok || !addData.success) return null;
        return { seriesId: addData.series_id, alreadyExists: !!addData.already_exists };
    } catch (e) {
        return null;
    }
}

async function _queueUnresolvedSeriesMatch(title, event) {
    const candidates = event.type === 'ebdz'
        ? (event.links || []).map(link => ({ ...link, source: 'ebdz', filename: decodeFilename(link.filename || ''), thread_url: event.url }))
        : [{
            source: 'telegram', channel: event.channel, message_id: event.message_id,
            channel_title: event.channel_title || '', filename: event.filename || title,
            title: event.filename || title,
            thread_url: (event.channel && event.message_id) ? `https://t.me/${event.channel}/${event.message_id}` : undefined,
        }];
    if (!candidates.length) return;
    try {
        await fetch('/api/auto-acquire/reviews/series-match', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_title: title, candidates }),
        });
    } catch (e) { /* le téléchargement ne doit pas être annulé par la file de validation */ }
}


async function addToEmuleFromNouveautesLink(link, button, title, event) {
    const knownSeriesId = event.matched_series_id ?? null;
    const rawHint = decodeFilename(title) || event.title;
    const [, resolved] = await Promise.all([
        addToEmule(link, button, title, knownSeriesId, null, null, 'ebdz', event.url || ''),
        knownSeriesId ? Promise.resolve(null) : _resolveSeriesForAutoAdd(event.title, rawHint),
    ]);
    if (!knownSeriesId && resolved) {
        await _attachSeriesToPendingDownload('amule', resolved.seriesId, { link });
    }
}

async function _attachSeriesToPendingDownload(client, seriesId, identity) {
    for (let attempt = 0; attempt < 4; attempt++) {
        if (attempt > 0) await new Promise(r => setTimeout(r, 1500));
        try {
            const response = await fetch('/api/missing-monitor/attach-pending-series', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client, series_id: seriesId, ...identity })
            });
            const data = await response.json();
            if (data.attached) return true;
        } catch (e) { /* best-effort */ }
    }
    return false;
}

async function _autoAddResultToast(title, resolved, event) {
    if (resolved) {
        showToast('nouveautes-auto-add', `« ${title} » : téléchargement lancé${resolved.alreadyExists ? '' : ', série ajoutée à la bibliothèque'}.`, { icon: 'check', autoHideMs: 5000, href: `/series/${resolved.seriesId}` });
    } else {
        await _queueUnresolvedSeriesMatch(title, event);
        showToast('nouveautes-auto-add', `« ${title} » : téléchargement lancé, mais correspondance de série trop incertaine pour l'associer automatiquement - à matcher manuellement.`, { icon: 'triangle-alert', autoHideMs: 7000 });
    }
}

async function autoAddNouveautesEbdzThread(index, button) {
    const ev = allNouveautesEvents[index];
    if (!ev || ev._autoAddTriggered) return;
    ev._autoAddTriggered = true;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    showToast('nouveautes-auto-add', `« ${ev.title} » : ajout automatique en cours…`, { icon: 'loader-circle' });
    try {
        // "the download should start automatically. no need to have a match to download
        // it" - à raison: _resolveSeriesForAutoAdd (recherche+matching Bédéthèque) peut
        // prendre jusqu'à ~30s pour une franchise à homonymes nombreux (voir
        // MAX_HOMONYM_FETCHES, scraper.py) et était jusqu'ici AWAIT avant même de lancer
        // le téléchargement - le fichier n'était donc mis en file d'attente qu'une fois
        // le matching terminé, contredisant "jamais bloquant sur l'incertitude" (voir
        // CLAUDE.md, déjà le principe documenté pour l'acquisition auto normale). Les deux
        // tournent maintenant en parallèle (Promise.all) : le téléchargement démarre sans
        // attendre le résultat du matching, seriesId toujours null au moment de l'appel
        // (jamais connu à temps de toute façon vu le délai possible) - un fichier arrivé
        // sans série pré-attachée est rattaché après coup dès que _resolveSeriesForAutoAdd
        // aboutit (voir _attachSeriesToPendingDownload plus bas), pour rester matchable
        // automatiquement par le pipeline d'import normal.
        const rawHint = (ev.links && ev.links[0] && decodeFilename(ev.links[0].filename)) || ev.title;
        const [resolved] = await Promise.all([
            _resolveSeriesForAutoAdd(ev.title, rawHint),
            // Séquentiel entre fichiers (comme avant), seulement mené en parallèle DE la
            // résolution ci-dessus - pas de raison de paralléliser les ajouts entre eux,
            // seule l'attente sur le matching devait disparaître.
            (async () => {
                for (const link of ev.links) {
                    await addToEmule(link.link, document.createElement('button'), decodeFilename(link.filename), null, null, link.volume ?? null, 'ebdz', ev.url || '');
                }
            })(),
        ]);
        if (resolved) {
            for (const link of ev.links) _attachSeriesToPendingDownload('amule', resolved.seriesId, { link: link.link });
        }
        await _autoAddResultToast(ev.title, resolved, ev);
        button.innerHTML = svgIcon('check');
        button.style.color = '#28a745';
    } catch (error) {
        ev._autoAddTriggered = false;
        // Même id que le toast "en cours" posé au clic (voir plus haut) - remplace ce
        // toast au lieu d'en empiler un second, qui laisserait "en cours…" affiché pour
        // toujours à côté de l'erreur.
        showToast('nouveautes-auto-add', `« ${ev.title} » : échec de l'ajout automatique - ${error.message}`, { icon: 'circle-x', autoHideMs: 7000 });
        button.disabled = false;
        button.innerHTML = original;
    }
}

async function autoAddNouveautesTelegramFile(index, button) {
    const ev = allNouveautesEvents[index];
    if (!ev || ev._autoAddTriggered) return;
    ev._autoAddTriggered = true;
    const title = ev.parsed_title || ev.filename;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    // Voir le commentaire jumeau dans autoAddNouveautesEbdzThread ci-dessus.
    showToast('nouveautes-auto-add', `« ${title} » : ajout automatique en cours…`, { icon: 'loader-circle' });
    try {
        // Voir le commentaire jumeau dans autoAddNouveautesEbdzThread ci-dessus - le
        // téléchargement ne doit plus attendre le matching (jusqu'à ~30s possible), les
        // deux tournent en parallèle. ev.filename: le nom de fichier ORIGINAL (avant que
        // parse_filename n'ait jeté auteur/éditeur/année dans des champs séparés pour ne
        // garder que `title`) - voir rawHint plus haut.
        const [resolved] = await Promise.all([
            _resolveSeriesForAutoAdd(title, ev.filename),
            downloadTelegramFile(ev.channel, ev.message_id, document.createElement('button'), ev.channel_title || '', ev.filename || '', null, null, ev.volume ?? null),
        ]);
        // Voir le commentaire jumeau dans autoAddNouveautesEbdzThread ci-dessus - fire-
        // and-forget, jamais attendu avant le toast de résultat.
        if (resolved) _attachSeriesToPendingDownload('telegram', resolved.seriesId, { channel: ev.channel, message_id: ev.message_id });
        await _autoAddResultToast(title, resolved, ev);
        // Voir le commentaire jumeau dans autoAddNouveautesEbdzThread ci-dessus.
        button.innerHTML = svgIcon('check');
        button.style.color = '#28a745';
    } catch (error) {
        // Voir le commentaire jumeau dans autoAddNouveautesEbdzThread ci-dessus.
        ev._autoAddTriggered = false;
        showToast('nouveautes-auto-add', `« ${title} » : échec de l'ajout automatique - ${error.message}`, { icon: 'circle-x', autoHideMs: 7000 });
        button.disabled = false;
        button.innerHTML = original;
    }
}

let allNouveautesEvents = [];
let currentNouveautesFilter = 'all'; // 'all' | 'ebdz' | 'telegram'
// Instantané du dernier passage sur la page, avant qu'il soit avancé à la fin du
// chargement. Il permet de colorer les mêmes éléments que le badge de la sidebar, sans
// conserver un état supplémentaire côté serveur.
let nouveautesUnreadSince = null;
// Tri par en-tête cliquable ("tous les tableaux doivent pouvoir etre ordonné en cliquant
// sur leur header") - column: null revient à l'ordre par défaut (date décroissante, déjà
// appliqué à allNouveautesEvents au chargement).
let nouveautesSort = { column: null, direction: 'asc' };
let nouveautesOriginFilters = new Set();

function _nouveautesOrigin(e) {
    return e.type === 'ebdz' ? (e.category || '—') : (e.channel_title || e.channel || '—');
}

function _nouveautesMatched(e) {
    // matched_series_id est l'identité locale la plus directe. Le fallback couvre
    // les réponses plus anciennes/cache qui ne contiennent que already_matched_thread.
    return e.type === 'ebdz'
        ? (!!e.matched_series_id || !!e.already_matched_thread || !!e.already_in_library)
        : !!e.already_in_library;
}

function _nouveautesIsNew(event) {
    if (!nouveautesUnreadSince || !event?.date) return false;
    // event.date vient de SQLite (UTC, sans fuseau dans la chaîne) tandis que
    // nouveautesUnreadSince vient de Date.toISOString() (déjà UTC, avec 'Z') - comparer
    // l'un mal interprété comme heure LOCALE contre l'autre correctement en UTC décalait
    // le seuil "nouveau" de l'écart horaire local/UTC (1h/2h selon la saison). Voir
    // parseDbUtcDate, nav.js.
    const eventTime = parseDbUtcDate(event.date)?.getTime();
    const sinceTime = new Date(nouveautesUnreadSince).getTime();
    return Number.isFinite(eventTime) && Number.isFinite(sinceTime) && eventTime > sinceTime;
}

function _nouveautesSortValue(e, column) {
    switch (column) {
        case 'date': return e.date ? (parseDbUtcDate(e.date)?.getTime() ?? 0) : 0;
        case 'type': return e.type === 'ebdz' ? 'EBDZ' : 'Telegram';
        case 'origin': return _nouveautesOrigin(e).toLowerCase();
        case 'title': return (e.type === 'ebdz' ? e.title : e.filename || '').toLowerCase();
        case 'matched': return _nouveautesMatched(e) ? 1 : 0;
        // "Actions" n'a plus de notion de possédée/non possédée (déjà dans la colonne
        // Série, voir _nouveautesMatched) depuis que ce doublon a été retiré d'ici - trie
        // à la place par activité d'import (en attente > importé > aucune), le seul
        // signal qui reste vraiment "à trier" dans cette colonne.
        case 'status': {
            const s = _nouveautesImportStatus(e);
            return s === 'pending' ? 2 : s === 'imported' ? 1 : 0;
        }
        default: return 0;
    }
}

function _compareNouveautesEvents(a, b, column, direction) {
    const va = _nouveautesSortValue(a, column);
    const vb = _nouveautesSortValue(b, column);
    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    return direction === 'asc' ? cmp : -cmp;
}

function _updateNouveautesSortIndicators() {
    document.querySelectorAll('#nouveautes-events-wrapper [data-sort-column]').forEach(th => {
        const active = th.dataset.sortColumn === nouveautesSort.column;
        const arrow = th.querySelector('.nouveautes-sort-arrow');
        if (arrow) arrow.textContent = active ? (nouveautesSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
        th.style.color = active ? 'var(--color-accent)' : '';
        th.style.fontWeight = active ? '600' : '';
    });
}

function setNouveautesSort(column) {
    if (nouveautesSort.column === column) {
        nouveautesSort.direction = nouveautesSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        nouveautesSort.column = column;
        nouveautesSort.direction = 'asc';
    }
    _updateNouveautesSortIndicators();
    renderNouveautesEvents();
}
let nouveautesDaysWindow = 60;
const NOUVEAUTES_WINDOW_STEP = 60;
let nouveautesHasMoreEbdz = false;
let nouveautesDisplayedCount = 0;
const NOUVEAUTES_PAGE_SIZE = 30;

let nouveautesPendingFilenames = new Set();

function _nouveautesImportStatus(event) {
    const filenames = event.type === 'ebdz' ? (event.links || []).map(l => l.filename) : [event.filename];
    if (filenames.some(f => nouveautesPendingFilenames.has(f))) return 'pending';
    if (event.type === 'telegram' && event.downloaded) return 'imported';
    return null;
}

function _nouveautesMatchedHtml(event) {
    const seriesId = event.type === 'ebdz' ? event.matched_series_id : event.series_id;
    const bedethequeUrl = event.type === 'ebdz' ? event.matched_bedetheque_url : event.bedetheque_url;
    if (!_nouveautesMatched(event)) {
        // "in telegram if there is a bedetheque link use it in the nouveautes serie
        // column" - un lien extrait de la légende du message Telegram (bedetheque_url_hint
        // côté backend) reste utile même si la série n'est pas encore dans la bibliothèque:
        // un aperçu direct de l'album au lieu de forcer un aller-retour par Découvrir.
        if (bedethequeUrl) {
            return `<a href="${escapeHtml(bedethequeUrl)}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir sur Bédéthèque (série pas encore dans votre bibliothèque)" onclick="event.stopPropagation()"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:14px; height:14px; vertical-align:-2px;"></a>`;
        }
        return '<span style="color:var(--color-text-muted);">—</span>';
    }
    const matchedSeriesTitle = event.type === 'ebdz' ? event.matched_series_title : event.series_title;
    const checkTooltip = matchedSeriesTitle
        ? `Déjà dans votre bibliothèque : ${escapeForAttribute(matchedSeriesTitle)}`
        : 'Déjà dans votre bibliothèque';
    const checkHtml = seriesId
        ? `<a href="/series/${seriesId}" class="icon-owned" data-tooltip="${checkTooltip} - ouvrir la fiche" onclick="event.stopPropagation()">${svgIcon('check')}</a>`
        : `<span class="icon-owned" data-tooltip="${checkTooltip}">${svgIcon('check')}</span>`;
    const changeMatchHtml = (event.type === 'ebdz' && seriesId)
        ? `<a href="/series/${seriesId}?open_ebdz_match=1" class="icon-owned" data-tooltip="Ce n'est pas la bonne série ? Changer le match EBDZ" onclick="event.stopPropagation()">${svgIcon('pencil')}</a>`
        : (event.type === 'telegram' && seriesId)
        ? `<a href="#" class="icon-owned" data-tooltip="Ce n'est pas la bonne série ? Changer le match" onclick="event.stopPropagation(); event.preventDefault(); openTelegramMatchOverrideModal('${escapeForAttribute(event.filename)}', ${seriesId})">${svgIcon('pencil')}</a>`
        : '';
    const bedethequeHtml = bedethequeUrl
        ? `<a href="${escapeHtml(bedethequeUrl)}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir sur Bédéthèque" onclick="event.stopPropagation()"><img src="/static/img/bedetheque-logo.png" alt="Bédéthèque" style="width:14px; height:14px; vertical-align:-2px;"></a>`
        : '';
    // "compares les volumes existants avec ceux nouveau et si ceux de nouveautés sont
    // manquants ou non de la série" - en plus du ✓ "série connue" ci-dessus, signale si ce
    // qui vient d'être trouvé apporte VRAIMENT quelque chose de nouveau (un tome/intégrale/
    // HS/épisode absent de la bibliothèque, voir already_owned/missing_links_count côté
    // API) ou si c'est déjà entièrement possédé - sans quoi "Série ✓" ne dit rien de plus
    // que "vous avez déjà cette série", pas "ce fichier vous intéresse".
    if (event.type === 'ebdz') {
        const missing = event.missing_links_count || 0;
        if (missing > 0) {
            return `${checkHtml} ${changeMatchHtml} ${bedethequeHtml} <span class="badge-missing" data-tooltip="${missing} fichier${missing > 1 ? 's' : ''} de ce sujet manquant${missing > 1 ? 's' : ''} dans votre bibliothèque">${missing}</span>`;
        }
        return `${checkHtml} ${changeMatchHtml} ${bedethequeHtml}`;
    }
    if (event.already_owned === false) {
        return `${checkHtml} ${changeMatchHtml} ${bedethequeHtml} <span class="badge-missing" data-tooltip="Ce fichier n'est pas encore dans votre bibliothèque">manquant</span>`;
    }
    return `${checkHtml} ${changeMatchHtml} ${bedethequeHtml}`;
}

// "dans nouveautés ca a match une mauvaise serie... changer ca et selectionner
// manuellement la série" côté Telegram (voir changeMatchHtml, _nouveautesMatchedHtml
// ci-dessus) - même principe de liste filtrable que openMergeSeriesModal (library.js),
// réimplémenté ici (cette page ne charge pas library.js, 7500+ lignes, voir
// series-detail.html pour le choix équivalent côté EBDZ) mais listant TOUTES les
// bibliothèques (pas une seule): Nouveautés n'a pas de "série source" dont on connaîtrait
// déjà la bibliothèque, contrairement à une fusion lancée depuis une fiche série précise.
async function openTelegramMatchOverrideModal(filename, currentSeriesId) {
    let modal = document.getElementById('telegram-match-override-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'telegram-match-override-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = '<div class="modal-content"><div class="loading"><div class="spinner"></div></div></div>';
    modal.classList.add('active');

    try {
        const libs = await (await fetch('/api/libraries')).json();
        const perLibrary = await Promise.all(
            (Array.isArray(libs) ? libs : []).map(lib => fetch(`/api/library/${lib.id}/series`).then(r => r.json()).catch(() => []))
        );
        const candidates = perLibrary.flat();

        modal.innerHTML = `
            <div class="modal-content" style="max-width: 700px;">
                <span class="close-modal" onclick="closeTelegramMatchOverrideModal()">×</span>
                <h2 class="modal-title" style="margin-bottom: 10px;">✏️ Changer le match</h2>
                <p class="modal-subtitle">Choisissez la bonne série pour tous les fichiers Telegram nommés comme
                    <strong>${escapeHtml(filename)}</strong> (s'applique à chaque fichier partageant ce titre, pas
                    seulement celui-ci).</p>
                <input type="text" id="telegram-match-override-filter" class="search-box" placeholder="Filtrer les séries..."
                       style="width: 100%; margin-bottom: 12px;">
                <div id="telegram-match-override-candidates" style="max-height: 50vh; overflow-y: auto;"></div>
            </div>
        `;

        const candidatesEl = document.getElementById('telegram-match-override-candidates');
        const filterInput = document.getElementById('telegram-match-override-filter');

        const renderCandidates = () => {
            // "les filtres de l'application ne devrait pas avoir a différencier les
            // accents" - même normalisation que le filtre Nouveautés ci-dessus.
            const needle = navNormalizeSearch(filterInput.value.trim());
            const matches = candidates.filter(c => navNormalizeSearch(c.title).includes(needle)).slice(0, 50);
            // Le match courant remonte en tête de liste plutôt que de rester noyé dans
            // l'ordre alphabétique/API par défaut - on ouvre cette modale précisément
            // parce que le match est faux, mais le vérifier d'un coup d'œil (avant de le
            // remplacer) reste utile, surtout si le nom exact du candidat correct diffère
            // légèrement du filename affiché.
            matches.sort((a, b) => {
                const aCurrent = currentSeriesId != null && a.id === currentSeriesId;
                const bCurrent = currentSeriesId != null && b.id === currentSeriesId;
                if (aCurrent !== bCurrent) return aCurrent ? -1 : 1;
                return 0;
            });
            // "quand la série courante s'affiche" (voir demande utilisateur) - marque
            // visuellement le candidat qui EST déjà le match courant (currentSeriesId,
            // reçu via openTelegramMatchOverrideModal), le sélectionner reste un no-op sûr
            // côté API mais confirme juste qu'on n'a pas à re-choisir un candidat identique.
            candidatesEl.innerHTML = matches.length ? matches.map(c => {
                const isCurrent = currentSeriesId != null && c.id === currentSeriesId;
                return `
                <div class="series-card${isCurrent ? ' series-card-current-match' : ''}" style="cursor: pointer; margin-bottom: 8px;${isCurrent ? ' border: 2px solid var(--color-primary, #2563eb);' : ''}"
                     onclick="confirmTelegramMatchOverride('${escapeForAttribute(filename)}', ${c.id}, '${escapeForAttribute(c.title)}')">
                    <div class="series-title">${escapeHtml(c.title)}${isCurrent ? ' <span class="badge-current-match" data-tooltip="C\'est la série actuellement associée à ce fichier">✅ Match actuel</span>' : ''}</div>
                    <div class="series-info">${c.total_volumes || 0} album${(c.total_volumes || 0) > 1 ? 's' : ''}</div>
                </div>
            `;
            }).join('') : '<div class="no-data"><p>😕 Aucune série ne correspond</p></div>';
        };

        filterInput.addEventListener('input', renderCandidates);
        renderCandidates();
        filterInput.focus();
    } catch (error) {
        modal.innerHTML = `
            <div class="modal-content">
                <span class="close-modal" onclick="closeTelegramMatchOverrideModal()">×</span>
                <div class="no-data"><p>❌ ${escapeHtml(error.message)}</p></div>
            </div>
        `;
    }
}

function closeTelegramMatchOverrideModal() {
    const modal = document.getElementById('telegram-match-override-modal');
    if (modal) modal.classList.remove('active');
}

async function confirmTelegramMatchOverride(filename, seriesId, seriesTitle) {
    const candidatesEl = document.getElementById('telegram-match-override-candidates');
    candidatesEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const response = await fetch('/api/telegram-channels/match-override', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename, series_id: seriesId })
        });
        const data = await response.json();
        if (!data.success) {
            alert('❌ ' + (data.error || 'Erreur inconnue'));
            closeTelegramMatchOverrideModal();
            return;
        }
        closeTelegramMatchOverrideModal();
        // Recharge les données (pas de rescrape, pas de reset de page/filtres) pour que
        // _annotate_already_in_library reprenne la correction tout juste enregistrée.
        loadNouveautesEvents(false, false);
    } catch (error) {
        alert('❌ Erreur de connexion: ' + error.message);
        closeTelegramMatchOverrideModal();
    }
}

// Icône/texte par fichier dans le détail déplié d'un sujet EBDZ (voir toggleNouveautesFiles)
// - already_owned est calculé par fichier (un même thread peut mélanger des tomes déjà
// possédés et d'autres manquants), contrairement au badge de la colonne Série qui ne
// résume qu'un COMPTE au niveau du thread.
function _nouveautesLinkOwnedHtml(link) {
    if (link.already_owned === true) {
        const tooltip = `En bibliothèque · ${link.filename ? decodeFilename(link.filename) : 'fichier inconnu'} · ${formatBytes(link.filesize)}`;
        return `<span class="icon-owned" data-tooltip="${escapeHtml(tooltip)}">${svgIcon('check')}</span>`;
    }
    if (link.already_owned === false) {
        return `<span class="badge-missing" data-tooltip="Manquant dans votre bibliothèque">Manquant</span>`;
    }
    return '<span style="color:var(--color-text-muted);">—</span>';
}

function _nouveautesImportStatusHtml(event) {
    const status = _nouveautesImportStatus(event);
    if (status === 'pending') {
        return `<span class="icon-owned" style="background:var(--color-accent); color:#fff;" data-tooltip="Déjà téléchargé, en attente d'import">${svgIcon('history')}</span>`;
    }
    if (status === 'imported') {
        return `<span class="icon-owned" data-tooltip="Déjà téléchargé et importé">${svgIcon('check-check')}</span>`;
    }
    return '';
}

function formatSessionDate(scrapedAt) {
    if (!scrapedAt) return '—';
    const d = parseDbUtcDate(scrapedAt);
    return d ? d.toLocaleString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ }) : '—';
}

// Colonne Date du tableau: juste le jour pour rester compact, SAUF pour aujourd'hui où
// l'heure seule suffit déjà à distinguer les entrées entre elles ("ce qui est arrivé
// aujourd'hui retire la date. juste l'heure. identique aux autres historiques" - même
// logique que historyEventRowHtml, history-shared.js). La date/heure complète reste
// consultable via le data-tooltip (voir formatSessionDate).
function formatSessionDateShort(scrapedAt) {
    if (!scrapedAt) return '—';
    const d = parseDbUtcDate(scrapedAt);
    if (!d) return '—';
    const isToday = isSameDisplayDate(d, new Date());
    return isToday
        ? d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', timeZone: BULLARR_DISPLAY_TZ })
        : d.toLocaleDateString('fr-FR', { timeZone: BULLARR_DISPLAY_TZ });
}

function filterNouveautesEvents(type) {
    currentNouveautesFilter = type;
    document.querySelectorAll('#nouveautes-filter-all, #nouveautes-filter-ebdz, #nouveautes-filter-telegram')
        .forEach(btn => btn.classList.remove('history-filter-active'));
    document.getElementById('nouveautes-filter-' + type).classList.add('history-filter-active');
    renderNouveautesEvents();
}

// Sujet EBDZ déplié/replié (données déjà en mémoire dans l'événement lui-même, voir
// loadNouveautesEvents) - même principe que toggleImportFiles côté Historique mais sans
// fetch, la liste des fichiers d'un sujet est déjà connue dès le scrape.
function toggleNouveautesFiles(rowId, event, index) {
    const detailRow = document.getElementById(`${rowId}-files`);
    if (!detailRow) return;
    const showing = detailRow.style.display !== 'none';
    if (showing) {
        detailRow.style.display = 'none';
        return;
    }
    detailRow.style.display = 'table-row';
    const cell = detailRow.querySelector('td');
    if (cell.dataset.built) return;
    cell.dataset.built = '1';

    cell.innerHTML = `
        <div style="padding:4px 8px 8px; display:flex; align-items:center; gap:8px; font-size:12px;">
            <span style="color:var(--color-text-muted); white-space:nowrap;">📅 ${formatSessionDate(event.date)}</span>
            <strong>${escapeHtml(event.title)}</strong>
        </div>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
            <tbody>
                ${event.links.map(link => {
                    const decodedFilename = decodeFilename(link.filename);
                    // "déjà possédé met une couleur différente au fichier comme ça je
                    // peux voir visuellement" - réutilise .volume-table-row-owned (même
                    // vert clair/sombre selon le thème que le tableau des tomes d'une
                    // fiche série, voir style-library-search.css) plutôt qu'une nouvelle
                    // teinte: l'icône ✓ seule (_nouveautesLinkOwnedHtml) demandait de
                    // regarder chaque ligne une à une pour repérer ce qui manque encore.
                    const ownedRowClass = link.already_owned === true ? ' volume-table-row-owned' : '';
                    return `
                        <tr class="${ownedRowClass.trim()}" style="border-top:1px solid var(--color-border);">
                            <td style="padding:6px 8px; color:var(--color-text-muted);">${escapeHtml(decodedFilename)}</td>
                            <td style="padding:6px 8px; white-space:nowrap;">${link.parsed_volume ? escapeHtml(link.parsed_volume) : '—'}</td>
                            <td style="padding:6px 8px; white-space:nowrap; text-align:center;">${_nouveautesLinkOwnedHtml(link)}</td>
                            <td style="padding:6px 8px; white-space:nowrap;">${formatBytes(link.filesize)}</td>
                            <td style="padding:6px 8px; text-align:right; white-space:nowrap;">
                                <button class="btn-icon-only" onclick="copyLink('${escapeForAttribute(link.link)}', this)" data-tooltip="Copier le lien ed2k">${svgIcon('copy')}</button>
                                <button class="btn-icon-only result-action-emule" onclick="addToEmuleFromNouveautesLink('${escapeForAttribute(link.link)}', this, '${escapeForAttribute(decodedFilename)}', allNouveautesEvents[${index}])" data-tooltip="Ajouter à eMule/aMule"><img src="/static/img/emule-logo.svg" alt="" class="torrent-client-logo"></button>
                            </td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table>
    `;
    checkEmuleStatus();
}

function _nouveautesEbdzRowHtml(event, index) {
    const rowId = `nouveautes-row-${index}`;
    const safeTitle = escapeHtml(event.title);
    const typeIconHtml = event.url
        ? `<a href="${escapeHtml(event.url)}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir « ${safeTitle} » sur EBDZ" onclick="event.stopPropagation()"><img src="/static/img/ebdz-logo.png" alt="EBDZ" style="width:16px; height:16px; vertical-align:-3px;"></a>`
        : `<span data-tooltip="EBDZ"><img src="/static/img/ebdz-logo.png" alt="EBDZ" style="width:16px; height:16px; vertical-align:-3px;"></span>`;
    const threadStatusTooltip = event.is_new_thread ? 'Nouveau sujet, jamais vu avant' : 'Sujet déjà connu, nouveau tome ajouté';
    const threadStatusDotClass = event.is_new_thread ? 'thread-status-dot-new' : 'thread-status-dot-existing';
    const addToLibraryHtml = (_nouveautesMatched(event) || !!event.already_in_library || event._autoAddTriggered) ? '' : `
        <button class="btn-icon-only" onclick="event.stopPropagation(); autoAddNouveautesEbdzThread(${event._index}, this)" data-tooltip="Ajouter automatiquement « ${safeTitle} » (télécharge tous les fichiers du sujet + associe la série si la correspondance est confiante)">${svgIcon('zap')}</button>
        <button class="btn-icon-only" onclick="event.stopPropagation(); window.location.href='/discover?q=' + encodeURIComponent('${escapeForAttribute(event.title)}')" data-tooltip="Ajouter manuellement « ${safeTitle} » (ouvre Découvrir)">${svgIcon('plus')}</button>
    `;
    const statusHtml = `
        <span class="thread-status-dot ${threadStatusDotClass}" data-tooltip="${threadStatusTooltip}" style="display:inline-block;"></span>
        ${event.already_monitored ? `<span class="badge-monitored" data-tooltip="Cette série fait partie de vos albums surveillés">📊</span>` : ''}
        ${_nouveautesImportStatusHtml(event)}
        ${addToLibraryHtml}
    `;

    return `
        <tr class="nouveautes-event-row ${_nouveautesIsNew(event) ? 'nouveautes-row-new' : ''}" style="border-bottom:1px solid var(--color-border); cursor:pointer;" onclick="toggleNouveautesFiles('${rowId}', allNouveautesEvents[${index}], ${index})">
            <td style="padding:10px; white-space:nowrap;" data-tooltip="${escapeHtml(formatSessionDate(event.date))}">${escapeHtml(formatSessionDateShort(event.date))}</td>
            <td style="padding:10px; white-space:nowrap; text-align:center;">${typeIconHtml}</td>
            <td class="nouveautes-origin-cell" style="padding:10px; color:var(--color-text-muted); font-size:0.9em;">${escapeHtml(_nouveautesOrigin(event))}</td>
            <td class="nouveautes-details-cell" style="padding:10px;">
                <div style="font-weight:600; display:flex; align-items:center; gap:6px;">▸ ${safeTitle}</div>
                <div style="color:var(--color-text-muted); font-size:0.9em;">${event.links.length} fichier${event.links.length > 1 ? 's' : ''}</div>
            </td>
            <td style="padding:10px; text-align:center;">${_nouveautesMatchedHtml(event)}</td>
            <td class="nouveautes-actions-cell" style="padding:10px; text-align:left;">${statusHtml}</td>
        </tr>
        <tr id="${rowId}-files" class="nouveautes-files-row" style="display:none;"><td colspan="6" style="padding:0 10px 10px 30px; background:var(--color-surface-alt);"></td></tr>
    `;
}

function _nouveautesTelegramRowHtml(event) {
    const safeFilename = escapeHtml(event.filename);
    const ownedRowClass = event.already_owned === true ? ' volume-table-row-owned' : '';
    const newRowClass = _nouveautesIsNew(event) ? ' nouveautes-row-new' : '';
    const addToLibraryQuery = event.parsed_title || event.filename;
    const addToLibraryHtml = (_nouveautesMatched(event) || !!event.already_in_library || event._autoAddTriggered) ? '' : `
        <button class="btn-icon-only" onclick="autoAddNouveautesTelegramFile(${event._index}, this)" data-tooltip="Ajouter automatiquement « ${escapeHtml(addToLibraryQuery)} » (télécharge + associe la série si la correspondance est confiante)">${svgIcon('zap')}</button>
        <button class="btn-icon-only" onclick="window.location.href='/discover?q=' + encodeURIComponent('${escapeForAttribute(addToLibraryQuery)}')" data-tooltip="Ajouter manuellement « ${escapeHtml(addToLibraryQuery)} » (ouvre Découvrir)">${svgIcon('plus')}</button>
    `;
    return `
        <tr class="nouveautes-event-row ${(ownedRowClass + newRowClass).trim()}" style="border-bottom:1px solid var(--color-border);">
            <td style="padding:10px; white-space:nowrap;" data-tooltip="${escapeHtml(formatSessionDate(event.date))}">${escapeHtml(formatSessionDateShort(event.date))}</td>
            <td style="padding:10px; white-space:nowrap; text-align:center;" data-tooltip="Telegram"><img src="/static/img/telegram-logo.svg" alt="Telegram" style="width:16px; height:16px; vertical-align:-3px;"></td>
            <td class="nouveautes-origin-cell" style="padding:10px; color:var(--color-text-muted); font-size:0.9em;">${escapeHtml(_nouveautesOrigin(event))}</td>
            <td class="nouveautes-details-cell" style="padding:10px;">
                <div style="font-weight:600;">${safeFilename}</div>
                <div style="color:var(--color-text-muted); font-size:0.9em;">
                    ${escapeHtml(event.channel_title || event.channel)} · ${formatBytes(event.file_size)}
                    ${event.parsed_volume ? ` · ${escapeHtml(event.parsed_volume)}` : ''}
                </div>
            </td>
            <td style="padding:10px; text-align:center;">${_nouveautesMatchedHtml(event)}</td>
            <td class="nouveautes-actions-cell" style="padding:10px; text-align:left;">
                <div style="display:flex; align-items:center; justify-content:flex-start; gap:8px;">
                    <button class="btn-icon-only" onclick="downloadTelegramFile('${escapeForAttribute(event.channel)}', ${event.message_id}, this, '${escapeForAttribute(event.channel_title || '')}', '${escapeForAttribute(event.filename || '')}', ${event.series_id ?? 'null'}, null, ${event.volume ?? 'null'})" data-tooltip="Télécharger vers l'import">${svgIcon('download')}</button>
                    ${event.already_monitored ? `<span class="badge-monitored" data-tooltip="Cette série fait partie de vos albums surveillés">📊</span>` : ''}
                    ${_nouveautesImportStatusHtml(event)}
                    ${addToLibraryHtml}
                </div>
            </td>
        </tr>
    `;
}

// Menu de filtre "Origine" reconstruit depuis les événements déjà chargés (pas de liste
// figée côté serveur: les catégories de forum EBDZ/canaux Telegram dépendent de la config
// de l'utilisateur) - même pattern que populateDynamicFilterOptions/toggleSeriesFilter
// (bibliothèque, library.js): un bouton par valeur, actif/inactif selon
// nouveautesOriginFilters, qui reste ouvert entre deux clics pour en cocher plusieurs.
function _populateNouveautesOriginOptions() {
    const menu = document.getElementById('nouveautesOriginFilterMenu');
    const countEl = document.getElementById('nouveautesOriginFilterCount');
    if (!menu || !countEl) return;

    // Scopé au filtre de type actif (Tous/EBDZ/Telegram, voir filterNouveautesEvents) -
    // sinon le menu "Origine" listait aussi les canaux Telegram quand on ne regarde que
    // l'onglet EBDZ (et inversement), au lieu de ne proposer que les origines pertinentes
    // pour ce qui est effectivement affiché.
    const eventsForOrigins = currentNouveautesFilter === 'all'
        ? allNouveautesEvents
        : allNouveautesEvents.filter(e => e.type === currentNouveautesFilter);
    const origins = [...new Set(eventsForOrigins.map(_nouveautesOrigin).filter(o => o && o !== '—'))]
        .sort((a, b) => a.localeCompare(b, 'fr', { sensitivity: 'base' }));
    // Une origine cochée qui a disparu de l'ensemble actuel (ex: filtre EBDZ/Telegram
    // changé entre-temps) ne doit pas rester "fantôme" dans le Set indéfiniment
    for (const value of nouveautesOriginFilters) {
        if (!origins.includes(value)) nouveautesOriginFilters.delete(value);
    }

    menu.innerHTML = origins.length === 0
        ? '<div style="padding:8px 10px; color:#999; font-size:0.85em;">Aucune origine</div>'
        : origins.map(o => `
            <button type="button" class="toolbar-dropdown-item${nouveautesOriginFilters.has(o) ? ' active' : ''}"
                    onclick="toggleNouveautesOriginFilter('${escapeForAttribute(o)}')">${escapeHtml(o)}</button>
        `).join('');

    countEl.textContent = nouveautesOriginFilters.size ? `(${nouveautesOriginFilters.size})` : '';
    document.getElementById('nouveautesOriginFilterBtn')?.classList.toggle('toolbar-btn-filter-active', nouveautesOriginFilters.size > 0);
}

function toggleNouveautesOriginFilter(value) {
    if (nouveautesOriginFilters.has(value)) {
        nouveautesOriginFilters.delete(value);
    } else {
        nouveautesOriginFilters.add(value);
    }
    renderNouveautesEvents();
}

function toggleNouveautesOriginMenu(anchorBtn) {
    const menu = document.getElementById('nouveautesOriginFilterMenu');
    if (!menu) return;
    const opening = menu.style.display !== 'block';
    if (!opening) {
        menu.style.display = 'none';
        return;
    }

    _populateNouveautesOriginOptions();
    document.body.appendChild(menu);
    const rect = anchorBtn.getBoundingClientRect();
    menu.style.position = 'fixed';
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
}

document.addEventListener('click', (e) => {
    const menu = document.getElementById('nouveautesOriginFilterMenu');
    if (!menu || menu.style.display !== 'block') return;
    if (e.target.closest('#nouveautesOriginFilterMenu') || e.target.closest('#nouveautesOriginFilterBtn')) return;
    menu.style.display = 'none';
});

// resetPage=false uniquement depuis "Charger plus" (loadMoreNouveautesEvents) - un
// changement de filtre/recherche/onglet doit, lui, repartir de la première page plutôt
// que de garder une position de pagination qui n'a plus de sens pour le nouvel ensemble
// filtré (même principe que discoverDisplayedCount dans discover.js).
function renderNouveautesEvents(resetPage = true) {
    const query = navNormalizeSearch(document.getElementById('nouveautesTitleFilter').value.trim());
    const matchedOnly = document.getElementById('nouveautesMatchedOnlyFilter').checked;
    _populateNouveautesOriginOptions();

    let filtered = allNouveautesEvents.filter((e, index) => {
        e._index = index;
        if (currentNouveautesFilter !== 'all' && e.type !== currentNouveautesFilter) return false;
        if (matchedOnly && !_nouveautesMatched(e)) return false;
        if (nouveautesOriginFilters.size && !nouveautesOriginFilters.has(_nouveautesOrigin(e))) return false;
        if (!query) return true;
        const haystack = navNormalizeSearch(e.type === 'ebdz' ? e.title : e.filename);
        return haystack.includes(query);
    });
    // e._index (position dans allNouveautesEvents, utilisé par toggleNouveautesFiles) reste
    // valide après un tri: c'est une propriété posée sur l'objet lui-même pendant le
    // filter() ci-dessus, pas une position recalculée - un sort() qui ne fait que
    // réordonner le tableau filtré ne la casse pas.
    if (nouveautesSort.column) {
        filtered = [...filtered].sort((a, b) => _compareNouveautesEvents(a, b, nouveautesSort.column, nouveautesSort.direction));
    }

    if (resetPage) nouveautesDisplayedCount = Math.min(NOUVEAUTES_PAGE_SIZE, filtered.length);

    const body = document.getElementById('nouveautes-events-body');
    const empty = document.getElementById('nouveautes-events-empty');
    const loadMoreDiv = document.getElementById('nouveautes-events-load-more');

    if (filtered.length === 0) {
        body.innerHTML = '';
        empty.style.display = 'block';
        _renderNouveautesLoadMoreButton(loadMoreDiv, 0);
        return;
    }
    empty.style.display = 'none';

    const visible = filtered.slice(0, nouveautesDisplayedCount);
    body.innerHTML = visible.map(e => e.type === 'ebdz' ? _nouveautesEbdzRowHtml(e, e._index) : _nouveautesTelegramRowHtml(e)).join('');
    checkEmuleStatus();

    _renderNouveautesLoadMoreButton(loadMoreDiv, filtered.length - nouveautesDisplayedCount);
}

function _renderNouveautesLoadMoreButton(loadMoreDiv, remaining) {
    if (remaining > 0) {
        loadMoreDiv.style.display = 'block';
        loadMoreDiv.innerHTML = `<button class="btn" onclick="loadMoreNouveautesEvents()">${svgIcon('chevron-down')} Charger plus (${remaining} restant${remaining > 1 ? 's' : ''})</button>`;
    } else if (nouveautesHasMoreEbdz) {
        loadMoreDiv.style.display = 'block';
        loadMoreDiv.innerHTML = `<button class="btn" onclick="loadOlderNouveautesEvents()">${svgIcon('chevron-down')} Charger une période plus ancienne</button>`;
    } else {
        loadMoreDiv.style.display = 'none';
    }
}

function loadMoreNouveautesEvents() {
    nouveautesDisplayedCount += NOUVEAUTES_PAGE_SIZE;
    renderNouveautesEvents(false);
}

async function loadOlderNouveautesEvents() {
    nouveautesDaysWindow += NOUVEAUTES_WINDOW_STEP;
    nouveautesDisplayedCount += NOUVEAUTES_PAGE_SIZE;
    await loadNouveautesEvents(false, false);
}

// "dans nouveauté ne charge pas tout en meme temps. limite toi à 100 articles pour
// l'instant et charge en background apres" - le premier lot (le plus récent, annotation
// comprise) s'affiche vite; la fenêtre complète (nouveautesDaysWindow jours, potentiellement
// des milliers d'items) se recharge ensuite silencieusement en tâche de fond et remplace
// ce premier jeu une fois prête (voir _loadNouveautesBatch).
const NOUVEAUTES_INITIAL_LIMIT = 100;
let nouveautesLoadGeneration = 0;

async function loadNouveautesEvents(forceScrape, resetPage = true) {
    const loading = document.getElementById('nouveautes-events-loading');
    const btn = document.getElementById('nouveautesRescanBtn');

    // Un chargement complet (visite/actualisation) démarre une nouvelle fenêtre de
    // comparaison avec le badge. Le bouton "Charger une période plus ancienne" conserve
    // volontairement le point de référence de la visite en cours.
    if (resetPage) {
        try { nouveautesUnreadSince = localStorage.getItem('nouveautesLastSeenAt'); }
        catch (e) { nouveautesUnreadSince = null; }
    }

    if (forceScrape) {
        btn.disabled = true;
        btn.innerHTML = '⏳';
        // Sondage partagé (nav.js) démarré tout de suite pour le scrape EBDZ (peut prendre
        // du temps, délai anti-bot par page scrapée) - affiche un toast persistant qui
        // survit à une navigation vers une autre page.
        pollEbdzScrapeStatus();
        await Promise.all([
            fetch('/api/ebdz/scrape', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }).catch(() => {}),
            fetch('/api/telegram-channels/scrape', { method: 'POST' }).catch(() => {})
        ]);
        btn.disabled = false;
        btn.innerHTML = svgIcon('refresh-cw');
    }

    // Une recherche/rechargement plus récent a démarré entre-temps (nouveau clic
    // Actualiser, changement de filtre déclenchant loadOlderNouveautesEvents...) - même
    // garde-fou que bdSearchGeneration/searchBothGeneration (search.js): une réponse
    // tardive d'un chargement périmé ne doit jamais écraser l'affichage courant.
    nouveautesLoadGeneration++;
    const generation = nouveautesLoadGeneration;

    loading.style.display = 'block';
    document.getElementById('nouveautes-events-empty').style.display = 'none';
    document.getElementById('nouveautes-events-body').innerHTML = '';

    try {
        // pendingScanData: fichiers actuellement dans la file d'import (voir
        // _nouveautesImportStatus) - même endpoint que la page /import elle-même, un seul
        // appel supplémentaire par chargement de la page Nouveautés.
        const pendingScanData = await fetch('/api/import/scan', { method: 'POST' }).then(r => r.json()).catch(() => ({ success: false }));
        if (generation !== nouveautesLoadGeneration) return;
        nouveautesPendingFilenames = new Set(
            pendingScanData.success ? (pendingScanData.files || []).map(f => f.filename) : []
        );

        await _loadNouveautesBatch(NOUVEAUTES_INITIAL_LIMIT, generation, resetPage);
        loading.style.display = 'none';

        // Charge le reste de la fenêtre sans bloquer l'affichage déjà visible - volontairement
        // sans await, la fonction appelante n'a pas besoin d'attendre ce second lot.
        _loadNouveautesBatch(null, generation, false).catch(() => {});
    } catch (error) {
        loading.style.display = 'none';
        document.getElementById('nouveautes-events-body').innerHTML =
            `<tr><td colspan="6" style="padding:20px; text-align:center; color:#c0392b;">Erreur: ${escapeHtml(error.message)}</td></tr>`;
    }
}

async function _loadNouveautesBatch(limit, generation, resetPage) {
    const limitParam = limit ? `&limit=${limit}` : '';

    let ebdzEvents = [];
    let telegramEvents = [];

    const ebdzPromise = fetch(`/api/ebdz/latest?days=${nouveautesDaysWindow}${limitParam}`).then(r => r.json()).catch(() => ({ success: false })).then(ebdzData => {
        if (generation !== nouveautesLoadGeneration) return;
        if (ebdzData.success && ebdzData.sessions) {
            ebdzData.sessions.forEach(session => {
                session.results.forEach(thread => {
                    ebdzEvents.push({
                        type: 'ebdz',
                        date: session.scraped_at,
                        title: thread.title,
                        url: thread.url,
                        category: thread.category,
                        already_in_library: thread.already_in_library,
                        already_matched_thread: thread.already_matched_thread,
                        already_monitored: thread.already_monitored,
                        matched_series_id: thread.matched_series_id,
                        matched_series_title: thread.matched_series_title,
                        matched_bedetheque_url: thread.matched_bedetheque_url,
                        is_new_thread: thread.is_new_thread,
                        links: thread.links,
                    });
                });
            });
        }
        nouveautesHasMoreEbdz = !!ebdzData.has_more;
    });

    const telegramPromise = fetch(`/api/telegram-channels/latest?days=${nouveautesDaysWindow}${limitParam}`).then(r => r.json()).catch(() => ({ success: false })).then(telegramData => {
        if (generation !== nouveautesLoadGeneration) return;
        telegramEvents = (telegramData.success ? (telegramData.files || []) : []).map(f => ({
            type: 'telegram',
            date: f.message_date,
            filename: f.filename,
            channel: f.channel,
            channel_title: f.channel_title,
            file_size: f.file_size,
            message_id: f.message_id,
            parsed_title: f.parsed_title,
            parsed_volume: f.parsed_volume,
            already_in_library: f.already_in_library,
            already_monitored: f.already_monitored,
            series_id: f.series_id,
            series_title: f.series_title,
            bedetheque_url: f.bedetheque_url,
            downloaded: !!f.downloaded,
        }));
    });

    await Promise.all([ebdzPromise, telegramPromise]);
    if (generation !== nouveautesLoadGeneration) return;

    allNouveautesEvents = [...ebdzEvents, ...telegramEvents].sort((a, b) => new Date(b.date) - new Date(a.date));
    renderNouveautesEvents(resetPage);

    try {
        localStorage.setItem('nouveautesLastSeenAt', new Date().toISOString());
    } catch (e) { /* localStorage indisponible (navigation privée...) - pas bloquant */ }
}

loadNouveautesEvents();
