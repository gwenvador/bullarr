// Thème clair/sombre (voir /settings, onglet "🌓 Thème"). Le choix est déjà appliqué
// avant même ce script - un petit script inline dans le <head> de chaque page lit
// localStorage et pose data-theme="dark" sur <html> avant le premier rendu, pour éviter
// un flash clair->sombre au chargement (nav.js est chargé en fin de <body>, trop tard
// pour ça). Ces deux fonctions gèrent le changement à chaud (bouton dans /settings, sans
// recharger la page) et la lecture de l'état courant pour synchroniser le sélecteur.
function setTheme(mode) {
    if (mode === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    try {
        localStorage.setItem('theme', mode);
    } catch (e) {
        // stockage indisponible - le thème reste appliqué pour la session en cours,
        // juste pas persisté ni repris sur une autre page/au prochain chargement
    }
    _updateHeaderThemeToggle(mode);
    // /settings a son propre sélecteur clair/sombre (rendu par settings.js, absent des
    // autres pages) - le tenir synchronisé aussi si présent, pour que basculer depuis le
    // bouton de l'en-tête ne le laisse pas affichant l'ancien mode.
    if (typeof _updateThemeSwitchUI === 'function') _updateThemeSwitchUI(mode);
}

function getTheme() {
    try {
        return localStorage.getItem('theme') === 'dark' ? 'dark' : 'light';
    } catch (e) {
        return 'light';
    }
}

// Bouton clair/sombre injecté à droite de l'en-tête de TOUTES les pages (même mécanisme
// que initHeaderSearch/initUserBadge plus bas: un enfant de plus ajouté à .header-content,
// poussé à droite par .header-left en flex:1) - évite d'avoir à passer par /settings juste
// pour changer de thème. L'icône affichée est celle du mode qu'un clic activerait (lune en
// clair, soleil en sombre), pas le mode courant.
function initThemeToggle() {
    const headerContent = document.querySelector('.header-content');
    if (!headerContent || document.querySelector('.header-theme-toggle')) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'header-search-btn header-theme-toggle';
    btn.addEventListener('click', () => setTheme(getTheme() === 'dark' ? 'light' : 'dark'));
    headerContent.appendChild(btn);
    _updateHeaderThemeToggle(getTheme());
}

function _updateHeaderThemeToggle(mode) {
    const btn = document.querySelector('.header-theme-toggle');
    if (!btn) return;
    btn.innerHTML = svgIcon(mode === 'dark' ? 'sun' : 'moon');
    btn.setAttribute('data-tooltip', mode === 'dark' ? 'Passer en mode clair' : 'Passer en mode sombre');
}

// Cache global "quel client de téléchargement est activé" (aMule, qBittorrent, rTorrent,
// Deluge), rempli une fois au chargement de la page - "affiche dans les téléchargements
// juste les clients qui sont activés": les boutons "Envoyer à ..." des résultats de
// recherche (search-results-table.js, utilisé par Recherche/Découvrir/fiche série) et de
// la page Surveillance (missing-monitor.js, rendu indépendant) lisent tous les deux ce
// même cache plutôt que de refaire chacun leurs propres appels /config - posé ici (nav.js)
// car c'est le seul script chargé sur TOUTES les pages, contrairement à
// search-results-table.js qui n'est pas inclus par missing-monitor.html.
// Avant que le cache soit rempli, tout est considéré désactivé (aucun bouton affiché)
// plutôt que l'inverse: une recherche déclenchée avant la fin du chargement de la page ne
// doit jamais montrer un bouton pour un client qui se révèle ensuite désactivé.
const enabledDownloadClients = { amule: false, qbittorrent: false, rtorrent: false, deluge: false };

async function refreshEnabledDownloadClients() {
    const endpoints = {
        amule: '/api/emule/config',
        qbittorrent: '/api/qbittorrent/config',
        rtorrent: '/api/rtorrent/config',
        deluge: '/api/deluge/config'
    };
    await Promise.all(Object.entries(endpoints).map(async ([key, url]) => {
        try {
            const response = await fetch(url);
            const config = await response.json();
            enabledDownloadClients[key] = !!config.enabled;
        } catch (e) {
            enabledDownloadClients[key] = false;
        }
    }));
}

// "make sure that if komga or ebdz is not configured they dont show up in the table or
// the settings with matching" / "j'ai désactivé prowlarr mais il s'affiche toujours dans
// découvrir et rechercher" - même pattern que enabledDownloadClients ci-dessus: rempli
// une fois au chargement, consulté par library.js (colonnes "Matching EBDZ"/"Matching
// Komga" du tableau bibliothèque, boutons de la fiche série) et discover.js/search.js
// (cases à cocher/boutons de source). Komga et Prowlarr ont un interrupteur "enabled"
// dédié (voir /api/komga/config, /api/prowlarr/config) ; EBDZ n'en a pas, "configuré"
// veut dire identifiants renseignés (voir is_ebdz_configured côté Flask, même critère
// que /api/ebdz/scrape) - le mot de passe renvoyé est masqué ('****') mais toujours une
// chaîne non vide si un mot de passe existe, donc utilisable tel quel ici.
const enabledIntegrations = { komga: false, ebdz: false, prowlarr: false, telegram: false, fourtoutici: true, annas_archive: true };

async function refreshEnabledIntegrations() {
    try {
        const [komgaRes, ebdzRes, prowlarrRes, telegramRes, fourtouticiRes, annasArchiveRes] = await Promise.all([
            fetch('/api/komga/config'),
            fetch('/api/ebdz/config'),
            fetch('/api/prowlarr/config'),
            fetch('/api/telegram-channels/config'),
            fetch('/api/fourtoutici/config'),
            fetch('/api/annas-archive/config'),
        ]);
        const komgaConfig = await komgaRes.json();
        const ebdzConfig = await ebdzRes.json();
        const prowlarrConfig = await prowlarrRes.json();
        const telegramConfig = await telegramRes.json();
        const fourtouticiConfig = await fourtouticiRes.json();
        const annasArchiveConfig = await annasArchiveRes.json();
        enabledIntegrations.komga = !!komgaConfig.enabled;
        enabledIntegrations.ebdz = !!(ebdzConfig.username && ebdzConfig.password);
        enabledIntegrations.prowlarr = !!prowlarrConfig.enabled;
        enabledIntegrations.telegram = !!telegramConfig.connected;
        // Activé par défaut si la config n'a pas encore répondu/existe pas (source
        // publique sans identifiants, voir FOURTOUTICI_CONFIG côté config.py) plutôt que
        // masqué par défaut comme Prowlarr - il n'y a rien à configurer avant de l'utiliser.
        enabledIntegrations.fourtoutici = fourtouticiConfig.enabled !== false;
        enabledIntegrations.annas_archive = annasArchiveConfig.enabled !== false;
    } catch (e) {
        enabledIntegrations.komga = false;
        enabledIntegrations.ebdz = false;
        enabledIntegrations.prowlarr = false;
        enabledIntegrations.telegram = false;
        enabledIntegrations.fourtoutici = true;
        enabledIntegrations.annas_archive = true;
    }
}

// Tooltips (data-tooltip): un seul élément partagé ajouté à <body> et positionné en
// fixed, pour échapper aux ancêtres à overflow:hidden/auto (.container, .content...) qui
// coupaient sinon le tooltip dès qu'un bouton était proche d'un bord (barre latérale,
// bord droit de la fenêtre...). Voir style.css #js-tooltip pour l'apparence.
let jsTooltipEl = null;

function getJsTooltipEl() {
    if (!jsTooltipEl) {
        jsTooltipEl = document.createElement('div');
        jsTooltipEl.id = 'js-tooltip';
        document.body.appendChild(jsTooltipEl);
    }
    return jsTooltipEl;
}

function showJsTooltip(target) {
    const text = target.getAttribute('data-tooltip');
    if (!text) return;
    const tooltip = getJsTooltipEl();
    tooltip.textContent = text;
    tooltip.style.display = 'block';

    const rect = target.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - tooltipRect.width / 2;
    left = Math.max(6, Math.min(left, window.innerWidth - tooltipRect.width - 6));
    // Au-dessus de la cible par défaut, bascule en dessous s'il n'y a pas la place
    // (ex: bouton tout en haut de la page)
    let top = rect.top - tooltipRect.height - 6;
    if (top < 6) top = rect.bottom + 6;

    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
}

function hideJsTooltip() {
    if (jsTooltipEl) jsTooltipEl.style.display = 'none';
}

document.addEventListener('mouseover', (e) => {
    const target = e.target.closest('[data-tooltip]');
    if (target) showJsTooltip(target);
});

document.addEventListener('mouseout', (e) => {
    const target = e.target.closest('[data-tooltip]');
    if (target && !target.contains(e.relatedTarget)) hideJsTooltip();
});

document.addEventListener('focusin', (e) => {
    const target = e.target.closest('[data-tooltip]');
    if (target) showJsTooltip(target);
});

document.addEventListener('focusout', (e) => {
    if (e.target.closest('[data-tooltip]')) hideJsTooltip();
});

// Évite un tooltip affiché au mauvais endroit après un scroll/clic sans mouseout
// intermédiaire (ex: molette de souris pendant que le tooltip est affiché)
document.addEventListener('scroll', hideJsTooltip, true);
document.addEventListener('click', hideJsTooltip, true);

// Fermeture générique des modals (.modal.active/.show) au clic sur le fond ou à Échap,
// commune à toutes les pages puisque nav.js y est chargé partout. Certains modals ont un
// nettoyage spécifique en plus de se cacher (reset de formulaire, variables d'état...):
// on appelle leur fonction de fermeture dédiée quand elle existe plutôt que de juste
// retirer la classe, pour se comporter exactement comme un clic sur leur bouton ✕
const MODAL_CLOSE_FUNCTIONS = {
    'ebdz-match-modal': 'closeEbdzMatchModal',
    'komga-match-modal': 'closeKomgaMatchModal',
    'bedetheque-match-modal': 'closeBedethequeMatchModal',
    'ebdz-files-modal': 'closeEbdzFilesModal',
    'search-ed2k-modal': 'closeSearchModal',
    'series-modal': 'closeModal',
    'rename-modal': 'closeRenameModal',
    'select-destination-modal': 'closeDestinationModal',
    // Doit résoudre la Promise en attente (voir openCbzPackagingModeModal, import.js) sinon
    // un clic en dehors / Échap laisserait le bouton "empaqueter" bloqué en spinner pour
    // toujours - un simple retrait de .active ne suffit pas ici.
    'cbz-packaging-mode-modal': 'closeCbzPackagingModeModal',
    'create-modal': 'closeCreateModal',
    'download-modal': 'closeDownloadModal',
    // Modales de configuration Indexeurs/Clients (settings.js, openIntegrationModal) -
    // fonctions zéro-argument dédiées car closeOpenModal appelle window[fnName]() sans
    // paramètre, alors que closeIntegrationModal(name) en a besoin
    'tab-amule': 'closeAmuleModal',
    'tab-ebdz': 'closeEbdzConfigModal',
    'tab-prowlarr': 'closeProwlarrModal',
    'tab-qbittorrent': 'closeQbittorrentModal',
    'tab-rtorrent': 'closeRtorrentModal',
    'tab-deluge': 'closeDelugeModal',
    'tab-komga': 'closeKomgaConfigModal',
    'tab-web': 'closeWebSourcesModal',
    'tab-telegram-channels': 'closeTelegramChannelsModal',
    'upload-volume-confirm-modal': 'closeUploadVolumeConfirmModal',
};

function closeOpenModal(modalEl) {
    const fnName = MODAL_CLOSE_FUNCTIONS[modalEl.id];
    if (fnName && typeof window[fnName] === 'function') {
        window[fnName]();
    } else {
        modalEl.classList.remove('active', 'show');
    }
}

document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.modal.active, .modal.show').forEach(closeOpenModal);
});

// Clic sur le fond assombri (en dehors de .modal-content): e.target est le .modal
// lui-même uniquement quand le clic n'a touché aucun enfant (modal-content en absorbe
// la propagation visuellement puisqu'il occupe une zone distincte)
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal') && (e.target.classList.contains('active') || e.target.classList.contains('show'))) {
        closeOpenModal(e.target);
    }
});

// Enveloppe les enfants directs de .sidebar-nav (liens de premier niveau + éventuel
// #settings-nav-subgroup) dans un conteneur .sidebar-nav-scroll injecté ici plutôt que
// posé dans chaque template (markup dupliqué dans 13 pages) - voir le commentaire sur
// .sidebar-nav/.sidebar-nav-scroll dans style.css: .sidebar-nav reste un simple bloc qui
// s'étire à la hauteur de toute la page (fond/bordure continus jusqu'en bas), tandis que
// ce conteneur porte le position:sticky qui garde les liens + le bouton replier ancrés à
// l'écran pendant le défilement. Doit tourner avant tout le reste (les fonctions
// ci-dessous ciblent .sidebar-nav-scroll pour les liens/l'ajout du bouton replier).
function wrapSidebarNavLinks() {
    const sidebar = document.querySelector('.sidebar-nav');
    if (!sidebar || sidebar.querySelector(':scope > .sidebar-nav-scroll')) return;

    const scroll = document.createElement('div');
    scroll.className = 'sidebar-nav-scroll';
    while (sidebar.firstChild) {
        scroll.appendChild(sidebar.firstChild);
    }
    sidebar.appendChild(scroll);
}

// Regroupe une liste de liens de premier niveau (présents sur les 14 templates) sous un
// nouveau parent avec sous-menu repliable - même pattern que Configuration/
// #settings-nav-subgroup (voir initConfigSubgroupToggle), mais construit dynamiquement ici
// plutôt que dupliqué dans chaque template, puisque ces regroupements doivent apparaître
// sur TOUTES les pages (contrairement au sous-menu Configuration qui n'existe que dans le
// HTML de /settings). Doit tourner après wrapSidebarNavLinks et avant la collecte de
// navLinks ci-dessous, qui doit voir la structure finale.
// hrefs: URLs (dans l'ordre voulu) des liens à regrouper. id/storageKey: identifiants
// propres à ce groupe (un par appel - "Activité" et "Actions" coexistent, ne doivent pas
// se marcher dessus dans le DOM ni dans localStorage). insertAfterHref (optionnel): place
// le nouveau lien parent juste après ce href plutôt qu'à la position du premier élément de
// hrefs ("met activités apres decouvrir" - la position naturelle du 1er lien regroupé,
// Import, était avant Découvrir, pas après).
// "la configuration reste ouverte quand je clique sur une autre entrée, ferme celles qui
// ne sont pas pertinentes" - Activité/Actions/Configuration sont 3 sous-menus indépendants
// (chacun son propre état, voir storageKey ci-dessous) qui pouvaient donc rester dépliés
// simultanément - un groupe resté ouvert sur une page (persisté dans localStorage) restait
// affiché déplié même après avoir navigué vers un tout autre groupe, sans rapport. Un seul
// sous-menu ouvert à la fois (accordéon): repère TOUS les .nav-subgroup du DOM (Activité/
// Actions toujours présents, Configuration seulement présent sur /settings) et referme
// tous ceux qui ne sont pas `exceptId`, y compris leur état persisté (data-storage-key,
// posé par initNavGroup ci-dessous) pour que ça reste vrai après un rechargement de page.
function _collapseOtherNavGroups(exceptId) {
    document.querySelectorAll('.nav-subgroup').forEach(subgroup => {
        if (subgroup.id === exceptId) return;
        subgroup.classList.add('nav-subgroup-collapsed');
        const storageKey = subgroup.dataset.storageKey;
        if (storageKey) {
            try { localStorage.setItem(storageKey, 'false'); } catch (e) { /* stockage indisponible */ }
        }
    });
}

function initNavGroup({ id, storageKey, label, hrefs, insertAfterHref }) {
    const scroll = document.querySelector('.sidebar-nav-scroll');
    if (!scroll || document.getElementById(id)) return;

    const links = hrefs.map(href => scroll.querySelector(`:scope > a[href="${href}"]`));
    if (links.some(link => !link)) return;

    // "si tu cliques sur activité ca fait rien. ca devrait ouvrir le premier lien" - un
    // vrai lien vers le premier href du groupe plutôt que href="#" (qui ne naviguait
    // jamais, preventDefault() systématique ci-dessous ne faisait que replier/déplier).
    const parent = document.createElement('a');
    parent.href = hrefs[0];
    parent.className = 'nav-link';
    parent.textContent = label;

    const subgroup = document.createElement('div');
    subgroup.className = 'nav-subgroup';
    subgroup.id = id;
    subgroup.dataset.storageKey = storageKey;

    const anchor = insertAfterHref ? scroll.querySelector(`:scope > a[href="${insertAfterHref}"]`) : null;
    if (anchor) {
        anchor.after(parent);
    } else {
        links[0].before(parent);
    }
    links.forEach(link => {
        link.classList.replace('nav-link', 'nav-sublink');
        subgroup.appendChild(link);
    });
    parent.after(subgroup);

    const isOnGroupPage = hrefs.includes(window.location.pathname);
    if (isOnGroupPage) parent.classList.add('active');

    let expanded = isOnGroupPage;
    try {
        expanded = isOnGroupPage || localStorage.getItem(storageKey) === 'true';
    } catch (e) {
        // stockage indisponible - replié par défaut sauf si on est déjà sur une des
        // pages regroupées
    }
    subgroup.classList.toggle('nav-subgroup-collapsed', !expanded);
    // Un seul sous-menu ouvert à la fois - voir _collapseOtherNavGroups. Ne referme les
    // autres que si CE groupe-ci finit réellement ouvert (sinon un groupe replié fermerait
    // à tort un autre groupe légitimement ouvert au chargement de la même page).
    if (expanded) _collapseOtherNavGroups(id);

    // Le clic navigue désormais réellement (parent.href = hrefs[0] ci-dessus) - déplie
    // en plus le groupe pour de bon (une fois sur la page, les autres liens du groupe
    // restent visibles juste en dessous au lieu de rester repliés) plutôt que l'ancien
    // comportement replie/déplie qui ne naviguait jamais nulle part.
    parent.addEventListener('click', () => {
        subgroup.classList.remove('nav-subgroup-collapsed');
        _collapseOtherNavGroups(id);
        try {
            localStorage.setItem(storageKey, 'true');
        } catch (err) {
            // stockage indisponible - la navigation fonctionne quand même
        }
    });

    _initNavGroupFlyout(parent, links);
}

// "en mode reduit je ne peux pas ouvrir les sous niveau. donc je ne peux pas acceder aux
// autres interfaces" - .nav-subgroup est display:none quand la sidebar est réduite
// (icônes seules, voir .sidebar-nav.collapsed .nav-subgroup dans style.css), sans autre
// moyen d'atteindre ses liens. Menu flottant au survol de l'icône du groupe, dans <body>
// en position:fixed (même technique que showJsTooltip plus haut) pour échapper à
// l'overflow-y:auto de .sidebar-nav-scroll qui couperait sinon un enfant position:absolute
// dépassant à droite de la sidebar réduite.
let _navGroupFlyoutEl = null;
let _navGroupFlyoutHideTimer = null;

function _getNavGroupFlyoutEl() {
    if (!_navGroupFlyoutEl) {
        _navGroupFlyoutEl = document.createElement('div');
        _navGroupFlyoutEl.id = 'nav-group-flyout';
        document.body.appendChild(_navGroupFlyoutEl);
        // Survoler le menu flottant lui-même (pour cliquer un de ses liens) ne doit pas
        // le refermer - seul un vrai départ de la zone (icône + menu) le referme, après
        // un court délai pour laisser le temps à la souris de traverser l'espace vide
        // entre l'icône et le menu.
        _navGroupFlyoutEl.addEventListener('mouseenter', () => clearTimeout(_navGroupFlyoutHideTimer));
        _navGroupFlyoutEl.addEventListener('mouseleave', _scheduleHideNavGroupFlyout);
    }
    return _navGroupFlyoutEl;
}

function _scheduleHideNavGroupFlyout() {
    clearTimeout(_navGroupFlyoutHideTimer);
    _navGroupFlyoutHideTimer = setTimeout(() => {
        if (_navGroupFlyoutEl) _navGroupFlyoutEl.style.display = 'none';
    }, 200);
}

function _initNavGroupFlyout(parent, links) {
    parent.addEventListener('mouseenter', () => {
        const sidebar = document.querySelector('.sidebar-nav');
        // Uniquement utile en mode réduit: en mode normal, .nav-subgroup est déjà visible
        // inline juste en dessous, pas besoin d'un second affichage flottant redondant.
        if (!sidebar || !sidebar.classList.contains('collapsed')) return;

        clearTimeout(_navGroupFlyoutHideTimer);
        const flyout = _getNavGroupFlyoutEl();
        flyout.innerHTML = '';
        links.forEach(link => flyout.appendChild(link.cloneNode(true)));
        flyout.style.display = 'block';

        const rect = parent.getBoundingClientRect();
        let top = rect.top;
        const flyoutRect = flyout.getBoundingClientRect();
        top = Math.min(top, window.innerHeight - flyoutRect.height - 6);
        flyout.style.left = `${rect.right}px`;
        flyout.style.top = `${Math.max(6, top)}px`;
    });
    parent.addEventListener('mouseleave', _scheduleHideNavGroupFlyout);
}

// "why actions and activité still opened even if I open only one" - initActivityGroup()
// tourne AVANT initActionsGroup(): quand on arrive sur une page d'Activité, son propre
// _collapseOtherNavGroups (voir initNavGroup) ne peut encore RIEN fermer chez Actions,
// qui n'existe pas encore dans le DOM à ce moment précis. Actions s'initialise ensuite et,
// si son localStorage était resté à 'true' depuis une autre page, se déplie à son tour et
// referme Activité en dernier - inversant le résultat voulu. Résolu une seule fois ici,
// AVANT que l'un ou l'autre ne soit construit: si la page actuelle appartient à un groupe
// connu, son storageKey passe à 'true' et TOUS les autres à 'false' - initNavGroup lit
// donc déjà la bonne valeur pour chacun, plus de course entre les deux initialisations.
const NAV_GROUPS_META = [
    { storageKey: 'activityExpanded', hrefs: ['/import', '/ebdz-nouveautes', '/history'] },
    { storageKey: 'actionsExpanded', hrefs: ['/bedetheque-enrich', '/missing-monitor', '/verification'] },
];

function _resolveActiveNavGroupStorage() {
    const currentPath = window.location.pathname;
    const activeMeta = NAV_GROUPS_META.find(g => g.hrefs.includes(currentPath));
    if (!activeMeta) return;
    try {
        NAV_GROUPS_META.forEach(g => localStorage.setItem(g.storageKey, g.storageKey === activeMeta.storageKey ? 'true' : 'false'));
    } catch (e) { /* stockage indisponible */ }
}

function initActivityGroup() {
    initNavGroup({
        id: 'activity-nav-subgroup',
        storageKey: 'activityExpanded',
        label: '📋 Suivi',
        hrefs: ['/import', '/ebdz-nouveautes', '/history'],
        insertAfterHref: '/discover'
    });
}

// "Bédéthèque" + "Surveillance" + "Vérification" regroupés sous
// "🛠️ Actions" - même mécanisme qu'Activité (Import/Nouveautés/Historique) ci-dessus.
function initActionsGroup() {
    initNavGroup({
        id: 'actions-nav-subgroup',
        storageKey: 'actionsExpanded',
        label: '🛠️ Actions',
        hrefs: ['/bedetheque-enrich', '/missing-monitor', '/verification']
    });
}

// Bouton "×" pour vider en un clic un champ de recherche/filtre ("ajoute un bouton x pour
// effacer le contenu", demandé pour toutes les barres de recherche/filtre de l'app) -
// ajouté dynamiquement plutôt que dupliqué dans chaque gabarit HTML/chaîne de template:
// entoure l'input d'un conteneur qui reprend son rôle dans la mise en page (flex/largeur,
// voir .search-clear-wrap dans style.css), insère le bouton en position absolue à
// l'intérieur. Idempotent (repérable via le parent .search-clear-wrap déjà posé) - peut
// être rappelé après chaque re-rendu d'un tableau filtrable (buildSeriesTableHtml,
// buildVolumesTableHtml) ou d'une modale de matching (match-modal.js) sans dupliquer les
// enveloppes. Dispatch à la fois 'input' et 'keyup' au clic sur le bouton: les champs de
// cette app sont câblés tantôt en oninput (filtres live), tantôt en onkeyup/onkeypress
// (recherche sur Entrée) - couvrir les deux évite de devoir connaître le câblage exact de
// chaque champ.
function initClearableSearchInputs(root) {
    // "dans tous les endroits ou il y a cette loupe... la garde et la recherche s'ecrit à
    // droite" - .results-filter-input (Découvrir) avait le même 🔍 collé dans son
    // placeholder, disparaissant dès la frappe, sans être couvert par ce mécanisme
    // jusqu'ici. .filter-input (page Recherche) a été retiré après coup ("reviens en
    // arriere... j'ai une grosse icone") - l'icône y ressortait bien plus grande que
    // partout ailleurs, pas encore diagnostiqué.
    // "dans bibliothèque le filter c'est carré avec un icône loupe... je veux celui la
    // partout" - input.th-filterable-control couvre désormais N'IMPORTE QUEL filtre de
    // colonne texte (Enrichir/Historique/Surveillance/résultats de recherche...), pas
    // seulement les 3 classes spécifiques à la bibliothèque/tomes/Découvrir listées
    // avant elle - .th-filterable-icon (l'entonnoir, voir style-library-search.css) reste
    // désormais TOUJOURS masqué par CSS, cette loupe posée ici est le seul mécanisme
    // d'icône pour un filtre texte, qui que soit son appelant.
    (root || document).querySelectorAll(
        'input[type="text"].search-input:not([readonly]), input[type="text"].search-box:not([readonly]), input.series-table-filter-input, input.volume-table-filter-input, input.results-filter-input, input[type="text"].th-filterable-control'
    ).forEach(input => {
        if (input.parentElement && input.parentElement.classList.contains('search-clear-wrap')) return;

        const wrap = document.createElement('span');
        wrap.className = 'search-clear-wrap';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        // Icône loupe persistante à gauche ("garde l'icone loupe et le texte s'affiche
        // après l'icone") plutôt qu'un 🔍 collé au début du texte du placeholder, qui
        // disparaît dès qu'on tape - le retire du placeholder ci-dessous puisque l'icône
        // le remplace visuellement.
        const iconSpan = document.createElement('span');
        iconSpan.className = 'search-icon-prefix';
        iconSpan.innerHTML = svgIcon('search');
        wrap.insertBefore(iconSpan, input);
        if (input.placeholder) {
            // 🔎 (loupe orientée à droite, voir le filtre de /search) en plus de 🔍 -
            // même traitement, sinon elle reste collée au placeholder à côté de la
            // nouvelle icône persistante. Flag /u INDISPENSABLE: 🔍/🔎 sont encodés en
            // paire de substituts UTF-16 (surrogate pair) - sans /u, `[🔍🔎]` est
            // interprété comme une classe de caractères sur les DEMI-UNITÉS 16 bits
            // séparément, pas les codepoints entiers. Le replace() ne retirait donc que
            // la moitié haute de l'emoji, laissant la moitié basse orpheline en tête du
            // placeholder - un substitut isolé s'affiche comme le glyphe de remplacement
            // Unicode (un "�", carré contenant un "?") juste après l'icône persistante
            // ("un gros ?", constaté en direct sur Découvrir).
            input.placeholder = input.placeholder.replace(/^[🔍🔎]\s*/u, '');
        }

        // Transfère le margin inline de l'input (ex: le filtre Nouveautés a
        // style="width:100%; margin-bottom:20px") sur l'enveloppe - sinon ce margin
        // gonfle la hauteur de la ligne flex au-delà de la hauteur visuelle de l'input
        // (voir .search-clear-wrap > input { margin: 0 !important } dans style.css), et
        // le bouton × se centre alors sur cette hauteur gonflée au lieu de celle de
        // l'input, visiblement décalé vers le bas.
        if (input.style.margin || input.style.marginBottom || input.style.marginTop ||
            input.style.marginLeft || input.style.marginRight) {
            wrap.style.margin = input.style.margin;
            wrap.style.marginBottom = input.style.marginBottom;
            wrap.style.marginTop = input.style.marginTop;
            wrap.style.marginLeft = input.style.marginLeft;
            wrap.style.marginRight = input.style.marginRight;
        }

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'search-clear-btn';
        btn.setAttribute('aria-label', 'Effacer');
        btn.tabIndex = -1;
        btn.innerHTML = svgIcon('x');
        wrap.appendChild(btn);

        const sync = () => wrap.classList.toggle('has-value', !!input.value);
        sync();
        input.addEventListener('input', sync);

        btn.addEventListener('click', () => {
            input.value = '';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
            input.focus();
        });
    });

    // "dans les tableaux je voudrais pouvoir elargir la taille de la colonne pour pouvoir
    // voir mieux ce qui se passe" - poignée de redimensionnement posée en JS après coup
    // sur chaque <th>, pas dans les gabarits HTML eux-mêmes (une douzaine de fonctions
    // buildXxxTableHtml à travers l'app). Appelée ici plutôt que dupliquée aux ~24
    // emplacements qui appellent déjà initClearableSearchInputs après CHAQUE (re)rendu de
    // tableau: un seul point d'ajout couvre tout, sans risque d'en oublier un.
    initResizableTableColumns(root);
}

// Identifiant stable pour persister les largeurs d'un tableau dans localStorage. La
// classe du <table> ne suffit pas seule: plusieurs tableaux de la page Vérification
// partagent exactement "series-table series-table-compact" (métadonnées manquantes/noms
// non-standards/fichiers invalides/...) mais ont des colonnes différentes - le plus proche
// ancêtre avec un id (le conteneur dans lequel chaque section fait son propre innerHTML)
// les distingue.
function _resizableTableKey(table) {
    const container = table.closest('[id]');
    return (container ? container.id + '::' : '') + (table.id || table.className || 'table');
}

// Un tableau filtrable/triable de cette app est entièrement reconstruit (innerHTML) à
// chaque tri/filtre/changement de données (voir CLAUDE.md) - la largeur ne peut donc pas
// survivre sur le seul nœud DOM, elle est relue depuis localStorage à chaque appel.
// "do the resize for every table" - une liste de classes précises (série/tomes/résultats
// de recherche/historique/import/surveillance) en avait déjà oublié plusieurs en pratique
// (table.import-files-table AJOUTÉE après coup suite à un premier oubli, "je ne peux pas
// resizer dans import" - et il restait encore d'autres <table> jamais couvertes). Tout
// <table> du DOM correspond désormais, sans distinction de classe - le garde-fou naturel
// est `if (!headerRow) return` juste en dessous: un <table> sans <thead><tr> (les petites
// listes imbriquées "fichiers de ce dossier"/"fichiers de ce pack", voir import.js) n'a
// simplement rien à quoi accrocher une poignée, ignoré sans effet de bord.
//
// "type est trop grand nom trop petit [...] c'est le bordel" - même cause que "Historique
// column balance ca marche pas": une largeur enregistrée AVANT un changement de pourcentages
// par défaut côté app (ex: import-files-table venait tout juste de recevoir ses %
// dédiés, voir style.css) gagne toujours sur le nouveau réglage, sans indice pour
// l'utilisateur qu'il regarde SA personnalisation vieillie plutôt que le nouveau défaut -
// le clic droit (voir plus bas) le corrige au cas par cas, mais rien n'empêchait une
// ancienne préférence de masquer indéfiniment un nouveau réglage livré côté app. Un
// numéro de version accompagne désormais chaque enregistrement (voir DEFAULT_WIDTHS_VERSION,
// onUp ci-dessous et _autoFitColumnWidth) - une entrée d'une version différente (ou
// l'ancien format sans version du tout) est ignorée UNE FOIS, retombant proprement sur
// les pourcentages CSS actuels au lieu de rester bloquée sur un vieux réglage oublié. À
// bumper à chaque fois que les pourcentages par défaut d'une table changent.
const DEFAULT_WIDTHS_VERSION = 3;

function _readSavedColumnWidths(key) {
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem('tableColWidths::' + key) || 'null'); } catch (_) {}
    if (stored && typeof stored === 'object' && stored.v === DEFAULT_WIDTHS_VERSION && stored.widths) {
        return stored.widths;
    }
    return {};
}

function _writeSavedColumnWidths(key, widths) {
    localStorage.setItem('tableColWidths::' + key, JSON.stringify({ v: DEFAULT_WIDTHS_VERSION, widths }));
}

function initResizableTableColumns(root) {
    (root || document).querySelectorAll('table').forEach(table => {
        // Anti-double-câblage pour CETTE instance DOM (un même tableau peut être reconstruit
        // plusieurs fois avant qu'un ancien nœud ne soit détruit, ex: tri puis filtre) -
        // jamais idempotent D'UN rendu à l'autre, chaque nouveau nœud repart de zéro et
        // relit localStorage, ce qui est voulu (voir savedWidths ci-dessous).
        if (table.dataset.resizableInit) return;
        table.dataset.resizableInit = '1';

        const headerRow = table.querySelector('thead tr');
        if (!headerRow) return;
        const key = _resizableTableKey(table);
        const savedWidths = _readSavedColumnWidths(key);
        const ths = Array.from(headerRow.children).filter(el => el.tagName === 'TH');
        // "the cell don't get wrap and extend on the other cells when I resize" - voir
        // .has-resized-columns (style.css): sans elle, un contenu que le CSS propre à
        // CETTE table ne prévoyait pas de faire retourner à la ligne (white-space:nowrap,
        // ou simplement un mot sans espace assez long) déborde visuellement dans la
        // colonne voisine une fois la largeur réellement contrainte (table-layout:fixed).
        if (Object.keys(savedWidths).length) {
            table.style.tableLayout = 'fixed';
            table.classList.add('has-resized-columns');
        }

        ths.forEach((th, index) => {
            if (savedWidths[index]) th.style.width = savedWidths[index] + 'px';
            th.classList.add('col-resizable');
            const handle = document.createElement('span');
            handle.className = 'col-resize-handle';
            handle.title = 'Glisser pour redimensionner - double-clic pour ajuster au texte - clic droit pour réinitialiser';
            // "non je ne peux pas les resizer" - le <th> entier est draggable=true par
            // initDraggableTableColumns (réordonnancement de colonnes, feature préexistante
            // sans rapport) ; un span descendant explicitement draggable=false est la façon
            // standard d'exclure UNE zone précise du drag natif de son ancêtre draggable,
            // sinon un mousedown même exactement sur la poignée peut être capté par le drag
            // natif de réordonnancement avant que _startColumnResize ne s'exécute.
            handle.draggable = false;
            handle.addEventListener('dragstart', e => e.preventDefault());
            // click en plus de mousedown: un <th> de cette app est presque toujours
            // cliquable pour trier (onclick inline) - sans stopPropagation sur le click
            // (événement séparé du mousedown, qui bubble indépendamment depuis ce span
            // enfant), glisser la poignée déclenchait aussi un tri au relâchement.
            handle.addEventListener('click', e => e.stopPropagation());
            handle.addEventListener('mousedown', e => _startColumnResize(e, table, th, key));
            // "double click sur la ligne verticale devrait mettre la largeur equivalente
            // optimale du texte sous-jacent" - même geste qu'un tableur (Excel/Sheets) :
            // ajuste automatiquement CETTE colonne à la largeur nécessaire pour son texte,
            // sans retour à la ligne, plutôt que de devoir tâtonner au glisser.
            handle.addEventListener('dblclick', e => {
                e.preventDefault();
                e.stopPropagation();
                _autoFitColumnWidth(table, th, key);
            });
            // "Historique column balance ca marche pas" - une largeur/un ordre de colonne
            // sauvegardé par un glissement passé (localStorage) gagne toujours sur un
            // nouveau réglage par défaut livré côté app (CSS), sans indice visible pour
            // l'utilisateur qu'il regarde SA personnalisation et pas la valeur d'origine.
            // Clic droit sur n'importe quelle poignée de CETTE table efface sa
            // personnalisation de largeurs (this table only) pour retrouver les
            // pourcentages par défaut de l'app.
            handle.addEventListener('contextmenu', e => {
                e.preventDefault();
                e.stopPropagation();
                localStorage.removeItem('tableColWidths::' + key);
                table.style.tableLayout = '';
                table.classList.remove('has-resized-columns');
                table.querySelectorAll('th, td').forEach(cell => { cell.style.width = ''; });
            });
            th.appendChild(handle);
        });
    });
}

// Mesure la largeur "naturelle" (texte non replié) de chaque cellule de cette colonne
// (en-tête + corps) via un clone hors-écran, plutôt que de manipuler le tableau réel en
// direct (basculer temporairement en table-layout:auto pour mesurer provoquerait un
// réagencement visible de tout le tableau, pas seulement de cette colonne). Ne mesure que
// .textContent ("la largeur [...] du texte sous-jacent", demandé explicitement) - une
// cellule avec une icône/un bouton sans texte n'a donc pas d'incidence ici.
function _autoFitColumnWidth(table, th, key) {
    const headerRow = table.querySelector('thead tr');
    if (!headerRow) return;
    const allThs = Array.from(headerRow.children).filter(el => el.tagName === 'TH');
    const index = allThs.indexOf(th);
    if (index < 0) return;

    const measurer = document.createElement('div');
    measurer.style.cssText = 'position:absolute; visibility:hidden; white-space:nowrap; top:-9999px; left:-9999px;';
    document.body.appendChild(measurer);

    let maxWidth = 0;
    const measure = (cell, isHeader) => {
        let text;
        if (isHeader) {
            // "si je double clique sur type c'est trop gros" - un <th> filtrable
            // (.th-filterable-control) contient un <select> dont .textContent concatène
            // TOUTES les <option> peu importe celle sélectionnée (ex: Historique -
            // "ToutImportsTéléchargementsRecherchesRenommagesConversionsSuppressions" en
            // un seul mot géant) - un clone sans select/input/option isole le libellé
            // réellement visible (ex: juste "Type").
            const clone = cell.cloneNode(true);
            clone.querySelectorAll('select, input, option').forEach(el => el.remove());
            text = (clone.textContent || '').trim();
        } else {
            text = (cell.textContent || '').trim();
        }
        if (!text) return;
        const style = getComputedStyle(cell);
        measurer.style.fontFamily = style.fontFamily;
        measurer.style.fontSize = style.fontSize;
        measurer.style.fontWeight = style.fontWeight;
        measurer.style.letterSpacing = style.letterSpacing;
        measurer.textContent = text;
        const paddingLeft = parseFloat(style.paddingLeft) || 0;
        const paddingRight = parseFloat(style.paddingRight) || 0;
        const width = measurer.getBoundingClientRect().width + paddingLeft + paddingRight;
        if (width > maxWidth) maxWidth = width;
    };

    measure(th, true);
    // Uniquement les lignes DIRECTES du tbody (pas les lignes de détail dépliées à
    // colspan, qui n'ont pas le même nombre de cellules que l'en-tête - même garde que
    // reorderTable côté initDraggableTableColumns).
    table.querySelectorAll(':scope > tbody > tr').forEach(row => {
        if (row.children.length !== allThs.length) return;
        const cell = row.children[index];
        if (cell) measure(cell, false);
    });
    document.body.removeChild(measurer);
    if (maxWidth <= 0) return;

    // +14px: marge de respiration + place pour la poignée elle-même (14px de large, voir
    // style-library-search.css), sinon le texte tout juste ajusté frôlerait la poignée.
    // Plafonné à 60% de la largeur visible: une cellule "résumé" (ex: le détail d'un
    // import groupé listant des dizaines de noms de fichiers en un seul bloc sans retour
    // à la ligne naturel) peut mesurer plusieurs dizaines de milliers de px sans retour à
    // la ligne - ce contenu est fait pour être replié sur plusieurs lignes (voir
    // .has-resized-columns), pas ajusté sur une seule ligne géante. Un texte "normal"
    // (nom de série, date...) ne s'approche jamais de ce plafond en pratique.
    const newWidth = Math.min(Math.max(40, Math.ceil(maxWidth) + 14), Math.round(window.innerWidth * 0.6));

    if (table.style.tableLayout !== 'fixed') {
        allThs.forEach(cellTh => {
            if (!cellTh.style.width) cellTh.style.width = cellTh.getBoundingClientRect().width + 'px';
        });
        table.style.tableLayout = 'fixed';
        table.classList.add('has-resized-columns');
    }
    th.style.width = newWidth + 'px';

    const widths = {};
    allThs.forEach((cellTh, i) => { widths[i] = Math.round(cellTh.getBoundingClientRect().width); });
    _writeSavedColumnWidths(key, widths);
}

// "quand je clique sur resize [...] c'est comme si je cliquais dessus" - un mousedown sur
// la poignée puis un mouseup ailleurs dans le <th> (le point de relâchement ne tombe pas
// forcément pile sur la poignée, qui elle-même se déplace pendant le glissement) fait
// quand même naître un vrai événement 'click' natif du navigateur, avec pour cible
// n'importe quel élément sous le curseur à ce moment-là - pas forcément la poignée, donc
// pas forcément intercepté par son propre stopPropagation. Ce <th> a presque toujours un
// onclick de tri: ce clic "fantôme" déclenchait donc un tri juste après chaque
// redimensionnement. _resizeJustEnded + cet intercepteur global en phase de CAPTURE
// (avant que le onclick du <th> ne s'exécute en phase de bulle) avalent ce click, quelle
// que soit sa cible réelle - un vrai clic de tri ultérieur n'est jamais concerné, le
// drapeau est consommé une seule fois.
let _resizeJustEnded = false;
document.addEventListener('click', e => {
    if (_resizeJustEnded) {
        _resizeJustEnded = false;
        e.stopPropagation();
        e.preventDefault();
    }
}, true);

function _startColumnResize(e, table, th, key) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startWidth = th.getBoundingClientRect().width;
    const headerRow = table.querySelector('thead tr');
    const allThs = Array.from(headerRow.children).filter(el => el.tagName === 'TH');

    if (table.style.tableLayout !== 'fixed') {
        // Fige la largeur ACTUELLE de toutes les colonnes avant de passer en fixed - sinon
        // ce passage auto->fixed redistribue les colonnes encore sans largeur explicite et
        // fait "sauter" tout le tableau dès le premier glissement, pas seulement la colonne
        // qu'on tire (même piège que table-layout:auto documenté ailleurs dans ce fichier/
        // style.css: une largeur posée sous "auto" n'est qu'un indice, jamais contraignante).
        allThs.forEach(cellTh => {
            if (!cellTh.style.width) cellTh.style.width = cellTh.getBoundingClientRect().width + 'px';
        });
        table.style.tableLayout = 'fixed';
        // Voir son commentaire dans initResizableTableColumns ci-dessus - même correctif
        // pour le tout premier glissement d'une table encore jamais redimensionnée
        // (initResizableTableColumns ne l'ajoute, lui, qu'à partir de largeurs déjà
        // sauvegardées en localStorage).
        table.classList.add('has-resized-columns');
    }

    document.body.classList.add('col-resizing');

    function onMove(moveEvent) {
        const newWidth = Math.max(40, startWidth + (moveEvent.clientX - startX));
        th.style.width = newWidth + 'px';
    }
    function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.classList.remove('col-resizing');
        // Toutes les colonnes sont maintenant figées (voir ci-dessus) - autant persister
        // leurs largeurs à toutes plutôt que juste celle glissée, pour retrouver le même
        // agencement complet à la prochaine visite au lieu d'une seule colonne restaurée
        // au milieu de colonnes redevenues auto.
        const widths = {};
        allThs.forEach((cellTh, i) => { widths[i] = Math.round(cellTh.getBoundingClientRect().width); });
        _writeSavedColumnWidths(key, widths);
        _resizeJustEnded = true;
        setTimeout(() => { _resizeJustEnded = false; }, 200);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

// Mettre à jour le lien actif dans la sidebar nav
document.addEventListener('DOMContentLoaded', function() {
    // "quand j'ai cliqué sur le header à gauche, ça a chargé l'ancienne interface pour une
    // fraction de seconde" - voir le commentaire sur .sidebar-nav (visibility:hidden par
    // défaut, style.css): tout ce bloc réorganise la sidebar (groupement Activité/Actions,
    // lien actif...) à partir du HTML brut "à plat" envoyé par le serveur. try/finally:
    // la sidebar redevient visible même si une étape plante en cours de route - sinon une
    // erreur JS la laisserait invisible pour de bon plutôt que de simplement retomber sur
    // l'ancien flash (un échec silencieux qui casse complètement la navigation serait pire
    // que le problème qu'on corrige).
    try {
    wrapSidebarNavLinks();
    _resolveActiveNavGroupStorage();
    initActivityGroup();
    initActionsGroup();
    // #settings-nav-subgroup (Configuration) n'existe dans le DOM que sur /settings
    // (construit directement dans templates/settings.html, toujours déplié par défaut à
    // l'arrivée sur cette page - voir initConfigSubgroupToggle) - contrairement à
    // Activité/Actions ci-dessus, il n'a pas de storageKey pour se signaler lui-même via
    // _collapseOtherNavGroups au moment de leur propre initialisation. Vérifié après coup,
    // ici: si on est bien sur /settings, Configuration est FORCÉMENT le groupe pertinent
    // pour cette page, quoi qu'Activité/Actions aient pu déplier au-dessus à tort à partir
    // d'un état localStorage périmé (resté "ouvert" depuis une autre page).
    if (document.getElementById('settings-nav-subgroup')) {
        _collapseOtherNavGroups('settings-nav-subgroup');
    }
    initClearableSearchInputs();

    const currentPath = window.location.pathname;
    // Enfants directs de .sidebar-nav-scroll (liens de premier niveau) + sous-liens
    // d'#activity-nav-subgroup et #actions-nav-subgroup (Import/Historique,
    // Surveillance/Vérification - href réel désormais imbriqué, voir
    // initNavGroup) - MAIS PAS '.sidebar-nav-scroll a' tout court: les sous-liens
    // d'#settings-nav-subgroup (Configuration) ont un href en fragment (#tab) géré par
    // switchTab(), pas par le chemin de la page - matchés par erreur par cette logique par
    // chemin, leur classe "active" posée par défaut dans le HTML (voir
    // templates/settings.html, "Vue d'ensemble") se faisait retirer ici sans jamais être
    // reposée (switchTab() ne tourne qu'au clic ou si l'URL a déjà un hash au chargement).
    const navLinks = document.querySelectorAll('.sidebar-nav-scroll > a, #activity-nav-subgroup > a, #actions-nav-subgroup > a');

    navLinks.forEach(link => {
        const href = link.getAttribute('href');
        // Cas particulier "/" -> "/library/<id>": currentPath.startsWith(href + '/')
        // devient currentPath.startsWith('//'), qui ne matche jamais rien - "Bibliothèque"
        // ne s'allumait donc jamais dans la sidebar une fois sur une bibliothèque précise.
        const isLibraryRoot = href === '/' && currentPath.startsWith('/library/');
        if (href === currentPath || isLibraryRoot || currentPath.startsWith(href + '/')) {
            link.classList.add('active');
        } else {
            link.classList.remove('active');
        }
    });

    initMobileNav(navLinks);
    initHeaderSearch();
    initThemeToggle();
    // #settings-nav-subgroup (Indexeurs/Clients/Notifications.../Backup, premier niveau de
    // Configuration) exclu de `navLinks` ci-dessus car son état actif est géré par
    // switchTab(), pas par le chemin de la page (voir commentaire plus haut) - mais la
    // modernisation d'icônes ci-dessous doit quand même s'y appliquer ("change les icones
    // pour des icones modernes dans le premier niveau des configurations"), d'où cette
    // liste élargie dédiée à ce seul appel plutôt que d'inclure ce sous-groupe dans
    // `navLinks` lui-même (qui casserait la logique d'état actif documentée plus haut).
    const navLinksWithSettings = document.querySelectorAll(
        '.sidebar-nav-scroll > a, #activity-nav-subgroup > a, #actions-nav-subgroup > a, #settings-nav-subgroup > a'
    );
    initSidebarCollapse(navLinksWithSettings);
    initImportBadge(navLinks);
    initNouveautesBadge(navLinks);
    initUserBadge();
    initManualReviewBadge();
    initConfigSubgroupToggle(navLinks);
    refreshEnabledDownloadClients();
    refreshEnabledIntegrations();
    } finally {
        const sidebar = document.querySelector('.sidebar-nav');
        if (sidebar) sidebar.style.visibility = 'visible';
    }
});

// Le sous-menu de Configuration (#settings-nav-subgroup, uniquement présent dans le HTML
// de /settings - voir templates/settings.html) est toujours déplié à l'arrivée sur la
// page. Un 2e clic sur le lien "⚙️ Configuration" (alors qu'on y est déjà) le replie au
// lieu de recharger la page pour rien ("clique sur configuration de nouveau devrait
// fermer l'ensemble des configurations") - reclic pour le déplier à nouveau.
function initConfigSubgroupToggle(navLinks) {
    const subgroup = document.getElementById('settings-nav-subgroup');
    if (!subgroup) return;

    const configLink = Array.from(navLinks).find(link => link.getAttribute('href') === '/settings');
    if (!configLink) return;

    configLink.addEventListener('click', (e) => {
        e.preventDefault();
        subgroup.classList.toggle('nav-subgroup-collapsed');
        // "when clicking configuration collapse activité et actions" - si ce clic déplie
        // Configuration (pas s'il vient de le replier), Activité/Actions n'ont plus lieu
        // d'être ouverts en même temps. Redondant avec le passage déjà fait au chargement
        // de la page (voir DOMContentLoaded plus haut) mais couvre aussi un reclic sur
        // Configuration alors qu'on est déjà sur /settings, qui ne redéclenche pas ce
        // chargement de page.
        if (!subgroup.classList.contains('nav-subgroup-collapsed')) {
            _collapseOtherNavGroups('settings-nav-subgroup');
        }
    });
}

// ===== Notification globale des résultats automatiques à valider =====
function initManualReviewBadge() {
    const headerContent = document.querySelector('.header-content');
    if (!headerContent || document.querySelector('.header-review-btn')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'header-search-btn header-review-btn';
    btn.setAttribute('data-tooltip', 'Recherches automatiques à valider');
    btn.innerHTML = `${svgIcon('bell')}<span class="header-review-count" aria-label="0"></span>`;
    btn.addEventListener('click', () => { window.location.href = '/validation'; });
    headerContent.appendChild(btn);

    async function refresh() {
        try {
            const response = await fetch('/api/auto-acquire/reviews/count');
            const data = await response.json();
            const count = Number(data.count || 0);
            const badge = btn.querySelector('.header-review-count');
            badge.textContent = count > 99 ? '99+' : (count ? String(count) : '');
            badge.classList.toggle('is-visible', count > 0);
            badge.setAttribute('aria-label', String(count));
        } catch (e) { /* badge non bloquant */ }
    }
    refresh();
    setInterval(refresh, 30000);
}

// ===== Badge utilisateur SSO/OIDC + lien de déconnexion, injecté dans l'en-tête de
// TOUTES les pages. N'affiche rien si le SSO est désactivé (session sans 'user') =====
async function initUserBadge() {
    try {
        const response = await fetch('/api/auth/me');
        const data = await response.json();
        const user = data.user;
        if (!user) return;

        const headerContent = document.querySelector('.header-content');
        if (!headerContent || document.querySelector('.header-user')) return;

        const badge = document.createElement('div');
        badge.className = 'header-user';
        badge.innerHTML = `
            <span class="header-user-name">👤 ${navEscapeHtml(user.name || user.email || '')}</span>
            <a href="/logout" class="header-user-logout">Déconnexion</a>
        `;
        headerContent.appendChild(badge);
    } catch (error) {
        // Ignorer silencieusement : l'absence de badge n'empêche pas d'utiliser l'app
    }
}

// ===== Badge de fichiers en attente dans les dossiers d'import surveillés (aMule/
// torrents) sur le lien "Import" de la sidebar, visible depuis n'importe quelle page.
// Interrogé périodiquement plutôt qu'une seule fois au chargement, pour refléter les
// nouveaux téléchargements arrivés pendant que l'utilisateur navigue ailleurs =====
function initImportBadge(navLinks) {
    // initNavGroup donne à l'en-tête du groupe le même href que son premier enfant
    // (`parent.href = hrefs[0]`, voir plus haut) pour rester navigable même replié -
    // navLinks contient donc DEUX éléments avec href="/import" (l'en-tête "Suivi" ET le
    // vrai sous-lien "Téléchargement"), et .find() tombait sur le premier du DOM: l'en-tête
    // "Suivi" (inséré avant le sous-groupe), pas Téléchargement. D'où le badge de fichiers
    // en attente affiché sur "Suivi" au lieu de "Téléchargement" ("déplace le numéro de
    // notification de Suivi vers Téléchargement"). Préfère explicitement le vrai sous-lien.
    const importLink = Array.from(navLinks).find(link => link.getAttribute('href') === '/import' && link.classList.contains('nav-sublink'))
        || Array.from(navLinks).find(link => link.getAttribute('href') === '/import');
    if (!importLink) return;

    async function refreshImportBadge() {
        try {
            const response = await fetch('/api/import/pending-count');
            const data = await response.json();
            const count = data.count || 0;

            let badge = importLink.querySelector('.nav-badge');
            if (count > 0) {
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'nav-badge';
                    importLink.appendChild(badge);
                }
                badge.textContent = count > 99 ? '99+' : String(count);
            } else if (badge) {
                badge.remove();
            }
        } catch (error) {
            // Ignorer silencieusement (page hors-ligne, requête interrompue par une
            // navigation...): pas de notification d'erreur pour un simple badge
        }
    }

    refreshImportBadge();
    setInterval(refreshImportBadge, 60000);
}

// ===== Badge de nouveautés EBDZ/Telegram non vues sur le lien "Nouveautés" de la
// sidebar ("affiche un numéro pour les nouveautés... comme pour import") - même pattern
// qu'initImportBadge (interrogé périodiquement, visible depuis n'importe quelle page).
// "since la dernière fois que j'ai validé" = la date de la dernière visite de
// /ebdz-nouveautes, posée dans localStorage par ebdz-latest.js à chaque chargement
// réussi de la page (juste avoir vu la page = "validé", pas de bouton de validation
// séparé). Première visite jamais faite: initialisée à "maintenant" ci-dessous plutôt que de
// compter tout l'historique existant comme "nouveau" et afficher un nombre énorme et peu
// utile dès la toute première ouverture de l'app. =====
const NOUVEAUTES_LAST_SEEN_KEY = 'nouveautesLastSeenAt';

function initNouveautesBadge(navLinks) {
    // Même précaution que initImportBadge ci-dessus (voir son commentaire): pas de collision
    // actuelle puisque '/ebdz-nouveautes' n'est pas hrefs[0] d'un groupe aujourd'hui, mais
    // préfère quand même le vrai sous-lien si l'ordre venait à changer.
    const nouveautesLink = Array.from(navLinks).find(link => link.getAttribute('href') === '/ebdz-nouveautes' && link.classList.contains('nav-sublink'))
        || Array.from(navLinks).find(link => link.getAttribute('href') === '/ebdz-nouveautes');
    if (!nouveautesLink) return;

    if (!localStorage.getItem(NOUVEAUTES_LAST_SEEN_KEY)) {
        localStorage.setItem(NOUVEAUTES_LAST_SEEN_KEY, new Date().toISOString());
    }

    async function refreshNouveautesBadge() {
        try {
            const since = localStorage.getItem(NOUVEAUTES_LAST_SEEN_KEY) || '';
            const response = await fetch(`/api/ebdz/nouveautes/new-count?since=${encodeURIComponent(since)}`);
            const data = await response.json();
            const count = data.count || 0;

            let badge = nouveautesLink.querySelector('.nav-badge');
            if (count > 0) {
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'nav-badge';
                    nouveautesLink.appendChild(badge);
                }
                badge.textContent = count > 99 ? '99+' : String(count);
            } else if (badge) {
                badge.remove();
            }
        } catch (error) {
            // Ignorer silencieusement, même raison qu'initImportBadge
        }
    }

    refreshNouveautesBadge();
    setInterval(refreshNouveautesBadge, 60000);
}

// Repli de la sidebar en mode icônes seules (desktop uniquement), état retenu d'une
// Emoji -> icône Lucide (voir icons.js) pour les liens de la sidebar ("trouve des icones
// plus moderne pour les options de la tab de gauche") - table de correspondance plutôt
// que de modifier les 13 templates qui portent chacun l'emoji en dur dans leur texte de
// lien: initSidebarCollapse (ci-dessous) sépare déjà emoji/libellé au premier espace pour
// tout autre usage (mode réduit), il suffit d'y brancher un rendu SVG à la place du texte
// brut quand l'emoji est reconnu ici.
const SIDEBAR_ICON_MAP = {
    '📚': 'library',
    '📥': 'download',
    '🎯': 'target',
    '🔍': 'search',
    '🆕': 'sparkles',
    '↔️': 'arrow-left-right',
    '📖': 'book-open',
    '🕓': 'history',
    '✅': 'check-check',
    '📊': 'activity',
    '📡': 'radio',
    '⚙️': 'settings',
    '📋': 'layout-list',
    '🛠️': 'wrench',
    // Premier niveau de Configuration (#settings-nav-subgroup, voir navLinksWithSettings
    // plus haut) - "change les icones pour des icones modernes dans le premier niveau des
    // configurations"
    '🔌': 'plug',
    '💻': 'monitor',
    '📱': 'smartphone',
    '🔐': 'lock',
    '🌓': 'moon',
    '🎨': 'palette',
    '🗂️': 'search',
    '✏️': 'pencil',
    '💾': 'save'
};

// page à l'autre via localStorage
function initSidebarCollapse(navLinks) {
    const sidebar = document.querySelector('.sidebar-nav');
    const scroll = document.querySelector('.sidebar-nav-scroll');
    if (!sidebar || !scroll) return;

    // Sépare l'icône (emoji) du libellé dans chaque lien, pour pouvoir cacher le
    // libellé en mode réduit tout en gardant l'icône visible.
    // "remove tooltips on the sidebar. there are useless" - posait avant un data-tooltip
    // (le libellé) sur CHAQUE lien, y compris en mode étendu où le libellé texte est déjà
    // visible juste à côté de l'icône - un tooltip flottant répétant ce texte déjà lisible
    // au survol n'apportait rien, seulement gênant. Retiré : en mode réduit, le nom de la
    // page reste accessible en cliquant (ou en dépliant temporairement la sidebar), pas de
    // filet de secours au survol.
    navLinks.forEach(link => {
        if (link.querySelector('.nav-label')) return;
        const text = link.textContent.trim();
        const spaceIndex = text.indexOf(' ');
        if (spaceIndex === -1) return;
        const icon = text.slice(0, spaceIndex);
        const label = text.slice(spaceIndex + 1);
        link.textContent = '';

        const iconSpan = document.createElement('span');
        iconSpan.className = 'nav-icon';
        const lucideIcon = SIDEBAR_ICON_MAP[icon];
        if (lucideIcon) {
            iconSpan.innerHTML = svgIcon(lucideIcon);
        } else {
            iconSpan.textContent = icon;
        }

        const labelSpan = document.createElement('span');
        labelSpan.className = 'nav-label';
        labelSpan.textContent = label;

        link.appendChild(iconSpan);
        link.appendChild(labelSpan);
    });

    const toggleBtn = document.createElement('button');
    toggleBtn.type = 'button';
    toggleBtn.className = 'sidebar-collapse-btn';
    toggleBtn.setAttribute('aria-label', 'Réduire/agrandir le menu');
    // Chevron SVG (jeu d'icônes déjà utilisé partout ailleurs dans l'app) plutôt que les
    // caractères texte ◀/▶ précédents - .sidebar-collapse-icon pivote via CSS selon l'état
    // (voir style.css), pas besoin d'une seconde icône dédiée "chevron-left"
    toggleBtn.innerHTML = `${svgIcon('chevron-down', 'sidebar-collapse-icon')}<span class="nav-label">Réduire</span>`;
    scroll.appendChild(toggleBtn);

    const collapseIcon = toggleBtn.querySelector('.sidebar-collapse-icon');
    const collapseLabel = toggleBtn.querySelector('.nav-label');

    // Largeur manuelle de la sidebar ("mets en place un ajustement manuel pour la largeur
    // de la sidebar"), mémorisée à part de l'état réduit/étendu ci-dessus. N'a de sens que
    // pour la colonne permanente desktop: en mode réduit la largeur est imposée à 60px par
    // le CSS (.sidebar-nav.collapsed), et une largeur inline la battrait (priorité des
    // styles inline sur les règles de classe) si on ne la retirait pas explicitement à la
    // fermeture ci-dessous. Sur mobile (tiroir, voir .sidebar-nav.open dans style.css) une
    // largeur inline issue d'un réglage desktop précédent casserait tout aussi mal le
    // tiroir (260px/80vw imposés par sa propre media query) - appliquée seulement au-dessus
    // de 768px, et retirée si la fenêtre passe sous ce seuil pendant que la page est ouverte.
    const SIDEBAR_WIDTH_KEY = 'sidebarWidth';
    const SIDEBAR_MIN_WIDTH = 160;
    const SIDEBAR_MAX_WIDTH = 420;
    const MOBILE_BREAKPOINT = 768;

    function applySavedWidth() {
        if (window.innerWidth <= MOBILE_BREAKPOINT) return;
        try {
            const saved = localStorage.getItem(SIDEBAR_WIDTH_KEY);
            if (saved) sidebar.style.width = `${saved}px`;
        } catch (e) { /* stockage indisponible */ }
    }

    function applyState(collapsed) {
        sidebar.classList.toggle('collapsed', collapsed);
        if (collapsed) {
            sidebar.style.width = '';
        } else {
            applySavedWidth();
        }
        if (collapseLabel) collapseLabel.textContent = collapsed ? 'Agrandir' : 'Réduire';
        if (collapseIcon) collapseIcon.style.transform = collapsed ? 'rotate(-90deg)' : 'rotate(90deg)';
    }

    applyState(localStorage.getItem('sidebarCollapsed') === 'true');

    toggleBtn.addEventListener('click', () => {
        const collapsed = !sidebar.classList.contains('collapsed');
        localStorage.setItem('sidebarCollapsed', String(collapsed));
        applyState(collapsed);
    });

    window.addEventListener('resize', () => {
        if (sidebar.classList.contains('collapsed')) return;
        if (window.innerWidth <= MOBILE_BREAKPOINT) sidebar.style.width = '';
        else applySavedWidth();
    });

    const handle = document.createElement('div');
    handle.className = 'sidebar-resize-handle';
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', 'vertical');
    handle.setAttribute('aria-label', 'Redimensionner le menu (double-clic pour réinitialiser)');
    sidebar.appendChild(handle);

    let startX = 0;
    let startWidth = 0;

    function onResizeMove(event) {
        const delta = event.clientX - startX;
        const width = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, startWidth + delta));
        sidebar.style.width = `${width}px`;
    }

    function onResizeEnd() {
        sidebar.classList.remove('resizing');
        document.removeEventListener('pointermove', onResizeMove);
        document.removeEventListener('pointerup', onResizeEnd);
        try {
            localStorage.setItem(SIDEBAR_WIDTH_KEY, String(Math.round(sidebar.getBoundingClientRect().width)));
        } catch (e) { /* stockage indisponible */ }
    }

    handle.addEventListener('pointerdown', (event) => {
        if (sidebar.classList.contains('collapsed') || event.button !== 0) return;
        event.preventDefault();
        startX = event.clientX;
        startWidth = sidebar.getBoundingClientRect().width;
        sidebar.classList.add('resizing');
        document.addEventListener('pointermove', onResizeMove);
        document.addEventListener('pointerup', onResizeEnd);
    });

    // Double-clic pour revenir à la largeur par défaut du CSS (200px/230px selon la
    // largeur d'écran, voir style.css) plutôt que de devoir la retrouver au pixel près
    // en glissant.
    handle.addEventListener('dblclick', () => {
        sidebar.style.width = '';
        try { localStorage.removeItem(SIDEBAR_WIDTH_KEY); } catch (e) { /* stockage indisponible */ }
    });
}

// Menu mobile : tiroir latéral déclenché par un bouton hamburger
function initMobileNav(navLinks) {
    const sidebar = document.querySelector('.sidebar-nav');
    const headerContent = document.querySelector('.header-content');
    if (!sidebar || !headerContent) return;

    const brand = document.createElement('a');
    brand.href = '/';
    brand.className = 'header-brand';
    brand.setAttribute('aria-label', 'Bullarr - accueil');
    brand.innerHTML = '<img src="/static/img/bullarr-icon.png" alt="" class="header-brand-icon"><span class="header-brand-name">Bull<span class="header-brand-accent">arr</span></span>';
    headerContent.prepend(brand);

    const hamburger = document.createElement('button');
    hamburger.type = 'button';
    hamburger.className = 'hamburger-btn';
    hamburger.setAttribute('aria-label', 'Ouvrir le menu de navigation');
    hamburger.setAttribute('aria-expanded', 'false');
    hamburger.innerHTML = '<span></span><span></span><span></span>';
    headerContent.prepend(hamburger);

    const overlay = document.createElement('div');
    overlay.className = 'nav-overlay';
    document.body.appendChild(overlay);

    function closeMenu() {
        sidebar.classList.remove('open');
        overlay.classList.remove('open');
        hamburger.setAttribute('aria-expanded', 'false');
    }

    function toggleMenu() {
        const isOpen = sidebar.classList.toggle('open');
        overlay.classList.toggle('open', isOpen);
        hamburger.setAttribute('aria-expanded', String(isOpen));
    }

    hamburger.addEventListener('click', toggleMenu);
    overlay.addEventListener('click', closeMenu);
    navLinks.forEach(link => link.addEventListener('click', closeMenu));
    window.addEventListener('resize', function() {
        if (window.innerWidth > 768) closeMenu();
    });
}

// ===== Recherche rapide d'une série, toutes bibliothèques confondues =====
// Injectée dans l'en-tête de TOUTES les pages (pas seulement celles qui chargent
// library.js), pour retrouver une série sans repasser par l'accueil. Charge la liste
// complète une seule fois (à la première ouverture), puis filtre localement à chaque
// frappe plutôt que de refaire un appel réseau par caractère tapé
let navHeaderSearchAllSeries = null;
let navHeaderSearchLoadingPromise = null;

// Pose/retire .has-value (voir .th-filterable-control.has-value, style-library-search.css)
// sur un contrôle de filtre de colonne selon qu'il a une valeur ou non. Vivait auparavant
// dans search-results-table.js, avec une copie locale dupliquée dans history.js (page qui
// ne charge pas ce fichier) - remonté ici puisque nav.js est chargé sur TOUTES les pages,
// pour que tout tableau filtrable (y compris le filtre auteur de bedetheque-enrich.html)
// puisse le réutiliser sans dupliquer ces deux lignes une troisième fois.
function _syncFilterControlActive(el) {
    el.classList.toggle('has-value', el.value.trim() !== '');
}

function navEscapeHtml(text) {
    const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

// "the time is in utc can you change to paris time" - toute date affichée dans l'app vient
// soit de SQLite (CURRENT_TIMESTAMP, TOUJOURS en UTC, stocké comme "2026-08-12 22:05:25"
// SANS aucune indication de fuseau dans la chaîne elle-même), soit d'un timestamp epoch
// (mtime de fichier, déjà un instant absolu), soit d'APScheduler (déjà une chaîne ISO AVEC
// décalage, ex: "...+00:00"). Un `new Date("2026-08-12 22:05:25")` (ou même remplacé par
// "T") est traité par le navigateur comme une heure LOCALE (spec ECMA-262, aucun décalage
// dans la chaîne) et non comme de l'UTC - l'heure affichée était donc littéralement les
// chiffres UTC bruts recopiés tels quels, jamais réellement convertis. BULLARR_DISPLAY_TZ +
// parseDbUtcDate (ancre explicitement une chaîne SQLite en UTC via un suffixe 'Z' avant de
// construire le Date) forment la paire à utiliser partout où l'app affiche une date
// provenant de la base - un timestamp déjà absolu (epoch, ISO avec décalage) n'a lui besoin
// que de {timeZone: BULLARR_DISPLAY_TZ} au moment du formatage, jamais de ce parsing.
const BULLARR_DISPLAY_TZ = 'Europe/Paris';
function parseDbUtcDate(value) {
    if (value == null || value === '') return null;
    const iso = String(value).trim().replace(' ', 'T');
    const withZone = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z';
    const d = new Date(withZone);
    return Number.isNaN(d.getTime()) ? null : d;
}
// "si ca se passé aujourdhui tu mets seulement l'heure" (history-shared.js/ebdz-latest.js):
// ce genre de comparaison utilisait Date.toDateString(), qui lit le fuseau LOCAL du
// navigateur - pas forcément Europe/Paris (voir BULLARR_DISPLAY_TZ ci-dessus). Compare la
// date calendaire telle qu'affichée à Paris pour les deux dates plutôt que celle du fuseau
// local du navigateur, pour rester cohérent avec le reste de cet affichage forcé en heure
// de Paris.
function isSameDisplayDate(a, b) {
    if (!a || !b) return false;
    const fmt = d => d.toLocaleDateString('en-CA', { timeZone: BULLARR_DISPLAY_TZ });
    return fmt(a) === fmt(b);
}

// Normalise un texte pour une recherche insensible aux accents/casse (ex: "ecole"
// retrouve "École"), même logique que normalize_search_text côté serveur (EBDZ)
function navNormalizeSearch(text) {
    return String(text)
        .replace(/œ/g, 'oe').replace(/Œ/g, 'OE')
        .replace(/æ/g, 'ae').replace(/Æ/g, 'AE')
        .normalize('NFKD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLowerCase();
}

function initHeaderSearch() {
    const headerContent = document.querySelector('.header-content');
    const contentEl = document.querySelector('.content');
    if (!headerContent || !contentEl || document.querySelector('.header-search')) return;

    // Le champ de recherche s'ouvre à même le header (pas de boîte flottante par-dessus
    // le contenu); les résultats s'affichent dans un panneau inséré en haut de la page,
    // qui pousse le contenu vers le bas comme une section normale
    const wrapper = document.createElement('div');
    wrapper.className = 'header-right';
    wrapper.innerHTML = `
        <div class="header-search">
            <input type="text" id="header-search-input" class="header-search-inline-input" placeholder="Chercher une série...">
            <button type="button" class="header-search-btn">${svgIcon('search')}</button>
        </div>
    `;
    headerContent.appendChild(wrapper);

    const panel = document.createElement('div');
    panel.className = 'header-search-panel';
    panel.id = 'header-search-panel';
    panel.style.display = 'none';
    panel.innerHTML = '<div id="header-search-results"></div>';
    contentEl.insertBefore(panel, contentEl.firstChild);

    wrapper.querySelector('.header-search-btn').addEventListener('click', toggleHeaderSearch);
    wrapper.querySelector('#header-search-input').addEventListener('keyup', (e) => {
        if (e.key === 'Escape') {
            closeHeaderSearch();
            return;
        }
        handleHeaderSearchInput();
    });

    document.addEventListener('click', (e) => {
        if (wrapper.contains(e.target) || panel.contains(e.target)) return;
        closeHeaderSearch();
    });
}

function closeHeaderSearch() {
    const wrapper = document.querySelector('.header-search');
    const panel = document.getElementById('header-search-panel');
    if (wrapper) wrapper.classList.remove('expanded');
    if (panel) panel.style.display = 'none';
}

async function toggleHeaderSearch() {
    const wrapper = document.querySelector('.header-search');
    if (!wrapper) return;
    const isExpanded = wrapper.classList.contains('expanded');
    if (isExpanded) {
        closeHeaderSearch();
        return;
    }
    wrapper.classList.add('expanded');
    document.getElementById('header-search-input').focus();
    await ensureHeaderSearchDataLoaded();
}

async function ensureHeaderSearchDataLoaded() {
    if (navHeaderSearchAllSeries) return;
    if (navHeaderSearchLoadingPromise) return navHeaderSearchLoadingPromise;

    const resultsEl = document.getElementById('header-search-results');
    navHeaderSearchLoadingPromise = (async () => {
        try {
            const libResponse = await fetch('/api/libraries');
            if (!libResponse.ok) throw new Error(`Chargement des bibliothèques impossible (${libResponse.status})`);
            const librariesPayload = await libResponse.json();
            const libraries = Array.isArray(librariesPayload)
                ? librariesPayload : (librariesPayload.libraries || []);
            const all = [];
            for (const lib of libraries) {
                const response = await fetch(`/api/library/${lib.id}/series`);
                if (!response.ok) continue;
                const seriesPayload = await response.json();
                const series = Array.isArray(seriesPayload)
                    ? seriesPayload : (seriesPayload.series || []);
                series.forEach(s => all.push({ ...s, library_name: lib.name }));
            }
            navHeaderSearchAllSeries = all;
            handleHeaderSearchInput();
        } catch (error) {
            resultsEl.innerHTML = `<div style="padding: 10px; color: #c33; font-size: 0.85em;">Erreur de chargement: ${navEscapeHtml(error.message)}</div>`;
        } finally {
            navHeaderSearchLoadingPromise = null;
        }
    })();
    return navHeaderSearchLoadingPromise;
}

function handleHeaderSearchInput() {
    const rawQuery = document.getElementById('header-search-input').value.trim();
    const query = navNormalizeSearch(rawQuery);
    const panel = document.getElementById('header-search-panel');
    const resultsEl = document.getElementById('header-search-results');
    if (!navHeaderSearchAllSeries) return;

    if (!query) {
        panel.style.display = 'none';
        resultsEl.innerHTML = '';
        return;
    }

    panel.style.display = 'block';
    const matches = navHeaderSearchAllSeries.filter(s => navNormalizeSearch(s.title).includes(query)).slice(0, 20);

    const encodedQuery = encodeURIComponent(rawQuery);
    const addLinkHtml = `<a href="/discover?q=${encodedQuery}" class="header-search-external-link">${svgIcon('plus')} Ajouter "${navEscapeHtml(rawQuery)}" à la bibliothèque</a>`;

    if (matches.length === 0) {
        resultsEl.innerHTML = `
            <div style="padding: 10px; color: #888; font-size: 0.9em;">Aucune série trouvée dans la bibliothèque</div>
            ${addLinkHtml}
        `;
        return;
    }

    resultsEl.innerHTML = matches.map(s => {
        const cover = s.local_cover_path || s.bedetheque_cover_path || s.komga_cover_path;
        const coverHtml = cover
            ? `<img class="header-search-result-cover" src="/${cover}" alt="">`
            : `<div class="header-search-result-cover"></div>`;
        return `
            <div class="header-search-result" onclick="window.location.href='/series/${s.id}'">
                ${coverHtml}
                <div class="header-search-result-info">
                    <div class="header-search-result-title" data-tooltip="${navEscapeHtml(s.title)}">${navEscapeHtml(s.title)}</div>
                    <div class="header-search-result-library">${navEscapeHtml(s.library_name)}</div>
                </div>
            </div>
        `;
    }).join('') + addLinkHtml;
}

// ============================================
// Toasts d'activité (bas-droite): petites notifications non-bloquantes pour signaler
// une action en arrière-plan (MAJ métadonnées, import, renommage, scan Komga...), sans
// interrompre l'utilisateur comme le ferait un alert(). Chaque toast a un id fourni par
// l'appelant: showToast met à jour le toast existant du même id s'il y en a déjà un
// (évite d'empiler plusieurs toasts pour une même opération relancée), dismissToast le
// retire (avec une transition de sortie avant suppression du DOM).
//
// Persistance via localStorage: un toast sans autoHideMs (donc en attente d'un
// dismissToast explicite - typiquement une opération en cours) est aussi écrit dans
// localStorage. Sans ça, changer de page (navigation classique, pas une SPA) détruit le
// DOM et donc le toast, même si l'opération qu'il signalait continue côté serveur (ex:
// écriture ComicInfo en arrière-plan) ou que l'utilisateur navigue juste pendant qu'un
// import est en cours. Chaque page recharge nav.js, qui restaure au chargement les
// toasts persistés encore "récents" (TOAST_MAX_AGE_MS): au-delà, on considère que le
// dismissToast correspondant a dû être manqué (page fermée avant la fin, requête
// abandonnée par la navigation...) et on nettoie plutôt que de laisser un toast fantôme
// affiché indéfiniment.
const TOAST_STORAGE_KEY = 'app-toasts';
const TOAST_MAX_AGE_MS = 10 * 60 * 1000;
let toastContainerEl = null;

// Registre des sondages de progression en cours (voir pollMetadataWriteProgress
// plus bas): un toast persisté (voir plus haut) ne fait que réafficher son dernier
// message connu au chargement d'une nouvelle page, il ne se remet pas à jour tout
// seul - l'utilisateur voyait un toast "figé" en changeant de page pendant qu'une
// MAJ métadonnées tournait encore côté serveur ("toast are not getting update when I
// change pages"). Ce registre permet à restorePersistedToasts (plus bas) de relancer
// automatiquement le sondage pour tout toast restauré qui en avait un actif.
const POLL_REGISTRY_KEY = 'app-toast-polls';

function _readPollRegistry() {
    try {
        return JSON.parse(localStorage.getItem(POLL_REGISTRY_KEY)) || {};
    } catch (e) {
        return {};
    }
}

function _writePollRegistry(polls) {
    try {
        localStorage.setItem(POLL_REGISTRY_KEY, JSON.stringify(polls));
    } catch (e) {
        // stockage indisponible - le sondage fonctionne quand même pour la page
        // courante, juste pas repris après une navigation
    }
}

function getToastContainerEl() {
    if (!toastContainerEl) {
        toastContainerEl = document.createElement('div');
        toastContainerEl.id = 'toast-container';
        document.body.appendChild(toastContainerEl);
    }
    return toastContainerEl;
}

function _readPersistedToasts() {
    try {
        return JSON.parse(localStorage.getItem(TOAST_STORAGE_KEY)) || {};
    } catch (e) {
        return {};
    }
}

function _writePersistedToasts(toasts) {
    try {
        localStorage.setItem(TOAST_STORAGE_KEY, JSON.stringify(toasts));
    } catch (e) {
        // stockage indisponible (navigation privée, quota...) - dégrade en toast simple
        // non persisté, pas bloquant pour la fonctionnalité principale
    }
}

function _renderToastElement(id, message, icon, href) {
    const container = getToastContainerEl();
    let toast = document.getElementById(`toast-${id}`);
    if (!toast) {
        toast = document.createElement('div');
        toast.id = `toast-${id}`;
        toast.className = 'app-toast';
        container.appendChild(toast);
    }
    // message toujours en textContent: restorePersistedToasts relit `icon`/`message` tel
    // quel depuis localStorage, jamais interpolé dans innerHTML depuis une valeur non
    // fiable. `icon` est l'exception documentée ci-dessous: un NOM d'icône (ex:
    // 'triangle-alert'), jamais du HTML - le seul contenu injecté via innerHTML est
    // toujours l'un des tracés fixes de ICON_PATHS (icons.js), choisi par une recherche
    // de clé dans un dictionnaire figé côté code, jamais construit à partir du contenu
    // de `icon` lui-même. Une valeur de `icon` absente de ICON_PATHS (ancien toast
    // persisté avant cette migration, encore un emoji brut) retombe simplement en texte
    // brut plutôt que de casser l'affichage.
    toast.innerHTML = `<span class="app-toast-icon"></span><span class="app-toast-message"></span><span class="app-toast-close" title="Fermer">×</span>`;
    const iconEl = toast.querySelector('.app-toast-icon');
    if (typeof ICON_PATHS !== 'undefined' && Object.prototype.hasOwnProperty.call(ICON_PATHS, icon)) {
        iconEl.innerHTML = svgIcon(icon);
    } else {
        iconEl.textContent = icon;
    }
    toast.querySelector('.app-toast-message').textContent = message;
    toast.querySelector('.app-toast-close').addEventListener('click', (e) => {
        e.stopPropagation();
        dismissToast(id);
    });
    // "ouvre un toast avec la série [...] si je cliques dessus ca va à la page de la
    // série" - href optionnel, réutilisable par n'importe quel appelant (pas seulement
    // l'ajout d'une série depuis "Albums de l'auteur") plutôt qu'un mécanisme propre à
    // ce seul flux.
    toast.classList.toggle('app-toast-clickable', !!href);
    toast.onclick = href ? () => { window.location.href = href; } : null;
    toast.classList.remove('app-toast-hiding');
    return toast;
}

// "aussi ajouter une section recherche dans l'historique ou tu mets les recherches
// (manuelles et auto)" - un seul appel par recherche lancée par l'utilisateur (pas un
// par source EBDZ/Prowlarr/Telegram/fourtoutici interrogée), voir POST /api/actions/
// log-search côté blueprints/library/routes.py. Dans nav.js (chargé sur /search,
// /discover et la fiche série, voir searchBoth/searchSources/searchMissingVolume) plutôt
// que dans un fichier propre à une seule de ces pages - best-effort, ne doit jamais
// bloquer/faire échouer la recherche elle-même si la journalisation échoue.
function logSearchHistoryEvent(title, detail, seriesId = null) {
    fetch('/api/actions/log-search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, detail, series_id: seriesId })
    }).catch(() => {});
}

// "you know exactly how many so put tomes if more than 1. i don't want the (s) [...]
// thing" - accorde un mot au pluriel français selon un compte déjà connu au moment de
// construire le message, au lieu du suffixe "(s)" ambigu utilisé un peu partout dans les
// toasts/confirmations/alertes de cette app. `plural` optionnel pour un pluriel
// irrégulier (ex: pluralize(n, 'un', 'des')) - par défaut, `singular` + 's'.
function pluralize(count, singular, plural) {
    return count > 1 ? (plural !== undefined ? plural : `${singular}s`) : singular;
}

function showToast(id, message, { icon = 'loader-circle', autoHideMs, href } = {}) {
    const toast = _renderToastElement(id, message, icon, href);
    clearTimeout(toast._autoHideTimer);

    if (autoHideMs) {
        toast._autoHideTimer = setTimeout(() => dismissToast(id), autoHideMs);
    } else {
        // createdAt réinitialisé à chaque (re)affichage, y compris ceux issus d'une
        // action utilisateur normale (pas seulement la restauration au chargement de
        // page) - c'est voulu: une opération dont le message est mis à jour plusieurs
        // fois (ex: progression) reste "fraîche" tant qu'elle est active, seule une
        // restoration silencieuse (voir restorePersistedToasts) doit préserver l'horodatage
        // d'origine pour que TOAST_MAX_AGE_MS purge un toast vraiment abandonné
        const toasts = _readPersistedToasts();
        toasts[id] = { message, icon, href, createdAt: Date.now() };
        _writePersistedToasts(toasts);
    }
}

function dismissToast(id) {
    const toasts = _readPersistedToasts();
    if (toasts[id]) {
        delete toasts[id];
        _writePersistedToasts(toasts);
    }

    // Retirer aussi du registre de sondage (voir pollMetadataWriteProgress): sans ça, un
    // toast fermé manuellement (bouton ×) via une page où le sondage tourne encore
    // ressuscitait avec un sondage relancé à la prochaine navigation, puisque le registre
    // le désignait toujours comme "à reprendre"
    const polls = _readPollRegistry();
    if (polls[id]) {
        delete polls[id];
        _writePollRegistry(polls);
    }

    const toast = document.getElementById(`toast-${id}`);
    if (!toast) return;
    clearTimeout(toast._autoHideTimer);
    clearInterval(toast._pollInterval);
    toast.classList.add('app-toast-hiding');
    setTimeout(() => toast.remove(), 300);
}

// Sonde la progression de l'écriture ComicInfo en arrière-plan (voir
// GET /update-metadata/series/<id>/progress côté Flask) pour afficher dans le toast le
// tome en cours de traitement (ex: "Mise à jour des métadonnées... (12/45 - Tome 12)")
// plutôt qu'un message générique sans détail pendant potentiellement plusieurs minutes.
// S'arrête (et referme le toast) dès que le serveur ne renvoie plus de progression pour
// cette série (terminé, voir _write_series_volumes_metadata_async qui retire son entrée
// en quittant). Vit dans nav.js (chargé sur toutes les pages) plutôt que library.js: une
// navigation vers une autre page perd le setInterval comme tout le JS de la page, mais
// nav.js se recharge à chaque page et peut donc reprendre le sondage via le registre
// ci-dessus (voir restorePersistedToasts) - avant ça le toast persisté restait figé sur
// son dernier message connu jusqu'à dismissToast ou la purge par ancienneté.
function pollMetadataWriteProgress(seriesId, toastId) {
    const polls = _readPollRegistry();
    polls[toastId] = { kind: 'metadata-write', seriesId };
    _writePollRegistry(polls);

    let sawProgress = false;
    const interval = setInterval(async () => {
        try {
            const response = await fetch(`/api/bedetheque/update-metadata/series/${seriesId}/progress`);
            const data = await response.json();
            const progress = data.progress;

            if (!progress) {
                clearInterval(interval);
                dismissToast(toastId);
                // Le thread ne déclenche le rescan Komga qu'à la toute fin (voir
                // _write_series_volumes_metadata_async, "if updated:") - l'afficher ici,
                // à la disparition de la progression, plutôt qu'au lancement de l'action
                // (comme avant) qui annonçait le scan plusieurs minutes trop tôt
                if (sawProgress) showToast('komga-scan', 'Scan Komga demandé', { icon: 'radio', autoHideMs: 4000 });
                return;
            }

            sawProgress = true;
            showToast(toastId, `Mise à jour des métadonnées... (${progress.index}/${progress.total} - ${progress.label})`);
        } catch (error) {
            // Erreur réseau ponctuelle: on retente au prochain tick plutôt que d'abandonner
            console.warn('Erreur lors du suivi de progression:', error);
        }
    }, 2000);

    // Rattaché au toast (comme _autoHideTimer) pour que dismissToast puisse l'arrêter
    // immédiatement plutôt que de laisser un dernier tick réafficher le toast juste fermé
    const toastEl = document.getElementById(`toast-${toastId}`);
    if (toastEl) toastEl._pollInterval = interval;
}

// Sonde POST /api/bedetheque/link-volumes-batch (rattachement Bédéthèque en lot depuis
// /verification, "this should be in background. if i quit the page") - même principe que
// pollMetadataWriteProgress juste au-dessus (toast persistant + registre de sondage repris
// à la restauration), mais ce job n'est pas scopé à une série (sélection multi-séries
// possible) donc son critère de fin est `running: false` plutôt que l'absence de
// progression pour un id donné. onComplete (optionnel, jamais persisté - une fonction ne
// survit pas à une vraie navigation/rechargement) laisse l'appelant réagir au résultat
// ({linked, total, failed}) UNIQUEMENT tant qu'il reste sur la même page JS qui a lancé ou
// repris ce sondage - une reprise depuis restorePersistedToasts (autre page) n'en fournit
// volontairement aucun, elle ne fait que garder le toast à jour.
function pollLinkVolumesBatchProgress(toastId, onComplete) {
    const polls = _readPollRegistry();
    polls[toastId] = { kind: 'link-volumes-batch' };
    _writePollRegistry(polls);

    const interval = setInterval(async () => {
        try {
            const response = await fetch('/api/bedetheque/link-volumes-batch/progress');
            const data = await response.json();

            if (!data.running) {
                clearInterval(interval);
                dismissToast(toastId);
                if (onComplete) onComplete(data.result || { linked: 0, total: 0, failed: [] });
                return;
            }

            if (data.progress) {
                showToast(toastId, `Rattachement Bédéthèque (${data.progress.index}/${data.progress.total}): ${data.progress.label}...`);
            }
        } catch (error) {
            console.warn('Erreur lors du suivi du rattachement Bédéthèque en lot:', error);
        }
    }, 2000);

    const toastEl = document.getElementById(`toast-${toastId}`);
    if (toastEl) toastEl._pollInterval = interval;
}

// Restaure au chargement de chaque page les toasts persistés encore valides (voir
// commentaire plus haut), et purge celles devenues trop vieilles
(function restorePersistedToasts() {
    const toasts = _readPersistedToasts();
    const polls = _readPollRegistry();
    const now = Date.now();
    let changed = false;
    let pollsChanged = false;

    for (const [id, toast] of Object.entries(toasts)) {
        if (now - toast.createdAt > TOAST_MAX_AGE_MS) {
            delete toasts[id];
            changed = true;
            if (polls[id]) {
                delete polls[id];
                pollsChanged = true;
            }
            continue;
        }
        // Rendu direct (pas showToast, qui écraserait createdAt et empêcherait la purge
        // d'un toast vraiment abandonné - voir commentaire dans showToast)
        _renderToastElement(id, toast.message, toast.icon, toast.href);

        // Reprend le sondage de progression pour tout toast restauré qui en avait un
        // actif (voir pollMetadataWriteProgress/pollLinkVolumesBatchProgress) - sans ça
        // le toast restait affiché figé sur son dernier message connu jusqu'à
        // dismissToast ou la purge par ancienneté, même si l'opération continuait bien
        // côté serveur. kind absent = anciennes entrées de registre d'avant l'ajout de
        // pollLinkVolumesBatchProgress, toutes des MAJ métadonnées série.
        if (polls[id]) {
            if (polls[id].kind === 'link-volumes-batch') {
                pollLinkVolumesBatchProgress(id);
            } else {
                pollMetadataWriteProgress(polls[id].seriesId, id);
            }
        }
    }

    // Registre de sondage sans toast correspondant (toast purgé ci-dessus, ou jamais eu
    // de toast persisté pour une raison ou une autre) - ne rien laisser traîner
    for (const id of Object.keys(polls)) {
        if (!toasts[id]) {
            delete polls[id];
            pollsChanged = true;
        }
    }

    if (changed) _writePersistedToasts(toasts);
    if (pollsChanged) _writePollRegistry(polls);
})();

// Sondage du scraping EBDZ manuel (voir GET /api/ebdz/scrape/status,
// blueprints/ebdz/routes.py) - contrairement à pollMetadataWriteProgress ci-dessus, pas
// de registre local: la vérité vient toujours du serveur (_manual_scrape_state côté
// Flask), pas d'un état retenu par la session JS qui l'a démarré. Un POST /scrape est une
// requête synchrone qui continue de tourner côté serveur même si le client a changé de
// page ou fermé l'onglet ("l'icone n'est pas rechargé quand je change de page et
// reviens") - donc une simple vérification au chargement de CHAQUE page (voir plus bas)
// suffit à retrouver l'état "en cours" quelle que soit la page sur laquelle on revient.
function pollEbdzScrapeStatus() {
    const toastId = 'ebdz-scrape';
    const tick = async () => {
        try {
            const response = await fetch('/api/ebdz/scrape/status');
            const data = await response.json();
            if (!data.success || !data.running) {
                dismissToast(toastId);
                return;
            }
            const progress = data.forums_total > 0 ? ` (${data.forums_done}/${data.forums_total} forum${data.forums_total > 1 ? 's' : ''})` : '';
            showToast(toastId, `Scraping EBDZ en cours...${progress}`, { icon: 'refresh-cw' });
            setTimeout(tick, 3000);
        } catch (error) {
            // Erreur réseau ponctuelle: on retente au prochain tick plutôt que d'abandonner
            setTimeout(tick, 3000);
        }
    };
    tick();
}

// Vérification unique au chargement de chaque page (pas un sondage répété tant que rien
// n'est en cours, pour ne pas cogner /scrape/status en permanence sans raison) - ne
// démarre le sondage complet ci-dessus que si un scrape est effectivement déjà en route.
(async function checkEbdzScrapeStatusOnLoad() {
    try {
        const response = await fetch('/api/ebdz/scrape/status');
        const data = await response.json();
        if (data.success && data.running) {
            pollEbdzScrapeStatus();
        } else {
            // Le scrape a pu se terminer entre le moment où le toast persistant a été
            // écrit (page précédente) et ce chargement de page-ci: restorePersistedToasts
            // (plus haut) l'a déjà réaffiché depuis localStorage sans savoir qu'il est
            // périmé, et comme aucun sondage ne démarre ici (rien "en cours"), rien
            // d'autre n'appellerait jamais dismissToast dessus - il resterait affiché
            // jusqu'à la purge par ancienneté (10 min). "scraping ebdz en cours toast ne
            // se ferme pas quand on change de page"
            dismissToast('ebdz-scrape');
        }
    } catch (error) {
        // best-effort - pas grave si ça échoue au chargement d'une page
    }
})();

function buildVolumeOptionLabel(v, { preferBedethequeTitle = false, showOwned = false } = {}) {
    let label;
    if (v.is_integral) label = `Intégrale${v.integral_number != null ? ' ' + v.integral_number : ''}`;
    else if (v.is_hs) label = `Hors-série${v.hs_number != null ? ' ' + v.hs_number : ''}`;
    else if (v.is_episode) label = `Épisode${v.episode_number != null ? ' ' + v.episode_number : ''}`;
    else if (v.volume_number != null) label = `Tome ${v.volume_number}`;
    else label = 'Édition unique';

    let ci = {};
    try { ci = typeof v.comicinfo === 'string' ? JSON.parse(v.comicinfo || '{}') : (v.comicinfo || {}); } catch (e) { ci = {}; }
    const title = preferBedethequeTitle ? (v.bedetheque_title || ci.title) : (ci.title || v.bedetheque_title);
    if (title) label += ` - ${title}`;
    // "garde le nom du volume mais mets une icone possédé" - une <option> native ne peut
    // pas contenir de balisage (pas d'icône SVG possible ici), ✓ est déjà la convention
    // du reste de l'app pour "déjà possédé"/"déjà ajouté" (voir search-results-table.js).
    if (showOwned && v.is_owned) label = `✓ ${label}`;
    return label;
}

// Barre de défilement horizontale miroir placée au-dessus des tableaux larges.
// Le tableau garde son défilement normal; cette barre permet d'atteindre les colonnes
// de droite sans devoir descendre jusqu'en bas du tableau.
(function initTopTableScrollbars() {
    const selectors = [
        '.series-list.series-table-wrapper', '.volumes-table-wrapper',
        '.monitor-series-table-wrapper', '.import-files-scroll',
        '#history-events-wrapper', '#import-history-section > div[style*="overflow-x:auto"]',
        'div[style*="overflow-x:auto"]'
    ];

    function attach(wrapper) {
        if (!wrapper || wrapper.dataset.topScrollbarReady === '1') return;
        const table = wrapper.querySelector('table');
        if (!table) return;
        wrapper.dataset.topScrollbarReady = '1';
        const top = document.createElement('div');
        top.className = 'table-scroll-top';
        top.setAttribute('aria-label', 'Défilement horizontal du tableau');
        const spacer = document.createElement('div');
        spacer.className = 'table-scroll-top-spacer';
        top.appendChild(spacer);
        wrapper.parentNode.insertBefore(top, wrapper);

        const syncWidth = () => { spacer.style.width = `${table.scrollWidth}px`; top.scrollLeft = wrapper.scrollLeft; };
        top.addEventListener('scroll', () => { wrapper.scrollLeft = top.scrollLeft; });
        wrapper.addEventListener('scroll', () => { if (top.scrollLeft !== wrapper.scrollLeft) top.scrollLeft = wrapper.scrollLeft; });
        if (window.ResizeObserver) new ResizeObserver(syncWidth).observe(table);
        syncWidth();
    }

    function scan(root = document) {
        const seen = new Set();
        selectors.forEach(selector => root.querySelectorAll(selector).forEach(wrapper => {
            if (!seen.has(wrapper)) { seen.add(wrapper); attach(wrapper); }
        }));
    }

    const start = () => {
        scan();
        new MutationObserver(() => scan()).observe(document.body, { childList: true, subtree: true });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();

(function initDraggableTableColumns() {
    const STORAGE_KEY = 'bullarr-table-column-order-v1';
    const DRAG_THRESHOLD_PX = 6;
    let savedOrders = {};
    let applying = false;
    try { savedOrders = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') || {}; } catch (_) {}

    function tableKey(table) {
        if (table.id) return `id:${table.id}`;
        const labels = [...table.querySelectorAll(':scope > thead > tr:first-child > th')]
            .map(th => (th.querySelector('.th-filterable-label') || th).textContent.trim().replace(/↕|↑|↓/g, '').trim());
        return `labels:${labels.join('|')}`;
    }

    function reorderTable(table, order) {
        if (!order || order.length !== table.rows[0]?.cells.length) return;
        const valid = order.every(i => Number.isInteger(i) && i >= 0 && i < order.length);
        if (!valid || new Set(order).size !== order.length) return;
        [...table.rows].forEach(row => {
            const cells = [...row.cells];
            // Les lignes de détail/colspan n'ont pas le même nombre de cellules que
            // l'en-tête: ne jamais les réordonner, sinon leur colspan casse le tableau.
            if (cells.length !== order.length) return;
            cells.forEach((cell, index) => {
                if (cell.dataset.dragOriginalIndex == null) cell.dataset.dragOriginalIndex = String(index);
            });
            const byOriginal = new Map(cells.map(cell => [Number(cell.dataset.dragOriginalIndex), cell]));
            order.forEach(i => { const cell = byOriginal.get(i); if (cell) row.appendChild(cell); });
        });
        const header = table.tHead?.rows[0];
        if (header) [...header.cells].forEach((th, index) => {
            th.dataset.columnVisualIndex = String(index);
            th.querySelectorAll('[data-col-index]').forEach(control => { control.dataset.colIndex = String(index); });
        });
    }

    function startReorderDrag(startEvent, table, startTh, header, key) {
        if (startEvent.target.closest('input, select, button, a, .col-resize-handle')) return;
        if (startEvent.button !== 0) return;
        const startX = startEvent.clientX;
        const startY = startEvent.clientY;
        let dragging = false;
        let fromIndex = null;
        let overTh = null;

        function onMove(moveEvent) {
            if (!dragging) {
                if (Math.abs(moveEvent.clientX - startX) < DRAG_THRESHOLD_PX && Math.abs(moveEvent.clientY - startY) < DRAG_THRESHOLD_PX) return;
                // Seuil franchi: à partir de maintenant c'est un vrai glissement, plus un
                // clic - empêche aussi la sélection de texte pendant qu'on tire.
                dragging = true;
                fromIndex = [...header.cells].indexOf(startTh);
                startTh.classList.add('column-dragging');
                document.body.classList.add('col-reordering');
            }
            moveEvent.preventDefault();
            const target = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY);
            const targetTh = target && target.closest('th');
            if (overTh && overTh !== targetTh) overTh.classList.remove('column-drag-over');
            overTh = (targetTh && targetTh !== startTh && header.contains(targetTh)) ? targetTh : null;
            if (overTh) overTh.classList.add('column-drag-over');
        }

        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            if (!dragging) return;
            startTh.classList.remove('column-dragging');
            document.body.classList.remove('col-reordering');
            if (overTh) {
                overTh.classList.remove('column-drag-over');
                const toIndex = [...header.cells].indexOf(overTh);
                if (fromIndex != null && toIndex >= 0 && fromIndex !== toIndex) {
                    const order = [...header.cells].map(cell => Number(cell.dataset.dragOriginalIndex));
                    const [moved] = order.splice(fromIndex, 1);
                    order.splice(toIndex, 0, moved);
                    savedOrders[key] = order;
                    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(savedOrders)); } catch (_) {}
                    applying = true;
                    reorderTable(table, order);
                    applying = false;
                }
            }
            // Même mécanisme que _resizeJustEnded (voir plus haut, réutilisé tel quel) -
            // avale le click fantôme qui peut suivre ce mouseup pour ne pas déclencher un
            // tri non voulu juste après un réordonnancement réussi.
            _resizeJustEnded = true;
            setTimeout(() => { _resizeJustEnded = false; }, 200);
        }

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }

    function attach(table) {
        const header = table.tHead?.rows[0];
        if (!header || header.cells.length < 2) return;
        const key = tableKey(table);
        [...header.cells].forEach((th, index) => {
            if (!th.dataset.dragOriginalIndex) th.dataset.dragOriginalIndex = String(index);
        });
        const stored = savedOrders[key];
        if (stored) {
            const current = [...header.cells].map(th => Number(th.dataset.dragOriginalIndex));
            const alreadyApplied = current.length === stored.length && current.every((value, index) => value === stored[index]);
            if (!alreadyApplied) reorderTable(table, stored);
        }
        [...header.cells].forEach(th => {
            if (th.dataset.columnDragReady === '1') return;
            th.dataset.columnDragReady = '1';
            th.addEventListener('mousedown', event => startReorderDrag(event, table, th, header, key));
        });
    }

    function scan(root = document) {
        root.querySelectorAll('table').forEach(attach);
    }
    const start = () => {
        scan();
        new MutationObserver(() => { if (!applying) scan(); }).observe(document.body, { childList: true, subtree: true });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
