// Utilitaires d'action de téléchargement partagés entre /discover et /ebdz-nouveautes
// (voir templates/discover.html, templates/ebdz-latest.html) - ce fichier hébergeait à
// l'origine le contrôleur d'une page /search autonome (recherche EBDZ/Prowlarr/Bédéthèque
// avec son propre champ #searchInput/#results/#filterBox), supprimée depuis; seules les
// fonctions ci-dessous restent réellement utilisées par les deux pages qui chargent encore
// ce script. escapeHtml/escapeForAttribute en particulier sont dépendues globalement par
// ebdz-latest.js, qui ne définit pas sa propre copie.

function formatBytes(bytes) {
    if (!bytes) return 'N/A';
    const b = parseInt(bytes);
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
}

// Fonction pour décoder les caractères encodés (comme %20) et les entités HTML
// résiduelles (comme &amp;) sur un nom scrapé avant la correction du scraper EBDZ -
// voir la copie commentée côté static/js/history-shared.js (même logique, gardée en
// synchro manuellement, ce fichier n'étant jamais chargé avec history-shared.js).
function decodeFilename(filename) {
    if (!filename) return filename;
    let decoded = filename;
    try {
        decoded = decodeURIComponent(decoded);
    } catch (e) {
        // Si le décodage échoue, garder la chaîne telle quelle
    }
    return decoded.replace(/&amp;|&lt;|&gt;|&quot;|&#0?39;|&apos;/g, m => ({
        '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#039;': "'", '&apos;': "'"
    }[m]));
}

// Fonction pour échapper les caractères spéciaux dans les attributs HTML
function escapeForAttribute(text) {
    return String(text ?? '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// Fonction pour échapper le HTML (contenu texte inséré via innerHTML) : les données
// scrapées depuis le forum EBDZ (titre/description/nom de fichier) ne sont pas fiables
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

async function copyLink(link, button) {
    try {
        await navigator.clipboard.writeText(link);
        button.textContent = '✓ Copié!';
        button.classList.add('copied');
        setTimeout(() => {
            // innerHTML (pas textContent) et pas de libellé "Copier": tous les boutons
            // qui appellent copyLink() sur les pages chargeant search.js sont des
            // btn-icon-only (voir search-results-table.js/ebdz-latest.js), l'icône seule
            // est donc le bon état de repos - textContent effacerait aussi l'icône SVG
            button.innerHTML = svgIcon('copy');
            button.classList.remove('copied');
        }, 2000);
    } catch (error) {
        alert('Erreur lors de la copie: ' + error);
    }
}

async function addToEmule(link, button, title, seriesId = null, volumeId = null, volumeNumber = null, source = 'ebdz', sourceLink = null, forceReplace = false) {
    const originalHtml = button.innerHTML;
    button.innerHTML = '<span class="btn-icon">⏳</span>';
    button.disabled = true;

    try {
        const response = await fetch('/api/emule/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({link: link, title: title, series_id: seriesId, volume_id: volumeId, volume_number: volumeNumber, source, source_link: sourceLink, force_replace: forceReplace})
        });

        const data = await response.json();

        if (data.success) {
            // Reste affiché en permanence (pas de retour à l'état initial): permet de
            // retrouver au scroll ce qui a déjà été ajouté dans une longue liste. Persisté
            // aussi sur le résultat lui-même (voir _markSearchResultAdded) - "quand je
            // rajoute un fichier a télécharger et appuie sur l'ordre je perds le bouton
            // just added": sans ça, trier une colonne reconstruit le tbody et oublie cet
            // état purement DOM.
            if (typeof _markSearchResultAdded === 'function') _markSearchResultAdded(link, 'amule');
            button.innerHTML = '<span class="btn-icon">✓</span>';
            button.classList.add('add-button-added');
            button.setAttribute('data-tooltip', 'Ajouté à aMule');
        } else {
            throw new Error(data.error || 'Erreur inconnue');
        }
    } catch (error) {
        button.innerHTML = '<span class="btn-icon">✗</span>';
        button.classList.add('add-button-error');
        alert('Erreur: ' + error.message);
        setTimeout(() => {
            button.innerHTML = originalHtml;
            button.disabled = false;
            button.classList.remove('add-button-error');
        }, 3000);
    }
}

async function downloadTelegramFile(channel, messageId, button, channelTitle, filename, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳';
    button.disabled = true;

    try {
        const response = await fetch('/api/telegram-channels/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // filename (optionnel, déjà connu de l'appelant à ce stade - voir
            // ebdz-latest.js/search-results-table.js) - sert de titre à la ligne "en
            // attente" sur /import (voir mark_download_pending côté Flask), avant que le
            // vrai nom de fichier ne soit confirmé une fois le téléchargement terminé.
            // source fixe 'telegram': ce bouton n'est jamais rendu pour une autre source
            // (voir search-results-table.js).
            body: JSON.stringify({
                channel, message_id: messageId, channel_title: channelTitle, filename,
                series_id: seriesId, volume_id: volumeId, volume_number: volumeNumber,
                source: 'telegram', source_link: sourceLink, force_replace: forceReplace
            })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Erreur inconnue');

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

// Télécharge un résultat fourtoutici.cc directement dans un répertoire d'import
// surveillé (voir FOURTOUTICI_IMPORT_DIRECTORY, config.py) - même contrat que
// downloadTelegramFile ci-dessus, mais avec un lien HTTP direct connu (fourtoutici n'a
// pas besoin d'une session applicative persistante comme Telegram/Telethon) : marqué
// "déjà ajouté" via _markSearchResultAdded (link-based) plutôt que _markTelegramResultAdded
// (channel/message_id-based), puisque ce lien existe réellement ici.
async function downloadFourtoutici(fileId, button, filename, seriesId = null, volumeId = null, volumeNumber = null, downloadUrl = null, forceReplace = false) {
    const originalHtml = button.innerHTML;
    button.innerHTML = '⏳';
    button.disabled = true;

    try {
        const response = await fetch('/api/fourtoutici/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // source fixe 'fourtoutici': ce bouton n'est jamais rendu pour une autre
            // source (voir search-results-table.js), pas de source_link (pas de page de
            // release distincte du lien de téléchargement pour ce site).
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

async function checkEmuleStatus() {
    try {
        const response = await fetch('/api/emule/config');
        const config = await response.json();

        // .result-action-emule (voir search-results-table.js) depuis le passage au
        // tableau compact partagé - ne touche pas aux boutons qBittorrent
        const addButtons = document.querySelectorAll('.result-action-emule');
        addButtons.forEach(button => {
            button.style.display = config.enabled ? 'inline-block' : 'none';
        });
    } catch (error) {
        console.error('Erreur lors de la vérification du statut aMule:', error);
    }
}

// Ajouter un torrent à qBittorrent avec la catégorie par défaut. Partagé entre le bouton
// icône seule 32x32 de cette page (.add-button-qbit) et le bouton icône+texte de la page
// Découvrir (.btn-download): le contenu affiché à chaque étape s'adapte en conséquence
async function addTorrentToQbittorrent(torrentUrl, button, title, seriesId = null, volumeId = null, volumeNumber = null, sourceLink = null, forceReplace = false) {
    const originalHtml = button.innerHTML;
    const isIconOnly = button.classList.contains('add-button');
    button.innerHTML = isIconOnly ? '<span class="btn-icon">⏳</span>' : '⏳ Envoi...';
    button.disabled = true;

    try {
        // Charger la config pour obtenir la catégorie par défaut
        const configResponse = await fetch('/api/qbittorrent/config');
        const config = await configResponse.json();

        // "pourquoi ne matche pas l'album... ca devrait marcher pour les imports de
        // cette page" - buildSearchResultRowHtml (search-results-table.js, partagé)
        // transmet déjà seriesId/volumeId/volumeNumber dans l'onclick, mais cette
        // fonction (et ses équivalentes ci-dessous) les ignorait silencieusement faute
        // de les déclarer en paramètres - aucun series_id n'était donc jamais envoyé à
        // mark_download_pending, quelle que soit la page d'origine (search.js est
        // partagé par /search ET /discover).
        const payload = {
            torrent_url: torrentUrl,
            title: title,
            series_id: seriesId,
            volume_id: volumeId,
            volume_number: volumeNumber,
            source: 'prowlarr',
            source_link: sourceLink,
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
            button.innerHTML = isIconOnly ? '<span class="btn-icon">✓</span>' : '✓ Ajouté!';
            button.classList.add('add-button-added');
            if (isIconOnly) button.setAttribute('data-tooltip', 'Ajouté à qBittorrent');
        } else {
            throw new Error(data.error || 'Erreur inconnue');
        }
    } catch (error) {
        button.innerHTML = isIconOnly ? '<span class="btn-icon">✗</span>' : '✗ Erreur';
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
    const originalHtml = button.innerHTML;
    const isIconOnly = button.classList.contains('add-button');
    button.innerHTML = isIconOnly ? '<span class="btn-icon">⏳</span>' : '⏳ Envoi...';
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
            button.innerHTML = isIconOnly ? '<span class="btn-icon">✓</span>' : '✓ Ajouté!';
            button.classList.add('add-button-added');
            if (isIconOnly) button.setAttribute('data-tooltip', `Ajouté à ${clientLabel}`);
        } else {
            throw new Error(data.error || 'Erreur inconnue');
        }
    } catch (error) {
        button.innerHTML = isIconOnly ? '<span class="btn-icon">✗</span>' : '✗ Erreur';
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

// Charge le statut au démarrage
checkEmuleStatus();
