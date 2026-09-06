// ===== TABLEAU COMPACT DE RÉSULTATS DE RECHERCHE (EBDZ + Prowlarr) =====
// Partagé entre la modale de recherche/remplacement d'un tome (static/js/library.js,
// series-detail.html) et la page Recherche (static/js/search.js, search.html) - même
// tableau trié par pertinence des deux côtés plutôt que deux affichages différents pour
// la même donnée. Dépend de fonctions utilitaires déjà définies indépendamment par
// chaque page qui charge ce script (escapeHtml, escapeForAttribute, formatBytes,
// decodeFilename, copyLink, addToEmule, addTorrentToQbittorrent) - ne pas les redéclarer
// ici pour éviter les conflits de nom.

// "ajouter un fihceir a shelfmark ca ne met pas ajouté" - bug réel: `result` ici vient du
// JSON sérialisé dans l'onclick (encodeURIComponent(JSON.stringify(...)), voir
// buildSearchResultRowHtml), donc une COPIE indépendante de l'objet réel - le marquer
// "ajouté" (result._addedClients.shelfmark = true) ne touchait que cette copie jetable,
// jamais l'objet vivant dans _searchTableAllResults que _renderSearchResultsTbody relit
// pour reconstruire chaque ligne. _markShelfmarkResultAdded (comme _markSearchResultAdded/
// _markTelegramResultAdded pour les autres clients) retrouve et mute le VRAI objet par
// md5, seul identifiant fiable ici (Anna's Archive n'a pas de download_url/link).
// "le toast aussi c'est success. retire ca" - {icon: 'success'} n'est pas une clé
// ICON_PATHS valide (icons.js): _renderToastElement retombe alors sur `iconEl.textContent
// = icon`, affichant littéralement le mot "success" au lieu d'une icône - 'check' (déjà
// utilisé par les autres toasts de succès de cette page) corrige ça.
async function downloadViaShelfmark(button, serializedResult) {
    let result;
    try { result = JSON.parse(decodeURIComponent(serializedResult)); } catch (_) { result = null; }
    if (!result || !result.md5) {
        showToast('shelfmark-missing-id', 'Identifiant Anna manquant pour Shelfmark', { icon: 'warning', autoHideMs: 4000 });
        return;
    }
    button.disabled = true;
    try {
        const response = await fetch('/api/annas-archive/shelfmark-download', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({md5: result.md5, title: result.title || result.filename, size: result.size, info_url: result.info_url, series_id: result.series_id, volume_id: result.volume_id, volume_number: result.volume_number, force_replace: !!result.force_replace})
        });
        const data = await response.json();
        if (!response.ok || !data.success) throw new Error(data.error || 'Échec Shelfmark');
        _markShelfmarkResultAdded(result.md5);
        if (typeof _renderSearchResultsTbody === 'function') _renderSearchResultsTbody();
        showToast('shelfmark-download', 'Téléchargement envoyé à Shelfmark', { icon: 'check', autoHideMs: 5000 });
    } catch (error) {
        button.disabled = false;
        showToast('shelfmark-download-error', error.message, { icon: 'warning', autoHideMs: 6000 });
    }
}

// _syncFilterControlActive (pose/retire .has-value sur un contrôle de filtre) vit
// maintenant dans nav.js - chargé littéralement sur toutes les pages, contrairement à ce
// fichier (absent de /bedetheque-enrich par ex.), donc plus approprié pour un helper
// partagé par tout tableau filtrable de l'app.

// Point d'entrée UNIQUE pour chercher une source (EBDZ/Prowlarr/Telegram) pour une série
// déjà identifiée - "same function same code... check that the search is the same for
// every function that do a search in the app": la fiche série (searchMissingVolume,
// library.js) et Découvrir (searchSources, discover.js) recherchent toutes les deux des
// sources pour UNE série déjà connue, exactement le même besoin. Avant ce partage,
// Découvrir avait sa propre implémentation (endpoints /api/search, /api/search/prowlarr,
// /api/telegram-channels/search + fonctions de normalisation dédiées) qui a fini par
// diverger du comportement de la fiche série pour Telegram: /api/missing-monitor/search
// interroge l'API Telegram EN DIRECT (insensible à l'ordre des mots), alors que
// /api/telegram-channels/search cherche une base locale pré-indexée en phrase EXACTE -
// une série ("Les chats en BD" en local vs "Chats En BD (Les)" côté release Telegram/
// EBDZ, article déplacé en fin de titre) trouvée d'un côté et introuvable de l'autre pour
// la même recherche. Définie ici (pas dans library.js) car c'est le seul fichier chargé
// par les deux pages (bibliothèque/fiche série ET Découvrir).
//
// La page Recherche (search.js) reste volontairement à part: elle sert une recherche
// libre par texte sans série identifiée (parcourir le contenu indexé), un besoin
// différent d'une confirmation de tome pour une série déjà connue - /api/missing-
// monitor/search suppose un titre de série et applique une logique de confirmation de
// volume qui n'a pas de sens pour ce cas-là.
async function searchMissingVolumeSource(source, seriesTitle, volumeNumber, seriesId, isIntegral, isHs, isEpisode) {
    try {
        const response = await fetch('/api/missing-monitor/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: seriesTitle, volume_num: volumeNumber, sources: [source],
                series_id: seriesId, is_integral: isIntegral, is_hs: isHs, is_episode: isEpisode
            })
        });
        const data = await response.json();
        return data.results || [];
    } catch (error) {
        console.warn(`Erreur recherche ${source}:`, error);
        return [];
    }
}

// "recherche et recherche automatique de discover et de la page album doit être la même...
// je ne veux pas de code en double pour rien" - POINT D'ENTRÉE UNIQUE pour "recherche
// automatique" (cherche ET télécharge directement, sans revue manuelle des résultats,
// contrairement à searchMissingVolumeSource ci-dessus) sur une série entière, un tome
// précis, ou un one-shot. Remplace 4 fonctions quasi-identiques qui dupliquaient chacune
// le même fetch + gestion d'erreur + toast: runAutoAcquireNowForSeries/ForVolume/ForOneshot
// (library.js, fiche série) et runAutoAcquireNow (discover.js) - cette dernière, en plus de
// dupliquer le code, divergeait aussi en COMPORTEMENT: seule elle sondait la progression
// (/auto-acquire/status/<id>) pour afficher un toast final "N fichiers envoyés", les 3
// autres se contentaient d'un toast de lancement (le résultat réel n'apparaissant qu'a
// posteriori dans l'Historique). Les deux pages ont désormais le même retour immédiat.
async function runSeriesAutoAcquire(seriesId, { volumeNumber = null, oneshot = false, isIntegral = false, isHs = false, isEpisode = false, seriesTitle = '', buttonEl = null } = {}) {
    if (buttonEl) buttonEl.disabled = true;
    // Retour immédiat avant l'appel réseau: même si le lancement prend du temps, le clic
    // doit être visible depuis la bibliothèque (et le toast est cliquable vers la série).
    if (typeof showToast === 'function') {
        const target = volumeNumber !== null ? `${seriesTitle || 'la série'} : Tome ${volumeNumber}` : (seriesTitle || 'la série');
        showToast('auto-acquire-now', `Recherche automatique lancée pour « ${target} »…`, { icon: 'radar', autoHideMs: 5000, href: `/series/${seriesId}` });
    }
    try {
        const body = { series_id: seriesId };
        if (oneshot) body.oneshot = true;
        else if (volumeNumber !== null) {
            body.volume_number = volumeNumber;
            // "Recherche automatique" est explicitement désactivé pour ces types dans
            // l'UI - transmis maintenant pour que la route construise le bon label
            // ("Intégrale N"/"HS N"/"Épisode N", voir POST /auto-acquire/run côté Flask)
            // sans quoi un résultat pourtant correct se faisait rejeter en amont comme
            // "ce n'est pas le tome N demandé" (vérification par simple numéro).
            if (isIntegral) body.is_integral = true;
            else if (isHs) body.is_hs = true;
            else if (isEpisode) body.is_episode = true;
        }

        const response = await fetch('/api/bedetheque/auto-acquire/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();

        // "dans les toast ajoute les liens des series vers l'app quand la serie s'affiche"
        // - seriesId est déjà connu avec certitude pour tous les toasts de cette fonction
        // (un seul appelé par série ciblée), cliquable vers sa fiche.
        const seriesHref = { href: `/series/${seriesId}` };

        if (!data.success) {
            showToast('auto-acquire-now', '❌ Erreur: ' + (data.error || 'Erreur inconnue'), { icon: 'circle-x', autoHideMs: 5000, ...seriesHref });
            return;
        }
        if (!data.started) {
            showToast('auto-acquire-now', 'Aucun tome manquant à chercher pour cette série.', { icon: 'info', autoHideMs: 4000, ...seriesHref });
            return;
        }

        // Style Sonarr ("Searching indexers for [Série : S03]. 12 active indexers") - le
        // nombre de tomes visés n'a de sens que pour une recherche série entière (une
        // recherche ciblée sur un tome précis ou un one-shot en vise déjà exactement un).
        const label = volumeNumber !== null ? `${seriesTitle} : Tome ${volumeNumber}` : seriesTitle;
        const tomeCountPrefix = (!oneshot && volumeNumber === null)
            ? `${data.count} ${pluralize(data.count, 'tome')}, ` : '';
        showToast('auto-acquire-now', `Recherche des sources pour [${label}]. ${tomeCountPrefix}${data.sources_count} ${pluralize(data.sources_count, 'source')} ${pluralize(data.sources_count, 'active')}`, { icon: 'search', autoHideMs: 5000, ...seriesHref });

        // Le résultat réel (téléchargé / rien de confiant) est de toute façon journalisé
        // dans l'Historique une fois la recherche terminée (voir log_action('auto_acquire',
        // ...), auto_acquire.py) - ce sondage court donne juste un retour immédiat sans
        // avoir à y aller, "tu peux charger les recherches instantanément" appliqué ici au
        // résultat final autant qu'au lancement.
        let checks = 0;
        const poll = setInterval(async () => {
            if (++checks > 150) return clearInterval(poll);
            try {
                const status = await (await fetch(`/api/bedetheque/auto-acquire/status/${seriesId}`)).json();
                if (status.running) return;
                clearInterval(poll);
                const count = status.downloaded_count || 0;
                showToast('auto-acquire-now', count
                    ? `✅ ${count} ${pluralize(count, 'fichier')} envoyé${count > 1 ? 's' : ''} au téléchargement — voir Import`
                    : 'ℹ️ Aucun fichier trouvé avec suffisamment de confiance.',
                    { icon: count ? 'check' : 'info', autoHideMs: 7000, ...seriesHref });
            } catch (_) { /* le prochain sondage réessaiera */ }
        }, 2000);
    } catch (error) {
        showToast('auto-acquire-now', '❌ Erreur de connexion: ' + error.message, { icon: 'circle-x', autoHideMs: 5000, href: `/series/${seriesId}` });
    } finally {
        if (buttonEl) buttonEl.disabled = false;
    }
}

// Nom complet à analyser pour un résultat: EBDZ a souvent un titre de thread qui ne porte
// pas l'extension/résolution (contrairement à son filename), et un torrent Prowlarr a
// parfois l'inverse - vérifier les DEUX champs concaténés plutôt qu'un seul en priorité
// (bug constaté: un .cbz clairement visible dans le filename ressortait quand même "?"
// côté format parce que seul result.title était regardé en premier, et il n'avait pas
// l'extension pour ces entrées EBDZ).
function _searchResultSearchableText(result) {
    return `${result.filename || ''} ${result.title || ''}`.toLowerCase();
}

function detectResultFormat(result) {
    // "pour prowlarr les fichiers ne sont pas retournés avec leur extension donc ton
    // parsing foire" - vérifié empiriquement: un titre de release Prowlarr est nommé à la
    // scene (points comme séparateurs de mots, ex.
    // "Foudroyants.T02.La.montagne.de.feu.2025.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER"), le
    // dernier segment après un point y est le groupe de release ("EBOOK-TONER"), pas
    // l'extension - même quand le vrai format (CBZ) apparaît bien plus tôt dans le titre.
    // Prowlarr ne fournit jamais de vrai nom de fichier (result.filename, seulement
    // result.title). "parse CBZ ou CBR, PDF que tu affiches" - au lieu du dernier segment
    // après un point (des bêtises pour cette source), cherche directement un des formats
    // connus (voir SEARCH_FORMAT_PRIORITY_DEFAULT ci-dessous) comme mot entier n'importe
    // où dans le titre - fiable même en scene naming, où le vrai format apparaît en plein
    // milieu plutôt qu'en dernière position. Premier trouvé dans l'ordre de priorité
    // configuré (utile quand un titre annonce plusieurs formats, ex. "[PDF & CBZ]": CBZ,
    // mieux classé par défaut, l'emporte).
    if (result.source === 'prowlarr') {
        const title = result.title || '';
        const known = getSearchFormatPriority().find(p => new RegExp(`\\b${p.format}\\b`, 'i').test(title));
        return known ? known.format : '';
    }
    // "zip c'est zip, rar c'est rar. matche just l'extension du fichier. ça pourrait
    // être. hgg tu mettras hgg." - extension brute telle quelle, aucun alias ni liste
    // fermée de formats connus (le correctif précédent affichait "CBZ" pour un .zip,
    // c'était faux : chaque extension s'affiche exactement pour ce qu'elle est).
    const name = result.filename || result.title || '';
    const parts = name.split('.');
    if (parts.length < 2) return '';
    const ext = parts[parts.length - 1].trim();
    return ext ? ext.toUpperCase() : '';
}

// Ordre de PRÉFÉRENCE des formats dans les résultats de recherche EBDZ/Prowlarr,
// configurable via /settings (onglet "Formats recherche", voir initSearchFormatPriority
// dans settings.js) - stocké en localStorage, purement un réglage d'affichage client.
//
// "format de recherche is wrong this only the entry we prefer not the one that show on
// search" - un format avec enabled:false NE retire PLUS aucun résultat (voir
// scoreSearchResult plus bas pour comment il est quand même déclassé) : ce réglage
// n'a jamais eu vocation à limiter/exclure des extensions de la recherche (manuelle ou
// automatique), seulement à exprimer un ordre de préférence pour le tri. Avant ce
// correctif, décocher un format le retirait entièrement des résultats affichés (voir
// l'ancienne _isSearchResultFormatAllowed/filterSearchResultsByFormat, supprimées) -
// y compris de la recherche automatique (discover.js/library.js réutilisent le même
// tableau de résultats, voir buildSearchResultsTableHtml), qui perdait donc des
// candidats valables plutôt que de simplement les classer après les formats préférés.
const SEARCH_FORMAT_PRIORITY_DEFAULT = [
    { format: 'CBZ', enabled: true },
    { format: 'CBR', enabled: true },
    { format: 'PDF', enabled: true },
];

function getSearchFormatPriority() {
    try {
        const stored = JSON.parse(localStorage.getItem('searchFormatPriority'));
        if (Array.isArray(stored) && stored.length) return stored;
    } catch (e) { /* localStorage corrompu -> défauts */ }
    return SEARCH_FORMAT_PRIORITY_DEFAULT;
}

// Ordre de préférence entre sources (Prowlarr/EBDZ/Telegram), configurable via /settings
// (même onglet "Formats recherche", voir initSearchSourcePriority dans settings.js) -
// demandé explicitement en plus du filtre de formats: "ajoute aussi ordre prowlarr > ebdz".
// Détermine directement le palier de tri (_searchResultTier) pour tout résultat
// effectivement téléchargeable - "la recherche met en avant les torrents en premier
// malgré le choix dans les settings. si c'est telegram ou ebdz en premier alors c'est mis
// en premier meme s'il y a des torrents disponibles". Seule exception, toujours reléguée
// au pire palier quelle que soit cette priorité: un torrent SANS seed ou un PDF (voir
// _searchResultTier) - ni l'un ni l'autre n'est réellement téléchargeable/désirable,
// aucun ordre de préférence ne change ça.
const SEARCH_SOURCE_PRIORITY_DEFAULT = ['prowlarr', 'ebdz', 'telegram', 'fourtoutici', 'annas_archive'];

function getSearchSourcePriority() {
    try {
        const stored = JSON.parse(localStorage.getItem('searchSourcePriority'));
        if (Array.isArray(stored) && stored.length) {
            // Une préférence sauvegardée avant l'ajout de 'telegram' à
            // SEARCH_SOURCE_PRIORITY_DEFAULT ne le contient pas - complété ici (en queue,
            // l'ordre déjà choisi par l'utilisateur pour les autres sources reste
            // intact) plutôt que de le faire disparaître silencieusement du tri ET de la
            // liste réordonnable des paramètres.
            const missing = SEARCH_SOURCE_PRIORITY_DEFAULT.filter(s => !stored.includes(s));
            return missing.length ? [...stored, ...missing] : stored;
        }
    } catch (e) { /* localStorage corrompu -> défauts */ }
    return SEARCH_SOURCE_PRIORITY_DEFAULT;
}

// "source order is the reference. In our settings it is telegram first. what are you
// doing?" - localStorage est PAR NAVIGATEUR: sauvegarder l'ordre depuis Configuration ne
// le propage jamais à un autre navigateur/appareil/session en navigation privée, qui
// retombe silencieusement sur SEARCH_SOURCE_PRIORITY_DEFAULT (Prowlarr en tête) même si
// le réglage serveur (auto_acquire_sources, déjà utilisé par l'acquisition automatique
// côté blueprints/bedetheque/auto_acquire.py) est bien à jour - deux vérités qui
// divergent silencieusement. Le serveur EST la référence désormais: ce script tourne sur
// toute page avec un tableau de résultats filtrable (voir CLAUDE.md), donc synchronise
// localStorage depuis /api/import/config dès le chargement, avant toute recherche/tri -
// best-effort, silencieux (une erreur réseau laisse simplement la dernière valeur connue
// en localStorage, jamais pire qu'avant ce correctif).
(function syncSearchSourcePriorityFromServer() {
    fetch('/api/import/config')
        .then(r => r.json())
        .then(config => {
            if (Array.isArray(config.auto_acquire_sources) && config.auto_acquire_sources.length) {
                localStorage.setItem('searchSourcePriority', JSON.stringify(config.auto_acquire_sources));
            }
        })
        .catch(() => {});
})();

// Résolution/tag de scan détecté dans le nom (ex: "UpScale 3840px", "Digital-1920px") -
// "why pour l'epervier tu ne matches pas correctement la resolution: [...]
// [Digital-2504] [...] - Digital-2504 is quality 2504?": result.resolution (calculé
// côté serveur par LibraryScanner.parse_filename, voir _resolution_label côté
// missing_monitor/searcher.py) reconnaît ce tag même SANS suffixe "px" explicite -
// préféré ici plutôt que deviné une seconde fois. Repli sur l'ancienne regex locale
// uniquement pour un résultat qui n'aurait pas encore ce champ (ex: réponse mise en
// cache d'avant cet ajout).
function detectResultResolution(result) {
    if (result.resolution) {
        const match = String(result.resolution).match(/(\d{3,4})/);
        if (match) return parseInt(match[1], 10);
    }
    const name = _searchResultSearchableText(result);
    const match = name.match(/(\d{3,4})\s*px/);
    return match ? parseInt(match[1], 10) : null;
}

// Regroupement en paliers (voir compareSearchResults) plutôt qu'un score continu: un
// torrent SANS seed n'est en pratique pas téléchargeable ("c'est nul") et un PDF est un
// format qu'on ne veut pas (perte de qualité/mise en page à la conversion) - ces deux cas
// restent toujours le pire palier (100, valeur sentinelle), quelle que soit la source ou
// sa priorité configurée: la qualité/disponibilité réelle du fichier ne rattrape jamais ça.
//
// EN DEHORS de ce pire palier, l'ordre entre torrent-avec-seeds/EBDZ/Telegram suit
// directement la priorité de source configurée par l'utilisateur (getSearchSourcePriority,
// réglable dans Configuration) plutôt qu'un ordre figé "torrent toujours premier" comme
// avant ce correctif - "la recherche met en avant les torrents en premier malgré le choix
// dans les settings. si c'est telegram ou ebdz en premier alors c'est mis en premier meme
// s'il y a des torrents disponibles".
function _searchResultTier(result) {
    if (detectResultFormat(result) === 'PDF') return 100;
    if (result.source === 'prowlarr' && !(result.seeders && result.seeders > 0)) return 100;

    const sourcePriority = getSearchSourcePriority();
    const idx = sourcePriority.indexOf(result.source);
    return idx === -1 ? sourcePriority.length : idx;
}

function scoreSearchResult(result) {
    const format = detectResultFormat(result);
    // Rang dérivé de l'ordre configuré (index 0 = meilleur) plutôt que des seuils fixes
    // d'avant - un format non trouvé dans la liste (ne devrait pas arriver, seuls les 4
    // formats ci-dessus sont détectés) ou non détecté (chaîne vide) retombe à 0, comme avant.
    const priority = getSearchFormatPriority();
    const idx = priority.findIndex(p => p.format === format && p.enabled);
    const formatRank = idx === -1 ? 0 : (priority.length - idx);

    const sourcePriority = getSearchSourcePriority();
    const sourceIdx = sourcePriority.indexOf(result.source);
    const sourceRank = sourceIdx === -1 ? 0 : (sourcePriority.length - sourceIdx);

    return {
        tier: _searchResultTier(result),
        formatRank,
        sourceRank,
        resolution: detectResultResolution(result) || 0,
        size: result.size || 0,
        seeders: result.source === 'prowlarr' ? (result.seeders || 0) : 0,
    };
}

// Palier d'abord, dans l'ordre de priorité de source configuré (voir _searchResultTier).
// Au sein d'un même palier ET quand les deux résultats sont des torrents Prowlarr: se
// départage par nombre de seeds (plus il y en a, plus le téléchargement sera rapide) puis
// par qualité en repli - le nombre de seeds n'a de sens qu'entre torrents entre eux, pas
// pour comparer un torrent à un lien EBDZ/Telegram (voir scoreSearchResult: seeders vaut
// toujours 0 pour ces sources). Sinon, se départage par qualité (format, puis résolution)
// d'abord, et à qualité égale par la taille la PLUS PETITE (même contenu en plus compact =
// mieux, pas l'inverse - taille plus grande ne veut pas dire meilleure qualité, juste plus
// lourd).
function compareSearchResults(a, b) {
    // "l'ordre n'est pas correct. si il y a un warning ça ne peut pas être en premier" -
    // unconfirmed_volume (voir _confirms_requested_volume côté searcher.py) doit primer
    // sur le palier/format/etc: le tri backend (_deduplicate_and_rank) le respecte déjà,
    // mais ce tri CLIENT (celui qui détermine réellement l'ordre affiché et le ⭐
    // "Meilleur choix", voir buildSearchResultsTableHtml) refaisait un tri complet sans
    // jamais consulter ce champ - un résultat non confirmé mais de bon palier/format
    // pouvait donc quand même remonter premier, y compris marqué comme le "meilleur choix".
    if (!!a.unconfirmed_volume !== !!b.unconfirmed_volume) {
        return a.unconfirmed_volume ? 1 : -1;
    }

    const sa = scoreSearchResult(a), sb = scoreSearchResult(b);
    if (sa.tier !== sb.tier) return sa.tier - sb.tier;

    if (a.source === 'prowlarr' && b.source === 'prowlarr') {
        return (sb.seeders - sa.seeders)
            || (sb.sourceRank - sa.sourceRank)
            || (sb.formatRank - sa.formatRank)
            || (sb.resolution - sa.resolution)
            || (sa.size - sb.size);
    }
    return (sb.sourceRank - sa.sourceRank)
        || (sb.formatRank - sa.formatRank)
        || (sb.resolution - sa.resolution)
        || (sa.size - sb.size);
}

// Icône de source (même logo que partout ailleurs dans l'app pour EBDZ/Prowlarr) - pour
// identifier l'origine d'un résultat en un coup d'œil, le nom de l'indexeur/forum seul
// (ex: "Torr9", "The Old School (API)") ne le rend pas évident.
//
// "ajoute lien sur l'icone de source vers la source dans les recherches comme ca je
// pourrais voir comment est le fichier source" - lien vers la PAGE de la release (fil du
// forum EBDZ, page de la release chez l'indexeur Prowlarr, message Telegram), PAS le lien
// de téléchargement direct (déjà géré par les boutons d'action à droite): pouvoir vérifier
// le fichier source avant de le télécharger, pas le récupérer directement depuis l'icône.
function _searchResultSourceLinkUrl(result) {
    if (result.source === 'prowlarr') return result.info_url || '';
    if (result.source === 'telegram') return (result.channel && result.message_id) ? `https://t.me/${result.channel}/${result.message_id}` : '';
    // "pour fourtici dans recherche tu peux mettre le lien sur l'icone" - essayé avec le
    // lien de téléchargement direct (aucune page de détail par fichier n'existe côté
    // fourtoutici, API JSON brute) puis abandonné : "Direct hotlinking not allowed" -
    // leur serveur bloque toute navigation directe vers ce lien (contrôle de Referer),
    // seul le téléchargement propre de l'app (download_fourtoutici_file_background,
    // requête serveur à serveur) fonctionne. Aucun lien utilisable n'existe donc pour
    // cette source - pas d'icône cliquable, comme avant.
    if (result.source === 'fourtoutici') return '';
    if (result.source === 'annas_archive') return result.info_url || '';
    return result.thread_url || '';
}

function _searchResultSourceIconHtml(result, sourceLabel) {
    const icon = result.source === 'prowlarr'
        ? `<img src="/static/img/prowlarr-logo.svg" alt="Prowlarr" class="replace-results-source-icon">`
        : result.source === 'telegram'
            ? `<img src="/static/img/telegram-logo.svg" alt="Telegram" class="replace-results-source-icon">`
            : result.source === 'fourtoutici'
                ? `<img src="/static/img/fourtoutici-favicon.svg" alt="fourtoutici" class="replace-results-source-icon">`
                : result.source === 'annas_archive'
                    ? `<img src="/static/img/annas-archive-favicon.ico" alt="Anna's Archive" class="replace-results-source-icon">`
                    : `<img src="/static/img/ebdz-logo.png" alt="EBDZ" class="replace-results-source-icon">`;
    const sourceLinkUrl = safeExternalHttpUrl(_searchResultSourceLinkUrl(result));
    return sourceLinkUrl
        ? `<a href="${escapeHtml(sourceLinkUrl)}" target="_blank" rel="noopener noreferrer" data-tooltip="${escapeHtml(sourceLabel)} - voir la source">${icon}</a>`
        : icon;
}

// Nom affiché dans la colonne Fichier - factorisé hors de buildSearchResultRowHtml pour
// être réutilisable comme valeur de tri (voir _searchResultSortValue) sans dupliquer la
// logique par source.
function _searchResultDisplayName(result) {
    if (result.source === 'ebdz') return decodeFilename(result.filename || result.title || '');
    if (result.source === 'telegram' || result.source === 'fourtoutici') return result.filename || result.title || '';
    return result.title || '';
}

// Marque un résultat comme déjà ajouté à un client de téléchargement, sur l'objet
// RÉSULTAT lui-même (pas sur le bouton DOM cliqué) - à appeler depuis le bloc
// `data.success` de addToEmule/addTorrentToQbittorrent/addTorrentToRtorrent/
// addTorrentToDeluge (search.js ET library.js, chacune sa propre copie) juste avant la
// mutation du bouton. Cherche par lien plutôt que par référence d'objet: ces fonctions ne
// reçoivent que le lien (celui passé à l'onclick), jamais le résultat complet.
function _markSearchResultAdded(link, clientKey) {
    const result = (_searchTableAllResults || []).find(r => (r.download_url || r.link) === link);
    if (!result) return;
    if (!result._addedClients) result._addedClients = {};
    result._addedClients[clientKey] = true;
    return result;
}

// Même principe que ci-dessus pour Telegram, qui n'a pas de lien direct (channel +
// message_id identifient le fichier à la place) - voir downloadTelegramFile.
function _markTelegramResultAdded(channel, messageId) {
    const result = (_searchTableAllResults || []).find(
        r => r.source === 'telegram' && r.channel === channel && r.message_id === messageId
    );
    if (!result) return;
    if (!result._addedClients) result._addedClients = {};
    result._addedClients.telegram = true;
    return result;
}

// Même principe pour Shelfmark (Anna's Archive n'a ni download_url ni link, voir
// blueprints/annas_archive/scraper.py - seul md5 identifie un résultat de façon fiable).
function _markShelfmarkResultAdded(md5) {
    const result = (_searchTableAllResults || []).find(r => r.md5 === md5);
    if (!result) return;
    if (!result._addedClients) result._addedClients = {};
    result._addedClients.shelfmark = true;
    return result;
}

// Bouton "Ajouter à <client>" - rendu déjà confirmé (coche, désactivé) si `added` est vrai
// plutôt que le bouton cliquable par défaut, pour refléter dès le rendu un état persisté
// sur le résultat (voir _markSearchResultAdded/added ci-dessus dans buildSearchResultRowHtml).
// actionVerb: "Ajouter"/"Envoyer" - reprend le libellé propre à chaque client d'avant ce
// correctif plutôt que d'en imposer un seul aux 4. extraClass: '.result-action-emule' pour
// le bouton eMule uniquement, que checkEmuleStatus() (search.js/library.js) continue de
// trouver par ce sélecteur pour le masquer/montrer selon la config aMule, quel que soit
// l'état ajouté/pas encore ajouté du bouton.
function _clientAddButtonHtml(added, clientLabel, iconHtml, onclickJs, actionVerb = 'Envoyer', extraClass = '') {
    if (added) {
        return `<button class="btn-icon-only add-button-added${extraClass}" data-tooltip="Ajouté à ${clientLabel}" disabled><span class="btn-icon">✓</span></button>`;
    }
    return `<button class="btn-icon-only${extraClass}" data-tooltip="${actionVerb} à ${clientLabel}" onclick="${onclickJs}">${iconHtml}</button>`;
}

// Construit une ligne du tableau de résultats - une seule structure compacte pour
// EBDZ/Prowlarr/Telegram au lieu d'affichages séparés, avec juste les actions qui
// diffèrent selon la source.
function buildSearchResultRowHtml(result) {
    // "met une icône quand j'ai déjà le volume. retire l'étoile. c'est inutile" -
    // _searchTableOwnedLabels n'est peuplé que pour une recherche "série entière" (voir
    // buildSearchResultsTableHtml/_loadOwnedVolumeLabels), null sinon - une recherche
    // d'un tome précis n'affiche donc jamais cette icône (il est par définition manquant).
    // Même pastille/icône que Nouveautés (.icon-owned, voir ebdz-latest.js) plutôt qu'une
    // nouvelle présentation pour la même idée "déjà possédé".
    const isOwned = _isResultOwned(result);
    const isEbdz = result.source === 'ebdz';
    const isTelegram = result.source === 'telegram';
    const isFourtoutici = result.source === 'fourtoutici';
    const isAnnasArchive = result.source === 'annas_archive';
    const displayName = _searchResultDisplayName(result);
    const format = detectResultFormat(result);
    const resolution = detectResultResolution(result);
    // Sources ed2k réelles pour EBDZ une fois connues (voir _loadEd2kAvailability, requête
    // groupée déclenchée après le rendu initial - "…" le temps qu'elle réponde), toujours
    // "—" pour Telegram/fourtoutici (pas de notion de disponibilité, un seul exemplaire
    // servi directement par le site/compte, pas un essaim de pairs).
    const seedsPeers = isEbdz
        ? (result._ed2kSources !== undefined ? `${result._ed2kSources} source${result._ed2kSources > 1 ? 's' : ''}` : '…')
        : (isTelegram || isFourtoutici || isAnnasArchive) ? '—' : `${result.seeders ?? 'N/A'} / ${result.peers ?? 'N/A'}`;
    const sourceLabel = isEbdz ? (result.forum || 'EBDZ') : isTelegram ? (result.channel_title || 'Telegram') : isFourtoutici ? 'fourtoutici' : isAnnasArchive ? "Anna's Archive" : (result.indexer || 'Prowlarr');
    // "dans historique il faudrait voir quelle est la source du téléchargement et
    // cliquable aussi" - même lien que l'icône de source ci-dessus (_searchResultSourceLinkUrl),
    // transmis jusqu'à log_manual_download par les fonctions addToEmule/
    // addTorrentToQbittorrent/etc. (search.js ET library.js, chacune sa propre copie).
    const sourceLinkUrl = _searchResultSourceLinkUrl(result);

    // Titre transmis à mark_download_pending/log_manual_download (voir addToEmule etc.) -
    // "the name should be the file downloaded so finding the right client download stats
    // is easy": toujours le nom de fichier RÉEL du résultat, jamais un libellé "Série -
    // Volume N" reconstruit - c'est ce nom que le client (qBittorrent/rTorrent/Deluge/
    // aMule) rapportera ensuite tel quel, une comparaison de chaîne (voir _filenames_match
    // côté downloader.py) n'est fiable que si les deux désignent le même texte. La série/
    // le tome (déjà connus avec certitude si la recherche part d'une fiche série, voir
    // displaySearchResults dans library.js) sont transmis À PART (trackingSeriesId/
    // trackingVolumeId/trackingVolumeNumber ci-dessous) plutôt que devinés plus tard par
    // matching flou depuis ce titre.
    const trackingTitle = displayName;
    // Littéraux JS 'null' (pas une chaîne vide/undefined) dans les onclick ci-dessous:
    // reçus tels quels par addToEmule/addTorrentToQbittorrent/etc. puis json-stringifiés
    // vers le serveur - mark_download_pending distingue explicitement "pas de contexte
    // série connu" (None) de 0, un id de série valide.
    const trackingSeriesId = _searchResultsContextSeriesId ?? 'null';
    const trackingVolumeId = _searchResultsContextVolumeId ?? 'null';
    // "pourquoi thorgal ca na pas bien matcher les volumes" - un résultat porte déjà SON
    // PROPRE numéro de tome parsé depuis son nom de fichier (result.volume, EBDZ/Telegram/
    // Prowlarr - voir LibraryScanner.parse_filename côté search/routes.py, telegram_channels/
    // routes.py, prowlarr/search.py), mais c'était jusqu'ici ignoré au profit du SEUL
    // contexte de recherche partagé (_searchResultsContextVolumeNumber, un seul numéro pour
    // TOUTE la table) - correct seulement quand la recherche part d'un "Rechercher CE tome"
    // depuis une fiche série, faux dès qu'une recherche plus large (Recherche/Nouveautés)
    // renvoie plusieurs tomes différents en une seule liste: chaque "Ajouter" collait alors
    // soit le même numéro à tous, soit aucun. Préfère désormais le numéro propre au résultat,
    // ne retombe sur le contexte partagé que si son propre nom n'a rien laissé extraire.
    // "01 à 09 ... matché tome 1" - bug réel: un résultat marqué intégrale/pack
    // (result.is_integral, ex. "Adèle Blanc-Sec - 01 à 09 - BDPACK.zip", parsé par
    // LibraryScanner.parse_filename côté searcher.py) n'a lui-même AUCUN volume unique
    // (result.volume reste null, c'est un lot de plusieurs tomes) - retomber quand même
    // sur le contexte de recherche partagé (le tome UNIQUE recherché, ex. "Tome 1
    // manquant") lui collait à tort ce numéro isolé, comme si le pack entier n'était
    // QUE ce tome. Un pack/une intégrale ne doit jamais hériter du contexte de
    // recherche à tome unique - seul un résultat qui n'est ni intégrale ni hors-série
    // ni épisode (donc un vrai tome simple sans numéro propre extrait) en profite.
    const trackingVolumeNumber = (
        result.volume != null
            ? result.volume
            : (result.is_integral || result.is_hs || result.is_episode ? null : _searchResultsContextVolumeNumber)
    ) ?? 'null';
    // "Remplacer quand même" - littéral JS true/false (jamais 'null': contrairement aux
    // trois id ci-dessus, force_replace a toujours une valeur connue, pas de notion de
    // "contexte absent" à distinguer) - voir _searchResultsContextIsReplacement plus haut.
    const trackingForceReplace = !!_searchResultsContextIsReplacement;

    // data-tooltip (pas title): l'app a son propre système d'infobulle instantané (voir
    // nav.js showJsTooltip) - un title="" natif se déclenche avec le délai de survol de
    // l'OS/navigateur (~1s), perceptible comme "pas instantané" comparé aux autres
    // boutons de l'app qui utilisent déjà data-tooltip
    // Un bouton par client de téléchargement, mais seulement s'il est activé dans
    // Configuration (voir enabledDownloadClients, nav.js - "affiche dans les
    // téléchargements juste les clients qui sont activés") plutôt que de toujours
    // afficher les 4 quel que soit leur état.
    //
    // added.CLIENT (voir _markSearchResultAdded) persiste "déjà ajouté" sur le RÉSULTAT
    // lui-même plutôt que seulement sur le bouton en DOM - "quand je rajoute un fichier a
    // télécharger et appuie sur l'ordre je perds le bouton just added": trier une colonne
    // (_renderSearchResultsTbody) reconstruit tout le tbody depuis _searchTableAllResults,
    // un innerHTML de bouton modifié après coup ne survit pas à cette reconstruction.
    const added = result._addedClients || {};
    let actionsHtml;
    if (isEbdz) {
        actionsHtml = `
            <button class="btn-icon-only" data-tooltip="Copier le lien" onclick="copyLink('${escapeForAttribute(result.link)}', this)">${svgIcon('copy')}</button>
            ${enabledDownloadClients.amule ? _clientAddButtonHtml(added.amule, 'eMule', `<img src="/static/img/emule-logo.svg" alt="" class="torrent-client-logo">`, `addToEmule('${escapeForAttribute(result.link)}', this, '${escapeForAttribute(trackingTitle)}', ${trackingSeriesId}, ${trackingVolumeId}, ${trackingVolumeNumber}, '${escapeForAttribute(result.source)}', '${escapeForAttribute(sourceLinkUrl)}', ${trackingForceReplace})`, 'Ajouter', ' result-action-emule') : ''}
        `;
    } else if (isTelegram) {
        // Pas de lien direct à copier (le fichier vit dans le compte Telegram, pas
        // derrière une URL) - le seul geste possible est de le télécharger via la session
        // connectée (voir downloadTelegramFile, appelle /api/telegram-channels/download)
        // droit dans un répertoire d'import surveillé, comme n'importe quel autre fichier.
        // Style "icône conservée + texte Ajouté" (pas une coche, voir downloadTelegramFile)
        // - même état déjà-ajouté persistant que les autres clients, juste un rendu différent.
        actionsHtml = added.telegram
            ? `<button class="btn-icon-only add-button-added" data-tooltip="Téléchargement démarré - voir sa progression sur la page Import" disabled>${svgIcon('download')} <span style="font-size:0.85em;">Ajouté</span></button>`
            : `<button class="btn-icon-only" data-tooltip="Télécharger vers l'import" onclick="downloadTelegramFile('${escapeForAttribute(result.channel)}', ${result.message_id}, this, '${escapeForAttribute(result.channel_title || '')}', '${escapeForAttribute(trackingTitle)}', ${trackingSeriesId}, ${trackingVolumeId}, ${trackingVolumeNumber}, '${escapeForAttribute(sourceLinkUrl)}', ${trackingForceReplace})">${svgIcon('download')}</button>`;
    } else if (isFourtoutici) {
        // Comme Telegram (téléchargement interne droit vers l'import, pas de client
        // externe à piloter) mais avec un vrai lien à copier - fourtoutici sert ses
        // fichiers en HTTP direct, pas depuis un compte applicatif privé.
        actionsHtml = `
            <button class="btn-icon-only" data-tooltip="Copier le lien" onclick="copyLink('${escapeForAttribute(result.download_url || result.link)}', this)">${svgIcon('copy')}</button>
            ${added.fourtoutici
                ? `<button class="btn-icon-only add-button-added" data-tooltip="Téléchargement démarré - voir sa progression sur la page Import" disabled>${svgIcon('download')} <span style="font-size:0.85em;">Ajouté</span></button>`
                : `<button class="btn-icon-only" data-tooltip="Télécharger vers l'import" onclick="downloadFourtoutici('${escapeForAttribute(result.file_id)}', this, '${escapeForAttribute(trackingTitle)}', ${trackingSeriesId}, ${trackingVolumeId}, ${trackingVolumeNumber}, '${escapeForAttribute(result.download_url || result.link || '')}', ${trackingForceReplace})">${svgIcon('download')}</button>`}
        `;
    } else if (isAnnasArchive) {
        const shelfmarkAdded = added.shelfmark;
        actionsHtml = `
            <a class="btn-icon-only" href="${escapeHtml(result.info_url || result.link || '#')}" target="_blank" rel="noopener noreferrer" data-tooltip="Voir le fichier sur Anna's Archive"><img src="/static/img/web-logo.svg" alt="Web" class="torrent-client-logo"></a>
            ${shelfmarkAdded
                    ? `<button class="btn-icon-only add-button-added" data-tooltip="Envoyé à Shelfmark" disabled><img src="/static/img/shelfmark.svg" alt="Shelfmark" class="torrent-client-logo"></button>`
                // "Tome null" - trackingSeriesId/trackingVolumeId/trackingVolumeNumber (voir
                // plus haut) sont délibérément la CHAÎNE 'null' quand inconnus, pour
                // s'interpoler en code JS BRUT (littéral null) dans les onclick des autres
                // clients ci-dessus (addToEmule, addTorrentToQbittorrent...). Ici, contrairement
                // à ces autres appels, la valeur traverse un JSON.stringify() qui s'EXÉCUTE
                // (pas juste du texte source) - la chaîne 'null' s'y sérialise alors en JSON
                // comme la CHAÎNE "null", pas le null JSON attendu, et ressortait telle quelle
                // jusqu'à l'affichage ("Tome null" sur /import, seul client affecté puisque
                // seul celui-ci sérialise ces valeurs plutôt que de les injecter en code brut).
                : `<button class="btn-icon-only" data-tooltip="Télécharger via Shelfmark" onclick="downloadViaShelfmark(this, '${encodeURIComponent(JSON.stringify({...result, series_id: trackingSeriesId === 'null' ? null : trackingSeriesId, volume_id: trackingVolumeId === 'null' ? null : trackingVolumeId, volume_number: trackingVolumeNumber === 'null' ? null : trackingVolumeNumber, force_replace: trackingForceReplace}))}')"><img src="/static/img/shelfmark.svg" alt="Shelfmark" class="torrent-client-logo"></button>`}`;
    } else {
        actionsHtml = `
            <button class="btn-icon-only" data-tooltip="Copier le lien" onclick="copyLink('${escapeForAttribute(result.download_url || result.link)}', this)">${svgIcon('copy')}</button>
            ${(result.download_url || result.link) ? `
                ${enabledDownloadClients.qbittorrent ? _clientAddButtonHtml(added.qbittorrent, 'qBittorrent', `<img src="/static/img/qbittorrent-logo.svg" alt="" class="torrent-client-logo">`, `addTorrentToQbittorrent('${escapeForAttribute(result.download_url || result.link)}', this, '${escapeForAttribute(trackingTitle)}', ${trackingSeriesId}, ${trackingVolumeId}, ${trackingVolumeNumber}, '${escapeForAttribute(sourceLinkUrl)}', ${trackingForceReplace})`) : ''}
                ${enabledDownloadClients.rtorrent ? _clientAddButtonHtml(added.rtorrent, 'rTorrent', `<img src="/static/img/rtorrent-logo.svg" alt="" class="torrent-client-logo">`, `addTorrentToRtorrent('${escapeForAttribute(result.download_url || result.link)}', this, '${escapeForAttribute(trackingTitle)}', ${trackingSeriesId}, ${trackingVolumeId}, ${trackingVolumeNumber}, '${escapeForAttribute(sourceLinkUrl)}', ${trackingForceReplace})`) : ''}
                ${enabledDownloadClients.deluge ? _clientAddButtonHtml(added.deluge, 'Deluge', `<img src="/static/img/deluge-logo.svg" alt="" class="torrent-client-logo">`, `addTorrentToDeluge('${escapeForAttribute(result.download_url || result.link)}', this, '${escapeForAttribute(trackingTitle)}', ${trackingSeriesId}, ${trackingVolumeId}, ${trackingVolumeNumber}, '${escapeForAttribute(sourceLinkUrl)}', ${trackingForceReplace})`) : ''}
            ` : ''}
        `;
    }

    // "vérifie si les résultats parsent bien aux volumes et séries recherchés. si c'est
    // pas le cas ils vont en dernier et avec un warning" - Prowlarr fait une recherche
    // plein texte (voir unconfirmed_volume, _confirms_requested_volume côté
    // searcher.py): le tri met déjà ces résultats en dernier, ce badge les signale aussi
    // visuellement plutôt que de les laisser se mêler silencieusement aux résultats
    // confirmés.
    // "le warning faudrait savoir pourquoi" - unconfirmed_reason (searcher.py,
    // _confirms_requested_volume) donne le motif concret (mauvais tome, intégrale/HS/
    // one-shot au lieu du tome demandé, titre trop générique...) plutôt qu'un message
    // générique identique pour tous les cas. "il faudrait voir la raison du warning" -
    // affiché en clair sous le nom de fichier (pas seulement dans le data-tooltip au
    // survol, facilement manqué) pour que le motif saute aux yeux immédiatement.
    const unconfirmedReasonText = result.unconfirmed_volume
        ? (result.unconfirmed_reason || 'Le titre ne confirme pas clairement le tome recherché - à vérifier avant de télécharger')
        : '';
    const unconfirmedVolumeHtml = result.unconfirmed_volume
        ? `<span style="color:#e67e22;" data-tooltip="${escapeHtml(unconfirmedReasonText)}">⚠️</span>`
        : '';

    return `
        <tr class="replace-results-row${isOwned ? ' replace-results-row-owned' : ''}">
            <td class="replace-results-best-marker">${isOwned ? `<span class="icon-owned" data-tooltip="En bibliothèque">${svgIcon('check')}</span>` : ''}</td>
            <td class="replace-results-filename" title="${escapeHtml(displayName)}">
                ${unconfirmedVolumeHtml} ${escapeHtml(displayName)}
                ${unconfirmedReasonText ? `<div style="color:#e67e22; font-size:0.8em; margin-top:2px;">${escapeHtml(unconfirmedReasonText)}</div>` : ''}
            </td>
            <td style="white-space:nowrap;">${result.parsed_volume ? escapeHtml(result.parsed_volume) : '—'}</td>
            <td style="white-space:nowrap; text-align:center;"${_searchResultSourceLinkUrl(result) ? '' : ` data-tooltip="${escapeHtml(sourceLabel)}"`}>${_searchResultSourceIconHtml(result, sourceLabel)}</td>
            <td>${format}</td>
            <td>${resolution ? `${resolution}px` : '—'}</td>
            <td style="white-space:nowrap;">${formatBytes(result.size)}</td>
            <td style="white-space:nowrap;">${seedsPeers}</td>
            <td class="replace-results-actions">${actionsHtml}</td>
        </tr>
    `;
}

// Filtres "type / taille" directement dans l'en-tête du tableau ("mets les filtres
// directement dans le header") plutôt qu'une barre de filtre séparée au-dessus - un
// second niveau de filtrage purement client (aucune nouvelle requête), appliqué sur
// _searchTableAllResults (déjà trié par préférence de format, voir buildSearchResultsTableHtml)
// et ne touchant que le <tbody> pour ne pas reconstruire l'en-tête à chaque changement.
// Se réinitialise à chaque nouvelle recherche (buildSearchResultsTableHtml régénère tout
// le tableau, y compris les <select> - remise à zéro naturelle, cohérente avec le filtre
// texte existant au-dessus de la page qui reconstruit lui aussi tout le tableau).
let _searchTableAllResults = [];
let _searchTableFilters = { source: '', format: '', size: '', volume: '', title: '', hideUnconfirmed: true, owned: '' };
// Identité RÉELLE de la série/du tome déjà connus quand la recherche part d'une fiche
// série - "le volume/album doit être matché si le clic vient d'une fiche série": transmis
// jusqu'à mark_download_pending (active_downloads) via les boutons d'action ci-dessous,
// pour que le fichier une fois sur disque se rattache à sa série/son tome sans repasser
// par un matching flou de nom de fichier. volumeId reste souvent null même avec un
// seriesId connu: un tome MANQUANT n'a par définition pas encore de ligne `volumes` en
// base à cet instant - seul un remplacement (currentVolumeId côté searchMissingVolume) en
// fournit un. volumeNumber (le numéro simple, toujours connu dès qu'un tome précis est
// recherché) comble cet écart pour l'affichage/l'auto-assignation à l'import.
let _searchResultsContextSeriesId = null;
let _searchResultsContextVolumeId = null;
let _searchResultsContextVolumeNumber = null;
// "Remplacer quand même" - true seulement pour une recherche explicite "Rechercher un
// remplacement" d'un tome DÉJÀ possédé (currentVolumeId non-null côté searchMissingVolume,
// library.js - jamais posé pour un tome manquant, voir son commentaire), jamais pour une
// simple recherche de tome manquant ou les pages Recherche/Nouveautés qui n'ont pas ce
// contexte. Transmis à chaque bouton "Ajouter"/"Télécharger" ci-dessous pour que le
// téléchargement porte cette intention jusqu'à l'import (voir mark_download_pending ->
// destination['force_replace'] -> _execute_import_batch, qui fait alors sauter la
// comparaison de taille is_better_volume).
let _searchResultsContextIsReplacement = false;

// "dans la recherche de série met une icône quand j'ai déjà le volume. retire l'étoile"
// - uniquement pertinent pour une recherche "série entière" (volumeNumber === null,
// plusieurs tomes différents remontent): chargé une fois par recherche (fire-and-forget,
// même principe que _loadEd2kAvailability plus bas) plutôt qu'à chaque ligne, réutilisé
// par buildSearchResultRowHtml pour comparer au parsed_volume de chaque résultat.
let _searchTableOwnedLabels = null;
// "pour les one-shot si le fichier existe c'est deja possédé" - un one-shot n'a qu'un
// seul album au total, sans numéro/classification distinctive à comparer (le tome
// possédé n'a ni volume_number/is_integral/is_hs/is_episode, ET un résultat de
// recherche pour ce même one-shot n'a le plus souvent aucun tag "OS"/"One-shot" non
// plus dans son propre nom de fichier - _ownedVolumeLabel renverrait null des deux
// côtés, jamais un match). Pour ce cas précis, la seule vraie question est "le fichier
// est-il déjà possédé", pas une comparaison étiquette par étiquette - vrai dès que le
// tome existant du one-shot a un fichier, quel que soit le résultat de recherche.
let _searchTableOneshotOwned = false;
let _ownedVolumesGeneration = 0;

// Même format que _parsed_volume_label côté serveur (missing_monitor/searcher.py) - pas
// exactement 1:1 (le cas "T01 à T06" d'une intégrale couvrant une plage de tomes n'est
// pas reproduit ici, trop rare pour la complexité que ça ajouterait), mais couvre les cas
// courants (tome/intégrale/hors-série/épisode numérotés ou non).
function _ownedVolumeLabel(v) {
    if (v.is_integral) return v.integral_number ? `Intégrale ${v.integral_number}` : 'Intégrale';
    if (v.is_hs) return v.hs_number ? `HS ${v.hs_number}` : 'Hors-série';
    if (v.is_episode) return v.episode_number ? `Épisode ${v.episode_number}` : 'Épisode';
    if (v.volume_number != null) return `Tome ${v.volume_number}`;
    return null;
}

async function _loadOwnedVolumeLabels(seriesId) {
    _ownedVolumesGeneration++;
    const generation = _ownedVolumesGeneration;
    try {
        const [volumes, series] = await Promise.all([
            fetch(`/api/series/${seriesId}/volumes`).then(r => r.json()),
            fetch(`/api/series/${seriesId}`).then(r => r.json())
        ]);
        if (generation !== _ownedVolumesGeneration) return; // une recherche plus récente a démarré entre-temps
        _searchTableOwnedLabels = new Set(
            (volumes || []).filter(v => v.filepath).map(_ownedVolumeLabel).filter(Boolean)
        );
        _searchTableOneshotOwned = !!series.is_oneshot && (volumes || []).some(v => v.filepath);
        _renderSearchResultsTbody();
    } catch (error) {
        console.warn('Erreur récupération des tomes déjà possédés:', error);
    }
}

function _isResultOwned(result) {
    if (_searchTableOneshotOwned) return true;
    return !!(_searchTableOwnedLabels && result.parsed_volume && _searchTableOwnedLabels.has(result.parsed_volume));
}

const SEARCH_SIZE_BUCKETS = [
    { value: '', label: 'Toutes tailles' },
    { value: 'lt100', label: '< 100 Mo' },
    { value: '100-300', label: '100-300 Mo' },
    { value: 'gt300', label: '> 300 Mo' },
];

// Compteur de génération ("Rechercher"/nouvelle recherche invalide une requête de
// disponibilité ed2k encore en vol d'une recherche précédente), même principe que
// searchGeneration (discover.js, voir CLAUDE.md) - une réponse tardive de
// ed2k.shortypower.org ne doit pas écrire dans un tableau qui affiche déjà les résultats
// d'une recherche plus récente.
let _ed2kAvailabilityGeneration = 0;

// Enrichit les résultats EBDZ déjà affichés avec leur disponibilité ed2k réelle (nombre de
// sources - "utilise amule cli or ed2k.shortypower.org pour checker la disponibilité des
// liens emule... ensuite on pourra ordonner par nombre de seed comme pour les torrents"),
// déclenché après coup depuis buildSearchResultsTableHtml: le tableau s'affiche
// immédiatement avec "…" en colonne Seeds/Peers, cet appel groupé (un seul POST, jamais un
// par ligne) le complète dès que ed2k.shortypower.org répond, sans jamais bloquer
// l'affichage initial des résultats.
async function _loadEd2kAvailability(results) {
    const ebdzLinks = [...new Set(results.filter(r => r.source === 'ebdz' && r.link).map(r => r.link))];
    if (ebdzLinks.length === 0) return;

    _ed2kAvailabilityGeneration++;
    const generation = _ed2kAvailabilityGeneration;

    try {
        const response = await fetch('/api/emule/ed2k-availability', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ links: ebdzLinks })
        });
        const data = await response.json();
        if (generation !== _ed2kAvailabilityGeneration) return; // une recherche plus récente a démarré entre-temps
        if (!data.success) return;

        const availability = data.availability || {};
        results.forEach(r => {
            if (r.source === 'ebdz' && r.link && availability[r.link] !== undefined) {
                r._ed2kSources = availability[r.link];
            }
        });
        _renderSearchResultsTbody();
    } catch (error) {
        console.warn('Erreur récupération disponibilité ed2k:', error);
    }
}

function _searchResultSourceLabel(source) {
    if (source === 'prowlarr') return 'Prowlarr';
    if (source === 'telegram') return 'Telegram';
    if (source === 'fourtoutici') return 'fourtoutici';
    if (source === 'annas_archive') return "Anna's Archive";
    return 'EBDZ';
}

function _searchResultMatchesSizeBucket(result, bucket) {
    if (!bucket) return true;
    const mb = (result.size || 0) / (1024 * 1024);
    if (bucket === 'lt100') return mb < 100;
    if (bucket === '100-300') return mb >= 100 && mb <= 300;
    if (bucket === 'gt300') return mb > 300;
    return true;
}

// Insensible aux accents pour le filtre "Fichier" ("dans les filtres du resultat des
// sources il prend en compte les accents. a ne pas prendre en compte") - seul filtre en
// texte libre de ce tableau, les autres (Source/Format/Taille/Volume) comparent une
// valeur exacte issue d'un <select> donc n'ont pas ce problème. Même logique que
// normalizeForSearch (library.js) mais dupliquée ici plutôt que partagée: ce fichier est
// le seul chargé sur TOUTES les pages ayant ce tableau (y compris /search et /discover,
// où library.js ne l'est pas - voir en-tête de fichier).
function _normalizeFilterText(text) {
    return String(text).normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

function _filteredSearchTableResults() {
    const filtered = _searchTableAllResults.filter(r => {
        if (_searchTableFilters.source && r.source !== _searchTableFilters.source) return false;
        if (_searchTableFilters.format && detectResultFormat(r) !== _searchTableFilters.format) return false;
        if (!_searchResultMatchesSizeBucket(r, _searchTableFilters.size)) return false;
        // "c'est quand tu fais une recherche globale de la série... met un filtre pour
        // choisir le volume" - utile pour isoler un tome précis parmi les résultats
        // d'une recherche série entière, comme les filtres Source/Format/Taille déjà là.
        if (_searchTableFilters.volume && (r.parsed_volume || '') !== _searchTableFilters.volume) return false;
        // "ajoute un filtre sur le titre" - texte libre (pas un <select>, contrairement
        // aux autres filtres dont les valeurs possibles sont limitées et connues d'avance)
        // sur le nom affiché du résultat, pour isoler une release précise parmi beaucoup.
        if (_searchTableFilters.title && !_normalizeFilterText(_searchResultDisplayName(r)).includes(_normalizeFilterText(_searchTableFilters.title))) return false;
        // "mets une checkbox pour afficher/masquer les résultats correspondant au bon
        // volume" - unconfirmed_volume (_confirms_requested_volume côté searcher.py) est
        // déjà affiché avec un ⚠️ + motif et trié en dernier, mais reste visible par
        // défaut (ex: Murena tome 14 pas encore sorti - montrer les tomes proches plutôt
        // qu'une liste vide) - cette case permet de les masquer complètement quand on ne
        // veut voir que de vrais matches.
        if (_searchTableFilters.hideUnconfirmed && r.unconfirmed_volume) return false;
        // "met un filtre pour voir où seuls les tomes non possédé" - _searchTableOwnedLabels
        // n'est peuplé que pour une recherche "série entière" (voir _loadOwnedVolumeLabels),
        // même portée que l'icône "Déjà possédé" qu'il alimente déjà.
        if (_searchTableFilters.owned) {
            const isOwned = _isResultOwned(r);
            if (_searchTableFilters.owned === 'missing' && isOwned) return false;
            if (_searchTableFilters.owned === 'owned' && !isOwned) return false;
        }
        return true;
    });
    // Sans colonne active, l'ordre de pertinence déjà appliqué à _searchTableAllResults
    // (voir buildSearchResultsTableHtml) est conservé tel quel par filter().
    if (!_searchTableSort.column) return filtered;
    return [...filtered].sort((a, b) => _compareSearchResultsByColumn(a, b, _searchTableSort.column, _searchTableSort.direction));
}

function _renderSearchResultsTbody() {
    const tbody = document.getElementById('search-results-tbody');
    if (!tbody) return;
    const filtered = _filteredSearchTableResults();
    tbody.innerHTML = filtered.length
        ? filtered.map(result => buildSearchResultRowHtml(result)).join('')
        : '<tr><td colspan="9" style="text-align:center; padding:20px; color:var(--color-text-muted);">Aucun résultat pour ces filtres</td></tr>';
}

function _applySearchTableFilter(field, value) {
    _searchTableFilters[field] = value;
    _renderSearchResultsTbody();
}

// Tri par en-tête cliquable ("pouvoir ordonner en cliquant sur le header"), même principe
// que seriesTableSort côté library.js: colonne active + sens, reclique la même colonne
// pour inverser. column === null revient au tri par pertinence par défaut
// (compareSearchResults, déjà appliqué à _searchTableAllResults à la construction).
let _searchTableSort = { column: null, direction: 'asc' };

function _searchResultSortValue(result, column) {
    switch (column) {
        case 'owned': return _isResultOwned(result) ? 1 : 0;
        case 'filename': return _searchResultDisplayName(result).toLowerCase();
        case 'volume': return (result.parsed_volume || '').toLowerCase();
        case 'source': return _searchResultSourceLabel(result.source).toLowerCase();
        case 'format': return detectResultFormat(result);
        case 'resolution': return detectResultResolution(result) || 0;
        case 'size': return result.size || 0;
        // Seeds/peers: nombre de seeds pour Prowlarr, nombre de sources ed2k réelles pour
        // EBDZ une fois connues (voir _loadEd2kAvailability/_ed2kSources - "ensuite on
        // pourra ordonner par nombre de seed comme pour les torrents"), -1 tant qu'inconnu
        // ou pour Telegram (pas de notion de disponibilité) - retombe en fin de tri
        // croissant plutôt que de se mêler aux vraies valeurs.
        case 'seeds':
            if (result.source === 'prowlarr') return result.seeders || 0;
            if (result.source === 'ebdz') return result._ed2kSources ?? -1;
            return -1;
        default: return 0;
    }
}

function _compareSearchResultsByColumn(a, b, column, direction) {
    const va = _searchResultSortValue(a, column);
    const vb = _searchResultSortValue(b, column);
    const cmp = (typeof va === 'number' && typeof vb === 'number')
        ? va - vb
        : String(va).localeCompare(String(vb), 'fr', { numeric: true, sensitivity: 'base' });
    return direction === 'asc' ? cmp : -cmp;
}

// Met à jour uniquement les flèches ▲/▼ des en-têtes triables (sans reconstruire tout le
// <thead>, ce qui réinitialiserait les <select> de filtre Source/Format/Taille et leur
// valeur déjà choisie par l'utilisateur).
function _updateSearchTableSortIndicators() {
    document.querySelectorAll('#search-results-table [data-sort-column]').forEach(el => {
        const active = el.dataset.sortColumn === _searchTableSort.column;
        el.classList.toggle('search-results-sort-active', active);
        const arrow = el.querySelector('.search-results-sort-arrow');
        if (arrow) arrow.textContent = active ? (_searchTableSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ↕';
    });
}

function _setSearchTableSort(column) {
    if (_searchTableSort.column === column) {
        _searchTableSort.direction = _searchTableSort.direction === 'asc' ? 'desc' : 'asc';
    } else {
        _searchTableSort.column = column;
        _searchTableSort.direction = 'asc';
    }
    _updateSearchTableSortIndicators();
    _renderSearchResultsTbody();
}

function _searchResultsSortableLabelHtml(column, label) {
    return `<span class="search-results-sort-label" data-sort-column="${column}" onclick="_setSearchTableSort('${column}')">${label}<span class="search-results-sort-arrow"></span></span>`;
}

// En-tête label triable + filtre de colonne, sur la même ligne et replié sur une icône
// loupe par défaut (voir .th-filterable-* dans style-library-search.css, pattern commun
// aux 3 tableaux filtrables de l'app) - factorisé ici pour ne pas répéter le même
// enrobage 5 fois dans buildSearchResultsTableHtml.
function _searchResultsFilterHeaderHtml(sortColumn, sortLabel, controlHtml) {
    return `
        <div class="th-filterable-row">
            ${_searchResultsSortableLabelHtml(sortColumn, sortLabel)}
            <span class="th-filterable-filter">
                <span class="th-filterable-icon" onclick="this.nextElementSibling.focus()">${svgIcon('filter')}</span>
                ${controlHtml}
            </span>
        </div>
    `;
}

// Table complète (avec en-têtes), triée par pertinence - wrapper commun réutilisé par
// displaySearchResults (library.js) et search.js. Retourne
// une chaîne vide si `results` est vide: à l'appelant de décider du message "aucun
// résultat" adapté à son contexte plutôt que de figer un seul message ici.
// seriesId/volumeId/volumeNumber: identité RÉELLE déjà connue quand la recherche part
// d'une fiche série (voir _searchResultsContextSeriesId ci-dessus) - absents en recherche
// libre (/search, discover.js). "if I search volume 4 from the serie search then I should
// get its number" - volumeNumber part directement du contexte de la recherche (le tome
// demandé), jamais redevine plus tard depuis un nom de fichier.
// preserveState: "quand prowlarr est mis à jour dans la recherche ça reset tous mes
// changements. comme si je filtre un volume ou la checkbox" - EBDZ/Telegram répondent
// vite, Prowlarr traîne (voir searchSources/searchMissingVolume, qui rappellent cette
// fonction à chaque source qui répond pour afficher les résultats déjà là - "tu peux
// charger les recherches instantanément"), et jusqu'ici CHAQUE rappel réinitialisait
// filtres/tri/checkbox sans distinction avec une VRAIE nouvelle recherche. true: garde
// _searchTableFilters/_searchTableSort tels quels (un rappel de la MÊME recherche, pas
// une nouvelle) et pré-remplit les contrôles avec leur valeur actuelle au lieu de les
// reconstruire vides - la liste de résultats grandit, mais rien que l'utilisateur a
// déjà choisi ne bouge. Reste false par défaut (recherche libre /search, un seul appel,
// pas de notion de "même recherche" à préserver entre deux appels différents).
function buildSearchResultsTableHtml(results, volumeNumber = null, seriesId = null, volumeId = null, preserveState = false, isReplacement = false) {
    if (!results || results.length === 0) return '';
    _searchTableAllResults = [...results].sort(compareSearchResults);
    if (!preserveState) {
        _searchTableFilters = { source: '', format: '', size: '', volume: '', title: '', hideUnconfirmed: true, owned: '' };
        _searchTableSort = { column: null, direction: 'asc' };
    }
    _searchResultsContextSeriesId = seriesId;
    _searchResultsContextVolumeId = volumeId;
    _searchResultsContextVolumeNumber = volumeNumber;
    _searchResultsContextIsReplacement = isReplacement;

    // Fire-and-forget: complète la colonne Seeds/Peers des lignes EBDZ une fois
    // ed2k.shortypower.org interrogé, sans retarder le rendu du tableau lui-même (voir
    // _loadEd2kAvailability).
    _loadEd2kAvailability(_searchTableAllResults);

    // "dans la recherche de série met une icône quand j'ai déjà le volume" - seulement
    // pour une recherche "série entière" (volumeNumber === null): une recherche d'UN
    // tome précis n'a par définition rien à comparer (on sait déjà qu'il manque). Pas
    // rechargé sur un rappel préservé (preserveState, même seriesId/volumeNumber donc
    // même réponse) pour ne pas répéter la requête à chaque source qui répond.
    if (seriesId && volumeNumber === null && !preserveState) {
        _searchTableOwnedLabels = null;
        _searchTableOneshotOwned = false;
        _loadOwnedVolumeLabels(seriesId);
    } else if (!seriesId || volumeNumber !== null) {
        _searchTableOwnedLabels = null;
        _searchTableOneshotOwned = false;
    }

    const sourceOptions = [...new Set(_searchTableAllResults.map(r => r.source))];
    const formatOptions = [...new Set(_searchTableAllResults.map(detectResultFormat))];
    // "met un filtre pour choisir le volume" - options limitées à ce qui est réellement
    // présent dans CETTE recherche (comme Source/Format), pas une liste figée de tomes
    // possibles: n'a de sens que pour une recherche série entière (plusieurs volumes
    // différents remontent), une recherche d'un tome précis ne montre presque toujours
    // qu'une seule valeur ici.
    const volumeOptions = [...new Set(_searchTableAllResults.map(r => r.parsed_volume).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b, 'fr', { numeric: true, sensitivity: 'base' }));

    // Pré-remplit chaque contrôle avec sa valeur actuelle (identique à '' juste après un
    // reset) - c'est ce qui permet à un filtre déjà choisi de rester visible et actif
    // après un rappel préservé (preserveState) plutôt que de réapparaître vide.
    const f = _searchTableFilters;
    const activeClass = (v) => v ? ' has-value' : '';

    // "la checkbox devrait s'afficher uniquement pour la recherche de volume. pas de la
    // série entière" - unconfirmed_volume n'a de sens que face à un tome précis demandé
    // (_confirms_requested_volume, searcher.py, renvoie toujours confirmé pour une
    // recherche "série entière" sans numéro) - la case n'aurait littéralement aucun
    // résultat à filtrer dans ce cas.
    const hideUnconfirmedCheckboxHtml = volumeNumber !== null ? `
        <label style="display:inline-flex; align-items:center; gap:6px; margin-bottom:8px; font-size:0.9em; cursor:pointer;" data-tooltip="Masquer les résultats dont le titre ne confirme pas le tome/l'album recherché">
            <input type="checkbox" ${f.hideUnconfirmed ? 'checked' : ''} onchange="_applySearchTableFilter('hideUnconfirmed', this.checked)">
            Bon volume uniquement
        </label>
    ` : '';
    // "je ne veux pas de checkbox je veux juste le filtre dans le header comme pour
    // fichier ou volume" - même contrôle de colonne que Fichier/Volume/Source/Format/
    // Taille (_searchResultsFilterHeaderHtml) plutôt qu'un contrôle isolé au-dessus du
    // tableau. Seulement pour une recherche "série entière" avec une série connue, la
    // seule situation où _searchTableOwnedLabels est alimenté (voir isOwned,
    // buildSearchResultRowHtml) - sinon l'en-tête reste une colonne vide comme avant.
    const ownedColumnHeaderHtml = (volumeNumber === null && seriesId)
        ? _searchResultsFilterHeaderHtml('owned', 'Possédé',
            `<select id="search-results-filter-owned" aria-label="Filtrer par possession" class="search-results-filter-select th-filterable-control${activeClass(f.owned)}" data-tooltip="Filtrer par possession" onchange="_syncFilterControlActive(this); _applySearchTableFilter('owned', this.value)">
                <option value="">Tout</option>
                <option value="missing"${f.owned === 'missing' ? ' selected' : ''}>✗</option>
                <option value="owned"${f.owned === 'owned' ? ' selected' : ''}>✓</option>
            </select>`)
        : '';

    return `
        ${hideUnconfirmedCheckboxHtml}
        <div style="overflow-x:auto;">
            <table class="replace-results-table" id="search-results-table">
                <thead>
                    <tr>
                        <th>${ownedColumnHeaderHtml}</th>
                        <th>
                            ${_searchResultsFilterHeaderHtml('filename', 'Fichier',
                                `<input type="text" id="search-results-filter-filename" aria-label="Filtrer par titre" class="search-results-filter-input th-filterable-control${activeClass(f.title)}" value="${escapeHtml(f.title)}" data-tooltip="Filtrer par titre" placeholder="Filtrer..." oninput="_syncFilterControlActive(this); _applySearchTableFilter('title', this.value)">`)}
                        </th>
                        <th>
                            ${_searchResultsFilterHeaderHtml('volume', 'Volume',
                                `<select id="search-results-filter-volume" aria-label="Filtrer par volume" class="search-results-filter-select th-filterable-control${activeClass(f.volume)}" data-tooltip="Filtrer par volume" onchange="_syncFilterControlActive(this); _applySearchTableFilter('volume', this.value)">
                                    <option value="">Tout</option>
                                    ${volumeOptions.map(v => `<option value="${escapeHtml(v)}"${f.volume === v ? ' selected' : ''}>${escapeHtml(v)}</option>`).join('')}
                                </select>`)}
                        </th>
                        <th>
                            ${_searchResultsFilterHeaderHtml('source', 'Source',
                                `<select id="search-results-filter-source" aria-label="Filtrer par source" class="search-results-filter-select th-filterable-control${activeClass(f.source)}" data-tooltip="Filtrer par source" onchange="_syncFilterControlActive(this); _applySearchTableFilter('source', this.value)">
                                    <option value="">Tout</option>
                                    ${sourceOptions.map(s => `<option value="${escapeHtml(s)}"${f.source === s ? ' selected' : ''}>${escapeHtml(_searchResultSourceLabel(s))}</option>`).join('')}
                                </select>`)}
                        </th>
                        <th>
                            ${_searchResultsFilterHeaderHtml('format', 'Format',
                                `<select id="search-results-filter-format" aria-label="Filtrer par format" class="search-results-filter-select th-filterable-control${activeClass(f.format)}" data-tooltip="Filtrer par format" onchange="_syncFilterControlActive(this); _applySearchTableFilter('format', this.value)">
                                    <option value="">Tout</option>
                                    ${formatOptions.map(fmt => `<option value="${escapeHtml(fmt)}"${f.format === fmt ? ' selected' : ''}>${escapeHtml(fmt)}</option>`).join('')}
                                </select>`)}
                        </th>
                        <th>${_searchResultsSortableLabelHtml('resolution', 'Résolution')}</th>
                        <th>
                            ${_searchResultsFilterHeaderHtml('size', 'Taille',
                                `<select id="search-results-filter-size" aria-label="Filtrer par taille" class="search-results-filter-select th-filterable-control${activeClass(f.size)}" data-tooltip="Filtrer par taille" onchange="_syncFilterControlActive(this); _applySearchTableFilter('size', this.value)">
                                    ${SEARCH_SIZE_BUCKETS.map(b => `<option value="${b.value}"${f.size === b.value ? ' selected' : ''}>${b.label}</option>`).join('')}
                                </select>`)}
                        </th>
                        <th>${_searchResultsSortableLabelHtml('seeds', 'Seeds/Peers')}</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="search-results-tbody">
                    ${_filteredSearchTableResults().map(result => buildSearchResultRowHtml(result)).join('')}
                </tbody>
            </table>
        </div>
    `;
}
