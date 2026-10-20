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
async function _resolveSeriesForAutoAdd(title, rawHint, bedethequeUrl = null, reportError = false) {
    try {
        let url = bedethequeUrl;
        if (!/^https:\/\/www\.bedetheque\.com\//i.test(url || '')) {
            const infoData = await (await fetch('/api/bedetheque/info?title=' + encodeURIComponent(title) + (rawHint ? '&raw=' + encodeURIComponent(rawHint) : ''))).json();
            if (!infoData.success || !infoData.info?.url) {
                if (reportError) throw new Error(infoData.error || 'Aucune fiche Bédéthèque confirmée');
                return null;
            }
            url = infoData.info.url;
        }
        const libraryId = await _nouveautesResolveAutoAddLibraryId();
        if (!libraryId) {
            if (reportError) throw new Error('Aucune bibliothèque disponible');
            return null;
        }
        const addResponse = await fetch('/api/bedetheque/add-series', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, library_id: libraryId, skip_auto_acquire: true })
        });
        const addData = await addResponse.json();
        if (!addResponse.ok || !addData.success) {
            if (reportError) throw new Error(addData.error || 'Ajout de la série impossible');
            return null;
        }
        return { seriesId: addData.series_id, alreadyExists: !!addData.already_exists };
    } catch (e) {
        if (reportError) throw e;
        return null;
    }
}

async function _queueNouveautesSeriesReview(ev, title, bedethequeUrl, reason, linkIndex = null) {
    const candidate = {
        source: ev.type === 'rss' ? 'prowlarr' : ev.type,
        title: ev.title || ev.filename || title,
        filename: ev.title || ev.filename || title,
        info_url: ev.link || ev.url || '',
        bedetheque_url: /^https:\/\/www\.bedetheque\.com\//i.test(bedethequeUrl || '') ? bedethequeUrl : null,
    };
    // De quoi télécharger plus tard, depuis la page de validation, sans repartir d'une recherche.
    if (ev.type === 'rss') {
        const direct = (ev.download_links || []).find(item => item.url);
        if (direct) candidate.link = direct.url;
    } else if (ev.type === 'telegram') {
        Object.assign(candidate, { channel: ev.channel, message_id: ev.message_id, channel_title: ev.channel_title || '' });
    } else if (ev.type === 'ebdz') {
        const first = linkIndex != null ? ev.links?.[linkIndex]
            : (ev.links || []).find(item => item.already_owned !== true && item.link);
        if (first?.link) {
            candidate.link = first.link;
            candidate.filename = decodeFilename(first.filename) || candidate.filename;
        }
    }
    const response = await fetch('/api/auto-acquire/reviews/series-match', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ series_title: title, candidates: [candidate], reason })
    });
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || 'Mise en validation impossible');
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

// Vérifier le fichier cible avant de lancer un remplacement explicite : si la fiche
// n'a plus de fichier, ne pas convertir ce clic en simple ajout par inadvertance.
async function _nouveautesReplacementVolumeId(seriesId, filename, button) {
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    try {
        const response = await fetch('/api/import/replacement-required', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ series_id: seriesId, title: filename })
        });
        const data = await response.json();
        if (!response.ok || !data.success || !data.has_file || !data.volume_id) {
            throw new Error('Fichier existant introuvable pour ce tome');
        }
        return data.volume_id;
    } catch (error) {
        showToast('nouveautes-replace-error', error.message,
            { icon: 'circle-x', autoHideMs: 6000 });
        return null;
    } finally {
        button.innerHTML = original;
        button.disabled = false;
    }
}

// Un fichier possédé garde sa série comme destination et demande son remplacement.
async function replaceNouveautesEbdzLink(eventIndex, linkIndex, button) {
    const event = allNouveautesEvents[eventIndex];
    const link = event?.links?.[linkIndex];
    if (!event?.matched_series_id || link?.already_owned !== true) return;
    const filename = decodeFilename(link.filename);
    const volumeId = await _nouveautesReplacementVolumeId(event.matched_series_id, filename, button);
    if (!volumeId) return;
    const volumeMatch = String(link.parsed_volume || '').match(/^Tome\s+(\d+)$/i);
    addToEmule(link.link, button, filename, event.matched_series_id,
        volumeId, link.volume ?? (volumeMatch ? Number(volumeMatch[1]) : null), 'ebdz', event.url || '', true);
}

async function replaceNouveautesTelegramFile(eventIndex, button) {
    const event = allNouveautesEvents[eventIndex];
    if (!event?.series_id || event.already_owned !== true) return;
    const volumeId = await _nouveautesReplacementVolumeId(event.series_id, event.filename || '', button);
    if (!volumeId) return;
    downloadTelegramFile(event.channel, event.message_id, button, event.channel_title || '',
        event.filename || '', event.series_id, volumeId, event.volume ?? null,
        `https://t.me/${String(event.channel || '').replace(/^@/, '')}/${event.message_id}`, true);
}

async function replaceNouveautesRssFile(eventIndex, button) {
    const event = allNouveautesEvents[eventIndex];
    if (!event?.series_id || event.already_owned !== true) return;
    const volumeId = await _nouveautesReplacementVolumeId(event.series_id, event.title || '', button);
    if (!volumeId) return;

    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    try {
        if (event.vk) {
            await _downloadVkNouveaute(event, event.series_id, volumeId, true);
            button.disabled = false;
            button.innerHTML = original;
            showToast('nouveautes-replace-vk', `📥 ${event.title} : téléchargement VK démarré`, { icon: 'download', autoHideMs: 5000 });
            return;
        }
        const torrentUrl = await _resolveRssTorrentUrl(event);
        if (!torrentUrl) throw new Error('Lien de téléchargement introuvable pour cette release');
        button.disabled = false;
        button.innerHTML = original;
        await addTorrentToQbittorrent(torrentUrl, button, event.title, event.series_id,
            volumeId, null, event.link || '', true);
    } catch (error) {
        button.disabled = false;
        button.innerHTML = original;
        showToast('nouveautes-replace-error', error.message,
            { icon: 'circle-x', autoHideMs: 6000 });
    }
}

// Lien de téléchargement d'une release RSS: lien direct du flux s'il y en a un, sinon la release
// exacte est retrouvée chez Prowlarr au clic (le flux RSS retire les liens contenant une clé API).
async function _resolveRssTorrentUrl(event) {
    const direct = (event.download_links || []).find(item => {
        const url = String(item.url || '').toLowerCase();
        return url.startsWith('magnet:') ||
            (url.startsWith('http') && (/\.torrent(?:[?#]|$)/.test(url) || url.includes('/download')));
    });
    let torrentUrl = direct?.url;
    if (!torrentUrl && event.prowlarr_indexer_id) {
        const query = event.series_title || event.title;
        const response = await fetch('/api/search/prowlarr?q=' + encodeURIComponent(query));
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error('Recherche Prowlarr indisponible');
        const exact = (data.results || []).filter(result =>
            String(result.title || '').toLowerCase() === String(event.title || '').toLowerCase());
        const release = event.link
            ? exact.find(result => result.info_url === event.link)
            : exact.length === 1 ? exact[0] : null;
        torrentUrl = release?.download_url;
    }
    if (!torrentUrl) throw new Error('Lien de téléchargement introuvable pour cette release');
    return torrentUrl;
}

// Envoie un lien torrent au premier client activé (qBittorrent, puis rTorrent, puis Deluge),
// en rattachant le téléchargement à la série. Sans alerte ni fenêtre: l'erreur remonte à l'appelant.
async function _sendRssTorrentToClient(torrentUrl, event, seriesId) {
    const clients = typeof enabledDownloadClients === 'undefined' ? {} : enabledDownloadClients;
    const client = ['qbittorrent', 'rtorrent', 'deluge'].find(name => clients[name]);
    if (!client) throw new Error('Aucun client torrent activé');
    const payload = {
        torrent_url: torrentUrl, title: event.title, series_id: seriesId,
        volume_id: null, volume_number: null, source: 'prowlarr', source_link: event.link || '', force_replace: false,
    };
    if (client === 'qbittorrent') {
        try {
            const config = await (await fetch('/api/qbittorrent/config')).json();
            if (config.default_category) payload.category = config.default_category;
        } catch (e) { /* catégorie par défaut optionnelle */ }
    }
    const response = await fetch(`/api/${client}/add`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || `Ajout à ${client} impossible`);
    return client;
}

// Fichier du forum VK : téléchargé par Bullarr lui-même (répertoire d'import VK).
async function _downloadVkNouveaute(ev, seriesId, volumeId = null, forceReplace = false) {
    const url = (ev.download_links || [])[0]?.url;
    if (!url) throw new Error('Lien VK introuvable pour ce fichier');
    const response = await fetch('/api/vk/download', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            url, filename: ev.filename || ev.title || '', series_id: seriesId, volume_id: volumeId,
            volume_number: null, source_link: ev.link || '', force_replace: forceReplace
        })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.error || `Téléchargement VK impossible (HTTP ${response.status})`);
    return 'VK';
}

async function downloadVkNouveaute(eventIndex, button) {
    const ev = allNouveautesEvents[eventIndex];
    if (!ev) return;
    button.disabled = true;
    try {
        await _downloadVkNouveaute(ev, ev.series_id || null);
        button.classList.add('add-button-added');
        button.innerHTML = svgIcon('check');
        showToast('vk-dl-' + eventIndex, `📥 ${ev.title} : téléchargement VK démarré - voir la page Import`,
            { icon: 'download', autoHideMs: 5000 });
    } catch (error) {
        button.disabled = false;
        showToast('vk-dl-error-' + eventIndex, error.message, { icon: 'circle-x', autoHideMs: 8000 });
    }
}

// Télécharge le(s) fichier(s) de la ligne (et uniquement ceux-là, sans recherche) vers le client
// adapté à sa source. Retourne le libellé de la destination pour le message affiché.
async function _downloadNouveautesRelease(ev, seriesId, linkIndex = null) {
    if (ev.type === 'rss' && ev.vk) return await _downloadVkNouveaute(ev, seriesId);
    if (ev.type === 'telegram') {
        const response = await fetch('/api/telegram-channels/download', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel: ev.channel, message_id: ev.message_id, channel_title: ev.channel_title || '',
                filename: ev.filename || '', series_id: seriesId, volume_id: null, volume_number: ev.volume ?? null,
                source: 'telegram', source_link: `https://t.me/${String(ev.channel || '').replace(/^@/, '')}/${ev.message_id}`,
                force_replace: false
            })
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Téléchargement Telegram impossible');
        return 'Telegram';
    }
    if (ev.type === 'ebdz') {
        // Un sujet EBDZ peut regrouper plusieurs fichiers: seuls ceux qui ne sont pas déjà
        // possédés partent, un seul par tome quand plusieurs liens portent le même numéro.
        const seen = new Set();
        const pool = linkIndex != null ? [ev.links[linkIndex]].filter(Boolean) : (ev.links || []);
        const links = pool.filter(link => {
            if (link.already_owned === true) return false;
            const key = link.parsed_volume || link.volume;
            if (key != null && key !== '') {
                if (seen.has(String(key))) return false;
                seen.add(String(key));
            }
            return true;
        });
        if (!links.length) throw new Error('Aucun fichier à télécharger : tout est déjà possédé');
        for (const link of links) {
            const response = await fetch('/api/emule/add', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    link: link.link, title: decodeFilename(link.filename), series_id: seriesId, volume_id: null,
                    volume_number: link.volume ?? null, source: 'ebdz', source_link: ev.url || '', force_replace: false
                })
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'Ajout à aMule impossible');
        }
        return links.length > 1 ? `aMule (${links.length} fichiers)` : 'aMule';
    }
    const torrentUrl = await _resolveRssTorrentUrl(ev);
    return await _sendRssTorrentToClient(torrentUrl, ev, seriesId);
}

// Éclair de Nouveautés (EBDZ, Telegram et RSS): ajoute la série déjà matchée (sans lancer la
// recherche de tous ses tomes) puis télécharge le fichier de CETTE ligne. Sans fiche
// Bédéthèque confirmée, la ligne est envoyée en validation.
async function addSeriesAndDownloadNouveautes(index, button, linkIndex = null) {
    const ev = allNouveautesEvents[index];
    // Ligne EBDZ = un fichier du sujet: l'éclair ne concerne que ce fichier.
    const target = linkIndex != null ? ev?.links?.[linkIndex] : ev;
    if (!ev || !target || target._autoAddTriggered) return;
    const title = ev.type === 'telegram'
        ? (ev.series_title || ev.bedetheque_title || ev.parsed_title || ev.filename)
        : (ev.series_title || ev.matched_series_title || ev.bedetheque_title || ev.title);
    const rawHint = ev.type === 'telegram' ? ev.filename
        : ev.type === 'ebdz' ? ((linkIndex != null ? target : ev.links?.[0])?.filename ? decodeFilename((linkIndex != null ? target : ev.links[0]).filename) : ev.title)
        : ev.title;
    const bedethequeUrl = (ev.type === 'ebdz' ? ev.matched_bedetheque_url : ev.bedetheque_url) || ev.bedetheque_url;
    let seriesId = ev.type === 'ebdz' ? ev.matched_series_id : ev.series_id;
    const seriesWasKnown = !!seriesId;
    const original = button.innerHTML;
    target._autoAddTriggered = true;
    button.disabled = true;
    button.innerHTML = svgIcon('loader-circle', 'icon-spin');
    showToast('nouveautes-auto-add', `« ${title} » : ${seriesWasKnown ? 'envoi du téléchargement' : 'ajout de la série et téléchargement'} en cours…`, { icon: 'loader-circle' });
    const restore = () => { target._autoAddTriggered = false; button.disabled = false; button.innerHTML = original; };
    if (!seriesId) {
        try {
            seriesId = (await _resolveSeriesForAutoAdd(title, rawHint, bedethequeUrl, true)).seriesId;
        } catch (error) {
            let reviewQueued = false;
            try {
                await _queueNouveautesSeriesReview(ev, title, bedethequeUrl, error.message, linkIndex);
                reviewQueued = true;
            } catch (reviewError) {
                console.error('Validation de série impossible', reviewError);
            }
            showToast('nouveautes-auto-add', `« ${title} » : ${error.message}${reviewQueued ? ' Envoyé en validation.' : ''}`,
                { icon: 'circle-x', autoHideMs: 9000, ...(reviewQueued ? { href: '/validation' } : {}) });
            restore();
            return;
        }
    }
    try {
        const destination = await _downloadNouveautesRelease(ev, seriesId, linkIndex);
        showToast('nouveautes-auto-add', `« ${title} » : ${seriesWasKnown ? 'téléchargement envoyé à ' : 'série ajoutée, téléchargement envoyé à '}${destination}.`,
            { icon: 'check', autoHideMs: 6000, href: `/series/${seriesId}` });
        button.innerHTML = svgIcon('check');
        button.style.color = 'var(--color-accent)';
    } catch (error) {
        // La série reste ajoutée: seul le téléchargement est à relancer.
        showToast('nouveautes-auto-add', `« ${title} » : ${seriesWasKnown ? '' : 'série ajoutée, mais '}${error.message}`,
            { icon: 'circle-x', autoHideMs: 9000, href: `/series/${seriesId}` });
        restore();
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

let allNouveautesEvents = [];
let currentNouveautesFilter = 'all'; // 'all' | 'ebdz' | 'telegram' | 'rss'
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
    return e.type === 'ebdz' ? (e.category || '—') : e.type === 'rss' ? (e.feed_title || e.feed_name || 'RSS') : (e.channel_title || e.channel || '—');
}

const NOUVEAUTES_SOURCE_ICONS = {
    ebdz: ['/static/img/ebdz-logo.png', 'EBDZ'],
    telegram: ['/static/img/telegram-logo.svg', 'Telegram'],
    rss: ['/static/img/rss-logo.svg', 'RSS'],
};

function _nouveautesOriginIconsHtml(types) {
    return ['ebdz', 'telegram', 'rss'].filter(t => types && types.has(t)).map(t => {
        const [src, label] = NOUVEAUTES_SOURCE_ICONS[t];
        return `<img src="${src}" alt="${label}" title="${label}" class="nouveautes-source-icon" style="width:16px; height:16px;">`;
    }).join('');
}

function _nouveautesMatched(e) {
    // matched_series_id est l'identité locale la plus directe. Le fallback couvre
    // les réponses plus anciennes/cache qui ne contiennent que already_matched_thread.
    return e.type === 'ebdz'
        ? (!!e.matched_series_id || !!e.already_matched_thread || !!e.already_in_library)
        : !!e.already_in_library;
}

// Le filtre de la colonne « Série » suit le rattachement de la série à la bibliothèque.
// La possession des fichiers reste indiquée séparément dans le détail EBDZ et les badges.
function _nouveautesOwnership(e) {
    return _nouveautesMatched(e) ? 'owned' : 'missing';
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
        case 'type': return e.type === 'ebdz' ? 'EBDZ' : e.type === 'rss' ? 'RSS' : 'Telegram';
        case 'origin': return _nouveautesOrigin(e).toLowerCase();
        case 'title': return (e.type === 'ebdz' ? e.title : e.type === 'rss' ? e.title : e.filename || '').toLowerCase();
        case 'genre': return ({ bd: 'BD', comics: 'Comics', manga: 'Manga' })[_nouveautesGenreKind(e)] || '~';
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
let nouveautesLoadingOlder = false;
let nouveautesLoadingInitial = false;
// Faux tant que seuls les NOUVEAUTES_INITIAL_LIMIT premiers éléments sont chargés: le reste
// de la fenêtre n'est récupéré qu'à la demande (« Charger plus »), pas automatiquement.
let nouveautesFullWindowLoaded = false;
let nouveautesDisplayedCount = 0;
const NOUVEAUTES_PAGE_SIZE = 30;

let nouveautesPendingFilenames = new Set();

function _nouveautesImportStatus(event) {
    const filenames = event.type === 'ebdz' ? (event.links || []).map(l => l.filename) : [event.filename];
    if (filenames.some(f => nouveautesPendingFilenames.has(f))) return 'pending';
    if ((event.type === 'telegram' || event.type === 'rss') && event.downloaded) return 'downloaded';
    return null;
}

// Crayon « choisir ou changer le match » à côté du nom de la série (colonne Détails),
// matchée ou non - plus dans la colonne de la coche.
function _nouveautesMatchPencilHtml(event) {
    const seriesId = event.type === 'ebdz' ? event.matched_series_id : event.series_id;
    const idArg = seriesId == null ? 'null' : seriesId;
    if (event.type === 'ebdz') {
        return `<a href="#" class="icon-owned" data-tooltip="Choisir ou changer le match EBDZ" onclick="event.stopPropagation(); event.preventDefault(); openEbdzThreadMatchModal('${escapeForAttribute(event.thread_id)}', '${escapeForAttribute(event.title || '')}', ${idArg})">${svgIcon('pencil')}</a>`;
    }
    if (event.type === 'rss') {
        return `<a href="#" class="icon-owned" data-tooltip="Choisir ou changer le match RSS" onclick="event.stopPropagation(); event.preventDefault(); openTelegramMatchOverrideModal('${escapeForAttribute(event.title || '')}', ${idArg}, 'rss', '${escapeForAttribute(event.link || '')}')">${svgIcon('pencil')}</a>`;
    }
    if (event.type === 'telegram') {
        return `<a href="#" class="icon-owned" data-tooltip="Choisir ou changer le match Telegram" onclick="event.stopPropagation(); event.preventDefault(); openTelegramMatchOverrideModal('${escapeForAttribute(event.filename || '')}', ${idArg})">${svgIcon('pencil')}</a>`;
    }
    return '';
}

function _nouveautesSeriesHtml(event) {
    // Série locale rattachée, sinon fiche du catalogue Bédéthèque (bedetheque_title/url,
    // même repli pour RSS, EBDZ et Telegram).
    const seriesTitle = (event.type === 'ebdz' ? event.matched_series_title : event.series_title) || event.bedetheque_title;
    const bedethequeUrl = (event.type === 'ebdz' ? event.matched_bedetheque_url : event.bedetheque_url) || event.bedetheque_url;
    const pencilHtml = _nouveautesMatchPencilHtml(event);
    if (!seriesTitle && !bedethequeUrl) {
        return pencilHtml ? `<div class="nouveautes-matched-series">${pencilHtml}</div>` : '';
    }

    // Le lien et son logo restent ensemble dans Détails, y compris lorsqu'une source
    // fournit une fiche Bédéthèque avant le rattachement à une série locale.
    const label = seriesTitle || 'Bédéthèque';
    const content = bedethequeUrl
        ? `<a href="${escapeHtml(bedethequeUrl)}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir la série sur Bédéthèque" onclick="event.stopPropagation()"><img src="/static/img/bedetheque-logo.png" alt="" style="width:14px; height:14px; vertical-align:-2px; margin-right:4px;">${escapeHtml(label)}</a>`
        : escapeHtml(label);
    return `<div class="nouveautes-matched-series" style="display:flex; align-items:center; gap:6px;">${content}${pencilHtml}</div>`;
}

// Colonne Genre (BD / Comics / Manga) : origine de la fiche Bédéthèque de la série, lue par
// POST /api/bedetheque/origins (bibliothèque, puis cache, puis page Bédéthèque - quelques
// fiches par appel, le serveur répond « pending » tant qu'il en reste). Gardée en mémoire
// pour la page ; le filtre Genre charge les origines de toutes les lignes, pas seulement des
// lignes affichées.
const _nouveautesOriginCache = {};
let _nouveautesOriginRun = null;

// Genres RSS à voir (Configuration > Indexeurs > Flux RSS) : une ligne RSS dont le genre
// est connu et décoché est masquée ; genre inconnu (pas de série Bédéthèque) : visible.
let nouveautesRssGenres = new Set(['bd', 'comics', 'manga']);

function _nouveautesRssGenreHidden(event) {
    if (event.type !== 'rss' || nouveautesRssGenres.size >= 3) return false;
    const kind = _nouveautesGenreKind(event);
    return !!kind && !nouveautesRssGenres.has(kind);
}

function _nouveautesBedethequeUrl(event) {
    return (event.type === 'ebdz' ? event.matched_bedetheque_url : event.bedetheque_url) || event.bedetheque_url || '';
}

function _nouveautesGenreKind(event) {
    const url = _nouveautesBedethequeUrl(event);
    return (url && _nouveautesOriginCache[url]?.kind) || null;
}

function _nouveautesGenreCellHtml(event) {
    const url = _nouveautesBedethequeUrl(event);
    const known = url ? _nouveautesOriginCache[url] : null;
    return `<td class="nouveautes-genre-cell" style="padding:10px;">${url
        ? `<span class="nouveautes-origin-slot" data-bdt-url="${escapeHtml(url)}">${known ? bedethequeOriginHtml(known.origin) : ''}</span>`
        : ''}</td>`;
}

function _fillNouveautesGenreSlots() {
    document.querySelectorAll('#nouveautes-events-body .nouveautes-origin-slot[data-bdt-url]').forEach(slot => {
        const known = _nouveautesOriginCache[slot.dataset.bdtUrl];
        if (known && !slot.innerHTML) slot.innerHTML = bedethequeOriginHtml(known.origin);
    });
}

// Retourne true si de nouvelles origines ont été lues.
async function loadNouveautesOrigins(urls) {
    if (_nouveautesOriginRun) await _nouveautesOriginRun;
    const wanted = [...new Set((urls || []).filter(Boolean))];
    let missing = wanted.filter(url => !(url in _nouveautesOriginCache));
    if (!missing.length) return false;
    _nouveautesOriginRun = (async () => {
        for (let round = 0; round < 60 && missing.length; round++) {
            try {
                const response = await fetch('/api/bedetheque/origins', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ urls: missing.slice(0, 100) })
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) return;
                const answered = Object.entries(data.origins || {});
                answered.forEach(([url, value]) => { _nouveautesOriginCache[url] = value; });
                _fillNouveautesGenreSlots();
                // Rien de lu et rien en attente côté serveur : adresses non reconnues, on les écarte.
                if (!answered.length && !data.pending) {
                    missing.slice(0, 100).forEach(url => { _nouveautesOriginCache[url] = { origin: null, kind: null }; });
                }
            } catch (error) {
                console.warn('Origine Bédéthèque indisponible:', error);
                return;
            }
            missing = wanted.filter(url => !(url in _nouveautesOriginCache));
            if (missing.length) await new Promise(resolve => setTimeout(resolve, 300));
        }
    })();
    try { await _nouveautesOriginRun; } finally { _nouveautesOriginRun = null; }
    return true;
}

// Lignes affichées d'abord (drapeaux visibles tout de suite) ; avec un filtre Genre actif,
// toutes les lignes, puis le tableau est redessiné avec les résultats.
async function fillNouveautesOriginFlags() {
    const visibleUrls = [...document.querySelectorAll('#nouveautes-events-body .nouveautes-origin-slot[data-bdt-url]')]
        .map(slot => slot.dataset.bdtUrl);
    const loadedVisible = await loadNouveautesOrigins(visibleUrls);
    // Genres RSS restreints : les origines de toutes les lignes RSS sont nécessaires pour
    // masquer les genres décochés (pas seulement des lignes affichées).
    if (nouveautesRssGenres.size < 3) {
        const loadedRss = await loadNouveautesOrigins(allNouveautesEvents.filter(e => e.type === 'rss').map(_nouveautesBedethequeUrl));
        if ((loadedVisible || loadedRss) && !_nouveautesGenreFilterValue()) renderNouveautesEvents(false);
    }
    if (_nouveautesGenreFilterValue()) {
        // Redessiné seulement si de nouvelles origines ont été lues (sinon boucle sans fin :
        // chaque rendu relance ce remplissage).
        const loaded = await loadNouveautesOrigins(allNouveautesEvents.map(_nouveautesBedethequeUrl));
        if (loaded && _nouveautesGenreFilterValue()) renderNouveautesEvents(false);
    }
}

function _nouveautesGenreFilterValue() {
    return document.getElementById('nouveautesGenreFilter')?.value || '';
}

function _nouveautesMatchedHtml(event) {
    const seriesId = event.type === 'ebdz' ? event.matched_series_id : event.series_id;
    if (!_nouveautesMatched(event)) return '';
    const matchedSeriesTitle = event.type === 'ebdz' ? event.matched_series_title : event.series_title;
    const checkTooltip = matchedSeriesTitle
        ? `Déjà dans votre bibliothèque : ${escapeForAttribute(matchedSeriesTitle)}`
        : 'Déjà dans votre bibliothèque';
    const checkHtml = seriesId
        ? `<a href="/series/${seriesId}" class="icon-owned" data-tooltip="${checkTooltip} - ouvrir la fiche" onclick="event.stopPropagation()">${svgIcon('check')}</a>`
        : `<span class="icon-owned" data-tooltip="${checkTooltip}">${svgIcon('check')}</span>`;
    if (event.type === 'ebdz') {
        const missing = event.missing_links_count || 0;
        if (missing > 0) {
            return `${checkHtml} <span class="badge-missing" data-tooltip="${missing} fichier${missing > 1 ? 's' : ''} de ce sujet manquant${missing > 1 ? 's' : ''} dans votre bibliothèque">${missing}</span>`;
        }
        return checkHtml;
    }
    if (event.already_owned === false) {
        return `${checkHtml} <span class="badge-missing" data-tooltip="Ce fichier n'est pas encore dans votre bibliothèque">manquant</span>`;
    }
    return checkHtml;
}

// "dans nouveautés ca a match une mauvaise serie... changer ca et selectionner
// manuellement la série" côté Telegram (voir changeMatchHtml, _nouveautesMatchedHtml
// ci-dessus) - même principe de liste filtrable que openMergeSeriesModal (library.js),
// réimplémenté ici (cette page ne charge pas library.js, 7500+ lignes, voir
// series-detail.html pour le choix équivalent côté EBDZ) mais listant TOUTES les
// bibliothèques (pas une seule): Nouveautés n'a pas de "série source" dont on connaîtrait
// déjà la bibliothèque, contrairement à une fusion lancée depuis une fiche série précise.
async function openTelegramMatchOverrideModal(filename, currentSeriesId, source = 'telegram', sourceIdentity = null) {
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
                <h2 class="modal-title" style="margin-bottom: 10px;">✏️ Matcher la série</h2>
                <p class="modal-subtitle">Choisissez la série pour <strong>${escapeHtml(filename)}</strong>.</p>
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
                     onclick="confirmTelegramMatchOverride('${escapeForAttribute(filename)}', ${c.id}, '${escapeForAttribute(c.title)}', '${escapeForAttribute(source)}', '${escapeForAttribute(sourceIdentity || '')}')">
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

async function confirmTelegramMatchOverride(filename, seriesId, seriesTitle, source = 'telegram', sourceIdentity = '') {
    const candidatesEl = document.getElementById('telegram-match-override-candidates');
    candidatesEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
        const isRss = source === 'rss';
        const response = await fetch(isRss ? '/api/ebdz/rss/match-override' : '/api/telegram-channels/match-override', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(isRss ? { link: sourceIdentity, title: filename, series_id: seriesId } : { filename, series_id: seriesId })
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

// Modale de rattachement manuel d'un sujet EBDZ depuis Nouveautés. Contrairement
// au raccourci historique vers /series/<id>, elle est également disponible quand le sujet
// n'est associé à aucune série locale.
async function openEbdzThreadMatchModal(threadId, threadTitle, currentSeriesId) {
    let modal = document.getElementById('ebdz-thread-match-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'ebdz-thread-match-modal';
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = '<div class="modal-content"><div class="loading"><div class="spinner"></div></div></div>';
    modal.classList.add('active');
    try {
        const libs = await (await fetch('/api/libraries')).json();
        const perLibrary = await Promise.all((Array.isArray(libs) ? libs : []).map(lib => fetch(`/api/library/${lib.id}/series`).then(r => r.json()).catch(() => [])));
        const candidates = perLibrary.flat();
        modal.innerHTML = `
            <div class="modal-content" style="max-width:700px;">
                <span class="close-modal" onclick="closeEbdzThreadMatchModal()">×</span>
                <h2 class="modal-title" style="margin-bottom:10px;">✏️ Matcher la série</h2>
                <p class="modal-subtitle">Choisissez la série pour <strong>${escapeHtml(threadTitle)}</strong>.</p>
                <input type="text" id="ebdz-thread-match-filter" class="search-box" placeholder="Filtrer les séries..." style="width:100%; margin-bottom:12px;">
                <div id="ebdz-thread-match-candidates" style="max-height:50vh; overflow-y:auto;"></div>
            </div>`;
        const candidatesEl = document.getElementById('ebdz-thread-match-candidates');
        const filterInput = document.getElementById('ebdz-thread-match-filter');
        const renderCandidates = () => {
            const needle = navNormalizeSearch(filterInput.value.trim());
            const matches = candidates.filter(c => navNormalizeSearch(c.title).includes(needle)).slice(0, 50);
            matches.sort((a, b) => Number(b.id === currentSeriesId) - Number(a.id === currentSeriesId));
            candidatesEl.innerHTML = matches.length ? matches.map(c => {
                const isCurrent = c.id === currentSeriesId;
                return `<div class="series-card${isCurrent ? ' series-card-current-match' : ''}" style="cursor:pointer; margin-bottom:8px;" onclick="confirmEbdzThreadMatch('${escapeForAttribute(threadId)}', ${c.id})"><div class="series-title">${escapeHtml(c.title)}${isCurrent ? ' <span class="badge-current-match">✅ Match actuel</span>' : ''}</div><div class="series-info">${c.total_volumes || 0} album${(c.total_volumes || 0) > 1 ? 's' : ''}</div></div>`;
            }).join('') : '<div class="no-data"><p>😕 Aucune série ne correspond</p></div>';
        };
        filterInput.addEventListener('input', renderCandidates);
        renderCandidates();
        filterInput.focus();
    } catch (error) {
        modal.innerHTML = `<div class="modal-content"><span class="close-modal" onclick="closeEbdzThreadMatchModal()">×</span><div class="no-data"><p>❌ ${escapeHtml(error.message)}</p></div></div>`;
    }
}

function closeEbdzThreadMatchModal() {
    document.getElementById('ebdz-thread-match-modal')?.classList.remove('active');
}

async function confirmEbdzThreadMatch(threadId, seriesId) {
    try {
        const response = await fetch(`/api/series/${seriesId}/ebdz-match`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ thread_id: threadId })
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Match impossible');
        closeEbdzThreadMatchModal();
        loadNouveautesEvents(false, false);
    } catch (error) {
        alert('❌ ' + error.message);
    }
}

function _nouveautesImportStatusHtml(event) {
    const status = _nouveautesImportStatus(event);
    if (status === 'pending') {
        return `<span class="icon-owned" style="background:var(--color-accent); color:#fff;" data-tooltip="Déjà téléchargé, en attente d'import">${svgIcon('history')}</span>`;
    }
    if (status === 'downloaded') {
        return `<span class="icon-owned" data-tooltip="Déjà téléchargé">${svgIcon('check-check')}</span>`;
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
    document.querySelectorAll('#nouveautes-filter-all, #nouveautes-filter-ebdz, #nouveautes-filter-telegram, #nouveautes-filter-rss')
        .forEach(btn => btn.classList.remove('history-filter-active'));
    document.getElementById('nouveautes-filter-' + type).classList.add('history-filter-active');
    renderNouveautesEvents();
}

// Sujet EBDZ déplié/replié (données déjà en mémoire dans l'événement lui-même, voir
// loadNouveautesEvents) - même principe que toggleImportFiles côté Historique mais sans
// fetch, la liste des fichiers d'un sujet est déjà connue dès le scrape.
// Colonne « Match » d'une ligne EBDZ (un fichier du sujet): même rendu que RSS, avec la
// mention « manquant » propre à CE fichier.
function _nouveautesEbdzLinkMatchedHtml(event, link) {
    const seriesId = event.matched_series_id;
    if (!_nouveautesMatched(event)) return '';
    const tooltip = event.matched_series_title
        ? `Déjà dans votre bibliothèque : ${escapeForAttribute(event.matched_series_title)}`
        : 'Déjà dans votre bibliothèque';
    const checkHtml = seriesId
        ? `<a href="/series/${seriesId}" class="icon-owned" data-tooltip="${tooltip} - ouvrir la fiche" onclick="event.stopPropagation()">${svgIcon('check')}</a>`
        : `<span class="icon-owned" data-tooltip="${tooltip}">${svgIcon('check')}</span>`;
    const missingHtml = link.already_owned === false
        ? ` <span class="badge-missing" data-tooltip="Ce fichier n'est pas encore dans votre bibliothèque">manquant</span>`
        : '';
    return `${checkHtml}${missingHtml}`;
}

// « que ce soit rss, telegram ou ebdz je voudrais que les lignes soient identiques » - un
// sujet EBDZ n'est plus une ligne repliable: chacun de ses fichiers a sa propre ligne, mise
// en page comme une ligne RSS (titre, série Bédéthèque, taille/tome, actions du fichier).
function _nouveautesEbdzRowHtml(event, index) {
    const safeThreadTitle = escapeHtml(event.title || '');
    const typeIconHtml = event.url
        ? `<a href="${escapeHtml(event.url)}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir « ${safeThreadTitle} » sur EBDZ" onclick="event.stopPropagation()"><img src="/static/img/ebdz-logo.png" alt="EBDZ" class="nouveautes-source-icon"></a>`
        : `<span data-tooltip="EBDZ"><img src="/static/img/ebdz-logo.png" alt="EBDZ" class="nouveautes-source-icon"></span>`;
    const seriesMatched = _nouveautesMatched(event);
    const newRowClass = _nouveautesIsNew(event) ? ' nouveautes-row-new' : '';
    const monitoredHtml = event.already_monitored ? `<span class="badge-monitored" data-tooltip="Cette série fait partie de vos albums surveillés">📊</span>` : '';
    return (event.links || []).map((link, linkIndex) => {
        const filename = decodeFilename(link.filename) || event.title || '';
        const owned = link.already_owned === true;
        const ownedRowClass = owned ? ' volume-table-row-owned' : '';
        const volumeLabel = link.parsed_volume ? String(link.parsed_volume) : '';
        const pendingHtml = nouveautesPendingFilenames.has(link.filename)
            ? `<span class="icon-owned" style="background:var(--color-accent); color:#fff;" data-tooltip="Déjà téléchargé, en attente d'import">${svgIcon('history')}</span>`
            : '';
        const downloadHtml = event.matched_series_id && owned
            ? `<button class="btn-icon-only" type="button" onclick="replaceNouveautesEbdzLink(${index}, ${linkIndex}, this)" data-tooltip="Télécharger et remplacer ce fichier" aria-label="Télécharger et remplacer ce fichier">${svgIcon('file-replace')}</button>`
            : `<button class="btn-icon-only result-action-emule" type="button" onclick="addToEmuleFromNouveautesLink('${escapeForAttribute(link.link)}', this, '${escapeForAttribute(filename)}', allNouveautesEvents[${index}])" data-tooltip="Ajouter à eMule/aMule" aria-label="Ajouter à eMule/aMule"><img src="/static/img/emule-logo.svg" alt="" class="torrent-client-logo"></button>`;
        const zapHtml = owned || link._autoAddTriggered ? ''
            : `<button class="btn-icon-only" type="button" onclick="addSeriesAndDownloadNouveautes(${index}, this, ${linkIndex})" data-tooltip="Ajouter la série (ou l'envoyer en validation si elle n'est pas reconnue) et télécharger ce fichier" aria-label="Ajouter la série et télécharger ce fichier">${svgIcon('zap')}</button>`;
        const discoverHtml = seriesMatched ? ''
            : `<button class="btn-icon-only" type="button" onclick="window.location.href='/discover?q=' + encodeURIComponent('${escapeForAttribute(event.title)}')" data-tooltip="Ajouter la série dans Découvrir" aria-label="Ajouter la série">${svgIcon('plus')}</button>`;
        return `<tr class="nouveautes-event-row${(ownedRowClass + newRowClass)}" style="border-bottom:1px solid var(--color-border);">
            <td style="padding:10px; white-space:nowrap;" data-tooltip="${escapeHtml(formatSessionDate(event.date))}">${escapeHtml(formatSessionDateShort(event.date))}</td>
            <td style="padding:10px; text-align:center;">${typeIconHtml}</td>
            <td class="nouveautes-origin-cell" style="padding:10px; color:var(--color-text-muted); font-size:0.9em;">${escapeHtml(_nouveautesOrigin(event))}</td>
            <td class="nouveautes-details-cell" style="padding:10px;">
                <div style="font-weight:600; display:flex; align-items:center; gap:6px;">${escapeHtml(filename)}</div>
                ${_nouveautesSeriesHtml(event)}
                <div style="color:var(--color-text-muted); font-size:0.9em;">${safeThreadTitle} · ${formatBytes(link.filesize)}${volumeLabel ? ` · ${escapeHtml(volumeLabel)}` : ''}</div>
            </td>
            ${_nouveautesGenreCellHtml(event)}
            <td style="padding:10px; text-align:center;">${_nouveautesEbdzLinkMatchedHtml(event, link)}</td>
            <td class="nouveautes-actions-cell" style="padding:10px;"><div style="display:flex; align-items:center; gap:8px;">${monitoredHtml}${pendingHtml}<button class="btn-icon-only" type="button" onclick="copyLink('${escapeForAttribute(link.link)}', this)" data-tooltip="Copier le lien ed2k" aria-label="Copier le lien ed2k">${svgIcon('copy')}</button>${downloadHtml}${zapHtml}${discoverHtml}</div></td>
        </tr>`;
    }).join('');
}

async function _addNouveautesRssTorrentToQbittorrent(torrentUrl, button, title, eventIndex, itemIndex, sourceLink) {
    const event = allNouveautesEvents[eventIndex];
    await addTorrentToQbittorrent(torrentUrl, button, title, event?.series_id ?? null, null, null, sourceLink);
    if (button.classList.contains('add-button-added')) {
        const item = event?.download_links?.[itemIndex];
        if (item) {
            item._addedClients = item._addedClients || {};
            item._addedClients.qbittorrent = true;
        }
    }
}

function _nouveautesRssTorrentClientButtons(event, item, itemIndex) {
    const url = item?.url || '';
    const probe = url.toLowerCase();
    const isTorrent = probe.startsWith('magnet:') || probe.startsWith('ed2k://') || /\.torrent(?:$|[?#])/.test(probe) || probe.includes('torrent') || probe.includes('/download');
    if (!isTorrent || typeof enabledDownloadClients === 'undefined') return '';
    const title = escapeForAttribute(event.title || 'RSS torrent');
    const sourceLink = escapeForAttribute(event.link || '');
    const encodedUrl = escapeForAttribute(url);
    const buttons = [];
    if (enabledDownloadClients.qbittorrent && typeof addTorrentToQbittorrent === 'function') {
        const added = !!item?._addedClients?.qbittorrent;
        buttons.push(added
            ? `<button class="btn-icon-only add-button-added" type="button" disabled data-tooltip="Ajouté à qBittorrent" aria-label="Ajouté à qBittorrent"><span class="btn-icon">✓</span> <span style="font-size:0.85em;">Ajouté</span></button>`
            : `<button class="btn-icon-only" type="button" onclick="event.stopPropagation(); _addNouveautesRssTorrentToQbittorrent('${encodedUrl}', this, '${title}', ${event._index}, ${itemIndex}, '${sourceLink}')" data-tooltip="Envoyer à qBittorrent" aria-label="Envoyer à qBittorrent"><img src="/static/img/qbittorrent-logo.svg" alt="qBittorrent" class="torrent-client-logo"></button>`);
    }
    if (enabledDownloadClients.rtorrent && typeof addTorrentToRtorrent === 'function') {
        buttons.push(`<button class="btn-icon-only" type="button" onclick="addTorrentToRtorrent('${encodedUrl}', this, '${title}', null, null, null, '${sourceLink}')" data-tooltip="Envoyer à rTorrent" aria-label="Envoyer à rTorrent"><img src="/static/img/rtorrent-logo.svg" alt="rTorrent" class="torrent-client-logo"></button>`);
    }
    if (enabledDownloadClients.deluge && typeof addTorrentToDeluge === 'function') {
        buttons.push(`<button class="btn-icon-only" type="button" onclick="addTorrentToDeluge('${encodedUrl}', this, '${title}', null, null, null, '${sourceLink}')" data-tooltip="Envoyer à Deluge" aria-label="Envoyer à Deluge"><img src="/static/img/deluge-logo.svg" alt="Deluge" class="torrent-client-logo"></button>`);
    }
    return buttons.join('');
}

// Lien Prowlarr (/<indexeur>/download) gardé sans clé API: passé par le serveur, qui
// ajoute la clé configurée (sinon 401). Les autres liens restent directs.
function _nouveautesRssDownloadLink(item, title) {
    const url = item.url || '';
    if (/^https:\/\/(m\.)?vk\.(com|ru)\/doc-?\d+_\d+/.test(url)) return { href: url, label: 'Ouvrir le fichier sur VK' };
    if (url.startsWith('magnet:')) return { href: url, label: 'Ouvrir le lien magnet' };
    if (url.startsWith('ed2k://')) return { href: url, label: 'Ouvrir le lien ed2k' };
    const label = 'Télécharger le fichier torrent';
    if (/\/\d+\/download(?:\?|$)/.test(url.split('#')[0])) {
        return { href: `/api/qbittorrent/torrent-file?url=${encodeURIComponent(url)}&name=${encodeURIComponent(title || '')}`, label };
    }
    return { href: url, label };
}

function _nouveautesRssRowHtml(event) {
    const escapedTitle = escapeHtml(event.title || 'Sans titre');
    const title = event.link
        ? `<a class="nouveautes-rss-link" href="${escapeHtml(event.link)}" target="_blank" rel="noopener noreferrer" data-tooltip="Ouvrir la page liée" onclick="event.stopPropagation()">${escapedTitle} ${svgIcon('external-link')}</a>`
        : escapedTitle;
    // DupeFR fournit une vignette HTML dans description ; ne pas l’afficher comme du texte brut.
    const description = event.description
        ? escapeHtml(event.description.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()).slice(0, 240)
        : '';
    const replaceOwned = event.already_owned === true && !!event.series_id;
    const addButton = event.title ? (replaceOwned
        ? `<button class="btn-icon-only" type="button" onclick="replaceNouveautesRssFile(${event._index}, this)" data-tooltip="Télécharger et remplacer ce fichier" aria-label="Télécharger et remplacer ce fichier">${svgIcon('file-replace')}</button>`
        : `${event._autoAddTriggered ? '' : `<button class="btn-icon-only" type="button" onclick="addSeriesAndDownloadNouveautes(${event._index}, this)" data-tooltip="Ajouter la série (ou l'envoyer en validation si elle n'est pas reconnue) et télécharger cette release" aria-label="Ajouter la série et télécharger cette release">${svgIcon('zap')}</button>`}${_nouveautesMatched(event) ? '' : `<button class="btn-icon-only" type="button" onclick="window.location.href='/discover?q=' + encodeURIComponent('${escapeForAttribute(event.title)}')" data-tooltip="Ajouter la série dans Découvrir" aria-label="Ajouter la série">${svgIcon('plus')}</button>`}`) : '';
    const matchedSeriesHtml = _nouveautesSeriesHtml(event);
    // Ligne VK : le titre ouvre déjà le message sur VK ; le bouton télécharge dans Bullarr.
    const downloadButtons = event.vk ? '' : (event.download_links || []).map(item => {
        const link = _nouveautesRssDownloadLink(item, event.title);
        return `<a class="btn-icon-only" href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer" data-tooltip="${escapeHtml(link.label)}" aria-label="${escapeHtml(link.label)}" onclick="event.stopPropagation()">${svgIcon('download')}</a>`;
    }).join('');
    const torrentClientButtons = event.vk
        ? `<button class="btn-icon-only" type="button" onclick="downloadVkNouveaute(${event._index}, this)" data-tooltip="Télécharger dans Bullarr (VK)" aria-label="Télécharger dans Bullarr (VK)">${svgIcon('download')}</button>`
        : (event.download_links || []).map((item, itemIndex) => _nouveautesRssTorrentClientButtons(event, item, itemIndex)).join('');
    const rssMonitoredHtml = event.already_monitored ? `<span class="badge-monitored" data-tooltip="Cette série fait partie de vos albums surveillés">📊</span>` : '';
    const rssStatusHtml = `${rssMonitoredHtml}${_nouveautesImportStatusHtml(event)}`;
    return `<tr class="nouveautes-event-row ${_nouveautesIsNew(event) ? 'nouveautes-row-new' : ''}" style="border-bottom:1px solid var(--color-border);"><td style="padding:10px; white-space:nowrap;" data-tooltip="${escapeHtml(formatSessionDate(event.date))}">${escapeHtml(formatSessionDateShort(event.date))}</td><td style="padding:10px; text-align:center;"><img src="/static/img/rss-logo.svg" alt="RSS" class="nouveautes-source-icon" data-tooltip="Flux RSS"></td><td class="nouveautes-origin-cell" style="padding:10px; color:var(--color-text-muted); font-size:0.9em;">${escapeHtml(_nouveautesOrigin(event))}</td><td class="nouveautes-details-cell" style="padding:10px;"><div style="font-weight:600; display:flex; align-items:center; gap:6px;">${title}</div>${matchedSeriesHtml}${description ? `<div style="color:var(--color-text-muted); font-size:0.9em;">${description}</div>` : ''}</td>${_nouveautesGenreCellHtml(event)}<td style="padding:10px; text-align:center;">${_nouveautesMatchedHtml(event)}</td><td class="nouveautes-actions-cell" style="padding:10px;"><div style="display:flex; align-items:center; gap:8px;">${rssStatusHtml}${downloadButtons}${torrentClientButtons}${addButton}</div></td></tr>`;
}

function _nouveautesTelegramRowHtml(event) {
    const safeFilename = escapeHtml(event.filename);
    const ownedRowClass = event.already_owned === true ? ' volume-table-row-owned' : '';
    const telegramPostUrl = event.channel && event.message_id != null
        ? `https://t.me/${encodeURIComponent(String(event.channel).replace(/^@/, ''))}/${event.message_id}`
        : null;
    const telegramIconHtml = telegramPostUrl
        ? `<a href="${escapeHtml(telegramPostUrl)}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir le post Telegram" onclick="event.stopPropagation()"><img src="/static/img/telegram-logo.svg" alt="Telegram" style="width:16px; height:16px; vertical-align:-3px;"></a>`
        : `<span data-tooltip="Telegram"><img src="/static/img/telegram-logo.svg" alt="Telegram" style="width:16px; height:16px; vertical-align:-3px;"></span>`;
    const newRowClass = _nouveautesIsNew(event) ? ' nouveautes-row-new' : '';
    const addToLibraryQuery = event.parsed_title || event.filename;
    const seriesMatched = _nouveautesMatched(event);
    const showReplace = !!event.series_id && event.already_owned === true;
    const addToLibraryHtml = `
        ${showReplace ? `<button class="btn-icon-only" onclick="replaceNouveautesTelegramFile(${event._index}, this)" data-tooltip="Télécharger et remplacer ce fichier" aria-label="Télécharger et remplacer ce fichier">${svgIcon('file-replace')}</button>` : event._autoAddTriggered ? '' : `<button class="btn-icon-only" onclick="addSeriesAndDownloadNouveautes(${event._index}, this)" data-tooltip="Ajouter la série (ou l'envoyer en validation si elle n'est pas reconnue) et télécharger ce fichier">${svgIcon('zap')}</button>`}
        ${seriesMatched ? '' : `<button class="btn-icon-only" onclick="window.location.href='/discover?q=' + encodeURIComponent('${escapeForAttribute(addToLibraryQuery)}')" data-tooltip="Ajouter la série dans Découvrir">${svgIcon('plus')}</button>`}
    `;
    return `
        <tr class="nouveautes-event-row ${(ownedRowClass + newRowClass).trim()}" style="border-bottom:1px solid var(--color-border);">
            <td style="padding:10px; white-space:nowrap;" data-tooltip="${escapeHtml(formatSessionDate(event.date))}">${escapeHtml(formatSessionDateShort(event.date))}</td>
            <td style="padding:10px; white-space:nowrap; text-align:center;">${telegramIconHtml}</td>
            <td class="nouveautes-origin-cell" style="padding:10px; color:var(--color-text-muted); font-size:0.9em;">${escapeHtml(_nouveautesOrigin(event))}</td>
            <td class="nouveautes-details-cell" style="padding:10px;">
                <div style="font-weight:600;">${safeFilename}</div>
                ${_nouveautesSeriesHtml(event)}
                <div style="color:var(--color-text-muted); font-size:0.9em;">
                    ${escapeHtml(event.channel_title || event.channel)} · ${formatBytes(event.file_size)}
                    ${event.parsed_volume ? ` · ${escapeHtml(event.parsed_volume)}` : ''}
                </div>
            </td>
            ${_nouveautesGenreCellHtml(event)}
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
    // Source(s) de chaque origine, pour son icône dans le menu (un même nom peut en
    // théorie venir de deux sources: une icône par source).
    const originTypes = new Map();
    for (const e of eventsForOrigins) {
        const o = _nouveautesOrigin(e);
        if (!originTypes.has(o)) originTypes.set(o, new Set());
        originTypes.get(o).add(e.type);
    }
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
                    style="display:flex; align-items:center; gap:8px;"
                    onclick="toggleNouveautesOriginFilter('${escapeForAttribute(o)}')">${_nouveautesOriginIconsHtml(originTypes.get(o))}${escapeHtml(o)}</button>
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
let _nouveautesScrollSyncReady = false;
function syncNouveautesHorizontalScrollbars() {
    const top = document.getElementById('nouveautes-top-scrollbar');
    const wrapper = document.getElementById('nouveautes-events-wrapper');
    const table = wrapper?.querySelector('.nouveautes-table');
    if (!top || !wrapper || !table) return;
    const width = Math.max(table.scrollWidth, wrapper.clientWidth);
    top.firstElementChild.style.width = `${width}px`;
    top.style.display = table.scrollWidth > wrapper.clientWidth ? 'block' : 'none';
    if (!_nouveautesScrollSyncReady) {
        let syncing = false;
        top.addEventListener('scroll', () => {
            if (syncing) return;
            syncing = true;
            wrapper.scrollLeft = top.scrollLeft;
            syncing = false;
        }, { passive: true });
        wrapper.addEventListener('scroll', () => {
            if (syncing) return;
            syncing = true;
            top.scrollLeft = wrapper.scrollLeft;
            syncing = false;
        }, { passive: true });
        window.addEventListener('resize', syncNouveautesHorizontalScrollbars);
        _nouveautesScrollSyncReady = true;
    }
    top.scrollLeft = wrapper.scrollLeft;
}

function renderNouveautesEvents(resetPage = true) {
    const query = navNormalizeSearch(document.getElementById('nouveautesTitleFilter').value.trim());
    const ownershipFilter = document.getElementById('nouveautesOwnershipFilter')?.value || '';
    const genreFilter = _nouveautesGenreFilterValue();
    _populateNouveautesOriginOptions();

    let filtered = allNouveautesEvents.filter((e, index) => {
        e._index = index;
        if (currentNouveautesFilter !== 'all' && e.type !== currentNouveautesFilter) return false;
        if (ownershipFilter && _nouveautesOwnership(e) !== ownershipFilter) return false;
        if (genreFilter && _nouveautesGenreKind(e) !== genreFilter) return false;
        if (_nouveautesRssGenreHidden(e)) return false;
        if (nouveautesOriginFilters.size && !nouveautesOriginFilters.has(_nouveautesOrigin(e))) return false;
        if (!query) return true;
        const haystack = navNormalizeSearch(e.type === 'ebdz' || e.type === 'rss' ? e.title : e.filename);
        return haystack.includes(query);
    });
    // e._index (position dans allNouveautesEvents, utilisé par les boutons de chaque ligne) reste
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
        empty.style.display = nouveautesLoadingOlder ? 'none' : 'block';
        _renderNouveautesLoadMoreButton(loadMoreDiv, 0);
        syncNouveautesHorizontalScrollbars();
        return;
    }
    empty.style.display = 'none';

    const visible = filtered.slice(0, nouveautesDisplayedCount);
    body.innerHTML = visible.map(e => e.type === 'ebdz' ? _nouveautesEbdzRowHtml(e, e._index) : e.type === 'rss' ? _nouveautesRssRowHtml(e) : _nouveautesTelegramRowHtml(e)).join('');
    checkEmuleStatus();
    fillNouveautesOriginFlags();

    _renderNouveautesLoadMoreButton(loadMoreDiv, filtered.length - nouveautesDisplayedCount);
    syncNouveautesHorizontalScrollbars();
}

function _renderNouveautesLoadMoreButton(loadMoreDiv, remaining) {
    if (nouveautesLoadingOlder) {
        loadMoreDiv.style.display = 'block';
        loadMoreDiv.innerHTML = `<div id="nouveautes-load-older-spinner" style="display:inline-flex; align-items:center; gap:8px; color:var(--color-text-muted);">${svgIcon('loader-circle', 'icon-spin')} Chargement</div>`;
    } else if (nouveautesLoadingInitial) {
        loadMoreDiv.style.display = 'none';
    } else if (remaining > 0) {
        loadMoreDiv.style.display = 'block';
        loadMoreDiv.innerHTML = `<button class="btn" onclick="loadMoreNouveautesEvents()">${svgIcon('chevron-down')} Charger plus (${remaining} restant${remaining > 1 ? 's' : ''})</button>`;
    } else if (!nouveautesFullWindowLoaded) {
        loadMoreDiv.style.display = 'block';
        loadMoreDiv.innerHTML = `<button class="btn" onclick="loadMoreNouveautesEvents()">${svgIcon('chevron-down')} Charger plus</button>`;
    } else if (nouveautesHasMoreEbdz && currentNouveautesFilter !== 'rss') {
        loadMoreDiv.style.display = 'block';
        loadMoreDiv.innerHTML = `<button class="btn" onclick="loadOlderNouveautesEvents()">${svgIcon('chevron-down')} Charger une période plus ancienne</button>`;
    } else {
        loadMoreDiv.style.display = 'none';
    }
}

async function loadMoreNouveautesEvents() {
    if (!nouveautesFullWindowLoaded && nouveautesDisplayedCount >= allNouveautesEvents.length) {
        // Lot initial épuisé: récupérer maintenant le reste de la fenêtre.
        const loadMoreDiv = document.getElementById('nouveautes-events-load-more');
        nouveautesLoadingOlder = true;
        _renderNouveautesLoadMoreButton(loadMoreDiv, 0);
        try {
            await _loadNouveautesBatch(null, nouveautesLoadGeneration, false);
            nouveautesFullWindowLoaded = true;
        } catch (e) {
            console.error('Chargement complet des nouveautés impossible', e);
        } finally {
            nouveautesLoadingOlder = false;
        }
    }
    nouveautesDisplayedCount += NOUVEAUTES_PAGE_SIZE;
    renderNouveautesEvents(false);
}

async function loadOlderNouveautesEvents() {
    const olderThanDays = nouveautesDaysWindow;
    nouveautesDaysWindow += NOUVEAUTES_WINDOW_STEP;
    const loadMoreDiv = document.getElementById('nouveautes-events-load-more');
    nouveautesLoadingOlder = true;
    // Masquer immédiatement l'état vide avant toute requête. Sinon, lorsque la
    // dernière liste filtrée était vide, « Aucun élément trouvé » reste affiché
    // sous le spinner pendant le chargement de la période suivante.
    const empty = document.getElementById('nouveautes-events-empty');
    if (empty) empty.style.display = 'none';
    loadMoreDiv.style.display = 'block';
    loadMoreDiv.innerHTML = `<div id="nouveautes-load-older-spinner" style="display:inline-flex; align-items:center; gap:8px; color:var(--color-text-muted);">${svgIcon('loader-circle', 'icon-spin')} Chargement</div>`;
    try {
        await loadNouveautesEvents(false, false, olderThanDays);
    } finally {
        nouveautesLoadingOlder = false;
        renderNouveautesEvents(false);
    }
}

// "dans nouveauté il n'y a que 100 entrées pour le premier affichage ca devrait suffire" -
// seul le premier lot (le plus récent, annotation comprise) est chargé; la fenêtre complète
// (nouveautesDaysWindow jours, potentiellement des milliers d'items) n'est récupérée que
// quand l'utilisateur clique « Charger plus » après avoir épuisé ce lot (voir
// loadMoreNouveautesEvents). Elle était auparavant rechargée d'office en arrière-plan, ce
// qui rendait la page très lente (annotation de toute la fenêtre à chaque ouverture).
const NOUVEAUTES_INITIAL_LIMIT = 100;
const NOUVEAUTES_CACHE_KEY = 'bullarr:nouveautes:v1';
const NOUVEAUTES_CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
let nouveautesLoadGeneration = 0;

function _saveNouveautesCache(loadedTypes = ['ebdz', 'telegram', 'rss']) {
    try {
        let cached = null;
        try { cached = JSON.parse(localStorage.getItem(NOUVEAUTES_CACHE_KEY) || 'null'); } catch (e) { cached = null; }
        const eventsByType = {};
        (cached?.events || []).forEach(event => {
            (eventsByType[event.type] ||= []).push(event);
        });
        loadedTypes.forEach(type => {
            eventsByType[type] = allNouveautesEvents.filter(event => event.type === type);
        });
        localStorage.setItem(NOUVEAUTES_CACHE_KEY, JSON.stringify({
            savedAt: Date.now(),
            events: ['ebdz', 'telegram', 'rss'].flatMap(type => eventsByType[type] || []),
        }));
    } catch (e) { /* cache indisponible ou trop volumineux - le réseau reste la source */ }
}

function _restoreNouveautesCache() {
    try {
        const cached = JSON.parse(localStorage.getItem(NOUVEAUTES_CACHE_KEY) || 'null');
        if (!cached || !Array.isArray(cached.events) || !cached.events.length) return false;
        if (Date.now() - Number(cached.savedAt || 0) > NOUVEAUTES_CACHE_MAX_AGE_MS) return false;
        // RSS depends on the current disabled-extension settings; wait for the filtered API response.
        allNouveautesEvents = cached.events.filter(event => event.type !== 'rss');
        if (!allNouveautesEvents.length) return false;
        renderNouveautesEvents(true);
        return true;
    } catch (e) {
        return false;
    }
}

function _mergeNouveautesClientState(events) {
    const addedByUrl = new Map();
    allNouveautesEvents.forEach(event => {
        (event.download_links || []).forEach(item => {
            if (item.url && item._addedClients) addedByUrl.set(item.url, item._addedClients);
        });
    });
    events.forEach(event => {
        (event.download_links || []).forEach(item => {
            const added = item.url && addedByUrl.get(item.url);
            if (added) item._addedClients = added;
        });
    });
}

async function loadNouveautesEvents(forceScrape, resetPage = true, olderThanDays = null) {
    const loading = document.getElementById('nouveautes-events-loading');
    const btn = document.getElementById('nouveautesRescanBtn');

    // Un chargement complet (visite/actualisation) démarre une nouvelle fenêtre de
    // comparaison avec le badge. Le bouton "Charger une période plus ancienne" conserve
    // volontairement le point de référence de la visite en cours.
    if (resetPage) {
        nouveautesLoadingInitial = true;
        document.getElementById('nouveautes-events-load-more').style.display = 'none';
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

    const restoredFromCache = resetPage ? _restoreNouveautesCache() : allNouveautesEvents.length > 0;
    loading.style.display = restoredFromCache ? 'none' : 'block';
    document.getElementById('nouveautes-events-empty').style.display = 'none';
    if (!restoredFromCache) document.getElementById('nouveautes-events-body').innerHTML = '';

    try {
        // pendingScanData: fichiers actuellement dans la file d'import (voir
        // _nouveautesImportStatus) - même endpoint que la page /import elle-même, un seul
        // appel supplémentaire par chargement de la page Nouveautés.
        // Le scan d'import peut parcourir le stockage et ne doit jamais retarder
        // les trois sources de Nouveautés. Il met à jour les marqueurs ensuite.
        if (resetPage) {
            const pendingScanPromise = fetch('/api/import/scan', { method: 'POST' })
                .then(r => r.json()).catch(() => ({ success: false }));
            pendingScanPromise.then(pendingScanData => {
                if (generation !== nouveautesLoadGeneration) return;
                nouveautesPendingFilenames = new Set(
                    pendingScanData.success ? (pendingScanData.files || []).map(f => f.filename) : []
                );
                if (allNouveautesEvents.length) renderNouveautesEvents(false);
            });
        }

        if (resetPage) {
            nouveautesFullWindowLoaded = false;
            await _loadNouveautesBatch(NOUVEAUTES_INITIAL_LIMIT, generation, true, null, false, !!forceScrape);
            loading.style.display = 'none';
            if (generation === nouveautesLoadGeneration) {
                nouveautesLoadingInitial = false;
                renderNouveautesEvents(false);
            }
        } else {
            // Pour une période plus ancienne, le lot limité aux 100 éléments les plus
            // récents ne contient pas les nouveaux éléments demandés. Charger directement
            // la fenêtre complète évite que le bouton semble ne rien faire avant le rendu
            // du second appel en arrière-plan.
            await _loadNouveautesBatch(null, generation, false, olderThanDays, true);
            nouveautesFullWindowLoaded = true;
            loading.style.display = 'none';
        }
    } catch (error) {
        if (generation === nouveautesLoadGeneration) nouveautesLoadingInitial = false;
        loading.style.display = 'none';
        document.getElementById('nouveautes-events-body').innerHTML =
            `<tr><td colspan="7" style="padding:20px; text-align:center; color:#c0392b;">Erreur: ${escapeHtml(error.message)}</td></tr>`;
    }
}

function _nouveautesEventKey(event) {
    if (event.type === 'telegram') return `telegram:${event.channel || ''}:${event.message_id || ''}`;
    if (event.type === 'rss') return `rss:${event.link || event.title || ''}`;
    return `ebdz:${event.thread_id || event.url || event.title || ''}:${event.date || ''}`;
}

async function _loadNouveautesBatch(limit, generation, resetPage, olderThanDays = null, appendEvents = false, refreshRss = false) {
    const limitParam = limit ? `&limit=${limit}` : '';
    const olderRangeParam = olderThanDays == null ? '' : `&older_than_days=${olderThanDays}`;

    // Garder les lignes visibles jusqu'à la réponse des trois sources. Un rendu par
    // source fait temporairement disparaître les autres et déplace les lignes RSS.
    let ebdzEvents = allNouveautesEvents.filter(event => event.type === 'ebdz');
    let telegramEvents = allNouveautesEvents.filter(event => event.type === 'telegram');
    let rssEvents = allNouveautesEvents.filter(event => event.type === 'rss');

    const renderBatch = () => {
        if (generation !== nouveautesLoadGeneration) return;
        const refreshedEvents = [...ebdzEvents, ...telegramEvents, ...rssEvents]
            .sort((a, b) => new Date(b.date) - new Date(a.date));
        _mergeNouveautesClientState(refreshedEvents);
        if (appendEvents) {
            const byKey = new Map(allNouveautesEvents.map(event => [_nouveautesEventKey(event), event]));
            refreshedEvents.forEach(event => byKey.set(_nouveautesEventKey(event), event));
            allNouveautesEvents = [...byKey.values()].sort((a, b) => new Date(b.date) - new Date(a.date));
        } else {
            allNouveautesEvents = refreshedEvents;
        }
        renderNouveautesEvents(resetPage);
        _saveNouveautesCache();
    };

    const ebdzPromise = fetch(`/api/ebdz/latest?days=${nouveautesDaysWindow}${olderRangeParam}${limitParam}`).then(r => r.json()).catch(() => ({ success: false })).then(ebdzData => {
        if (generation !== nouveautesLoadGeneration) return;
        if (ebdzData.success && ebdzData.sessions) {
            ebdzEvents = [];
            ebdzData.sessions.forEach(session => {
                session.results.forEach(thread => {
                    ebdzEvents.push({ ...thread, type: 'ebdz', date: session.scraped_at });
                });
            });
        }
        if (ebdzData.success) nouveautesHasMoreEbdz = !!ebdzData.has_more;
    });

    const telegramPromise = fetch(`/api/telegram-channels/latest?days=${nouveautesDaysWindow}${olderRangeParam}${limitParam}`).then(r => r.json()).catch(() => ({ success: false })).then(telegramData => {
        if (generation !== nouveautesLoadGeneration) return;
        if (telegramData.success) telegramEvents = (telegramData.files || []).map(f => ({
            ...f, type: 'telegram', date: f.message_date, downloaded: !!f.downloaded,
        }));
    });

    const rssPromise = olderThanDays != null
        ? Promise.resolve()
        : fetch(`/api/ebdz/rss/latest?limit=${limit || 100}${refreshRss ? '&refresh=1' : ''}`)
            .then(r => { if (!r.ok) throw new Error('RSS indisponible'); return r.json(); })
            .catch(() => ({ success: false }))
            .then(rssData => {
                if (generation !== nouveautesLoadGeneration) return;
                if (rssData.success) {
                    rssEvents = (rssData.entries || []).map(entry => ({ type: 'rss', ...entry }));
                    nouveautesRssGenres = new Set(rssData.genres && rssData.genres.length ? rssData.genres : ['bd', 'comics', 'manga']);
                }
                if (!rssData.success || rssData.errors?.length) {
                    const names = (rssData.errors || []).map(error => error.name).join(', ');
                    showToast('nouveautes-rss-error', `RSS${names ? ' (' + names + ')' : ''} : actualisation impossible. Dernières données disponibles affichées.`, { icon: 'triangle-alert', autoHideMs: 10000 });
                } else {
                    dismissToast('nouveautes-rss-error');
                }
            });

    await Promise.all([ebdzPromise, telegramPromise, rssPromise]);
    if (generation !== nouveautesLoadGeneration) return;
    renderBatch();

    try {
        localStorage.setItem('nouveautesLastSeenAt', new Date().toISOString());
    } catch (e) { /* localStorage indisponible (navigation privée...) - pas bloquant */ }
}

window.addEventListener('download-clients-ready', () => {
    if (allNouveautesEvents.length) renderNouveautesEvents(false);
});

loadNouveautesEvents();
