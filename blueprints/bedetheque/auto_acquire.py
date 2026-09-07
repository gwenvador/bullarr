"""
Acquisition automatique à l'ajout d'une série ("quand je rajoute une série ça va chercher
automatiquement tout seul et lancer le téléchargement").

Volontairement INDÉPENDANT de blueprints/missing_monitor/ (Surveillance) - pas de table
missing_volume_monitor, pas de scheduler périodique, aucun réglage Surveillance touché.
C'est un coup d'essai unique déclenché à l'ajout d'une série depuis Bédéthèque (voir
add_series_from_bedetheque, blueprints/bedetheque/routes.py) ou à la demande explicite de
l'utilisateur (fiche série, molette d'un tome, étape Découvrir - voir
POST /api/bedetheque/auto-acquire/run) : si un tome n'est trouvé avec confiance par
aucune source, rien ne retente automatiquement plus tard.

Réutilise deux classes utilitaires de missing_monitor comme du pur code de recherche/
téléchargement (MissingVolumeSearcher, MissingVolumeDownloader) - ce sont des fonctions
génériques, pas la fonctionnalité Surveillance elle-même.

"I would prefer like in sonarr [...] I don't want the search page" - un tome trouvé mais
pas confirmé n'est jamais téléchargé automatiquement (comme Sonarr, qui se contente d'un
log "Search completed. 0 reports downloaded."). Contrairement à Sonarr cependant, ce cas
borderline est aussi mis en file d'attente (queue_manual_review, table
auto_acquire_reviews) pour une revue manuelle centralisée sur /validation (bell
notification, en haut à droite de l'app) plutôt que de forcer l'utilisateur à revenir sur
chaque fiche série individuellement pour vérifier.
"""
import re
import json
import sqlite3
import threading
from urllib.parse import unquote
import html as html_module
import time

_running_series_ids = set()
_running_series_lock = threading.Lock()
_last_auto_acquire_results = {}


def _decode_display_filename(filename):
    """Nom de fichier prêt à être écrit dans un texte affiché à l'utilisateur (résumé
    d'auto-acquire, voir download_lines ci-dessous) - un nom EBDZ reste %-encodé en base
    (voir extract_ed2k_links, blueprints/ebdz/scraper.py) et pouvait porter des entités
    HTML type "&amp;" pour les threads scrapés avant la correction de ce même scraper.
    unquote() seul (déjà utilisé ailleurs dans ce fichier pour le matching interne)
    laissait ces deux lignes du résumé affichées telles quelles ("...%20...",
    "...&amp;..."), contrairement à decodeFilename côté JS (search.js/library.js/
    history-shared.js) qui gère déjà les deux - même correction, côté serveur cette fois
    puisque ce résumé est un texte figé stocké dans action_history.detail, jamais
    retraité au moment de l'affichage."""
    if not filename:
        return filename
    try:
        filename = unquote(filename)
    except Exception:
        pass
    return html_module.unescape(filename)


def _ensure_manual_review_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auto_acquire_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id INTEGER,
            series_title TEXT NOT NULL,
            volume_number INTEGER,
            volume_label TEXT,
            candidates_json TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
    """)
    # Older installations created series_id as NOT NULL.  Unresolved matches
    # (the series is not in the library yet) legitimately have no id, so make
    # that column nullable while preserving existing review rows.
    columns = conn.execute('PRAGMA table_info(auto_acquire_reviews)').fetchall()
    series_column = next((row for row in columns if row[1] == 'series_id'), None)
    if series_column and series_column[3]:
        conn.execute('DROP INDEX IF EXISTS idx_auto_acquire_reviews_status')
        conn.execute('ALTER TABLE auto_acquire_reviews RENAME TO auto_acquire_reviews_legacy')
        conn.execute("""
            CREATE TABLE auto_acquire_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id INTEGER,
                series_title TEXT NOT NULL,
                volume_number INTEGER,
                volume_label TEXT,
                candidates_json TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            )
        """)
        conn.execute("""
            INSERT INTO auto_acquire_reviews
                (id, series_id, series_title, volume_number, volume_label,
                 candidates_json, reason, status, created_at, resolved_at)
            SELECT id, series_id, series_title, volume_number, volume_label,
                   candidates_json, reason, status, created_at, resolved_at
            FROM auto_acquire_reviews_legacy
        """)
        conn.execute('DROP TABLE auto_acquire_reviews_legacy')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_auto_acquire_reviews_status ON auto_acquire_reviews(status, created_at)')


def _manual_review_candidates(candidates, series_title, volume_number):
    """Keep only high-scoring candidates below the automatic threshold.

    "there should not be a difference between one-shot and tomes" - le seuil de confiance
    utilisé pour décider si un candidat mérite d'être TÉLÉCHARGÉ AUTOMATIQUEMENT reste
    volontairement différent pour un one-shot (_ONESHOT_TITLE_MATCH_THRESHOLD, plus strict -
    voir _best_confident_result: sans numéro de tome à confirmer, l'identité est moins sûre,
    un one-shot mal identifié serait téléchargé à tort) qu'pour un tome
    (_VOLUME_TITLE_MATCH_THRESHOLD) - ÇA, c'est une vraie question de sécurité du
    téléchargement automatique, pas touché ici. Mais CETTE fonction ne décide pas ça : elle
    filtre seulement ce qui vaut la peine d'être MONTRÉ pour une revue humaine sur
    /validation, une fois qu'un candidat n'a de toute façon PAS été téléchargé
    automatiquement - reprendre le même seuil plus strict des one-shots ici n'avait plus de
    justification et cachait des candidats qu'un humain aurait pu vouloir voir (visible
    seulement via le repli "candidats bruts" de get_manual_reviews quand le filtrage ne
    laissait rien). Bande unique pour les deux cas désormais."""
    from blueprints.bedetheque.scraper import BedethequeScraper
    minimum = 0.45
    maximum = _VOLUME_TITLE_MATCH_THRESHOLD
    selected = []
    for candidate in candidates or []:
        if volume_number is not None and candidate.get('unconfirmed_volume'):
            continue
        text = unquote(candidate.get('filename') or candidate.get('title') or '')
        score = BedethequeScraper._match_score(series_title, text)
        if volume_number is not None:
            identity = _series_identity_matches(text, series_title, volume_number)
        else:
            identity = _oneshot_title_contained(text, series_title)
        effective = 1.0 if identity else score
        if minimum <= effective < maximum:
            selected.append(candidate)
    return selected


def queue_manual_review(series_id, series_title, volume_number, volume_label, candidates, reason, force_candidates=False):
    """Persist borderline results, or explicitly rejected packs, for manual review."""
    candidates = list(candidates or []) if force_candidates else _manual_review_candidates(candidates, series_title, volume_number)
    if not candidates:
        return
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    try:
        _ensure_manual_review_table(conn)
        payload = json.dumps(candidates, ensure_ascii=False, default=str)
        existing = conn.execute(
            "SELECT id FROM auto_acquire_reviews WHERE series_id = ? AND volume_number IS ? AND status = 'pending'",
            (series_id, volume_number)
        ).fetchone()
        if existing:
            conn.execute(
                'UPDATE auto_acquire_reviews SET series_title = ?, volume_label = ?, candidates_json = ?, reason = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?',
                (series_title, volume_label, payload, reason, existing[0])
            )
        else:
            conn.execute(
                'INSERT INTO auto_acquire_reviews (series_id, series_title, volume_number, volume_label, candidates_json, reason) VALUES (?, ?, ?, ?, ?, ?)',
                (series_id, series_title, volume_number, volume_label, payload, reason)
            )
        conn.commit()
    finally:
        conn.close()


def queue_series_match_review(series_title, candidates, reason='La série n’a pas pu être associée automatiquement.'):
    """Queue a source whose series could not be resolved at all.

    Unlike a borderline volume match, this review has no library series id yet;
    the user must match the displayed title before importing it.
    """
    queue_manual_review(None, series_title, None, 'Série à matcher', candidates, reason, force_candidates=True)


def get_manual_reviews():
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_manual_review_table(conn)
        rows = conn.execute("""
            SELECT r.id, r.series_id, r.series_title, r.volume_number, r.volume_label,
                   r.candidates_json, r.reason, r.status, r.created_at, r.resolved_at,
                   s.bedetheque_scenaristes, s.bedetheque_dessinateurs
            FROM auto_acquire_reviews r
            LEFT JOIN series s ON s.id = r.series_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at DESC, r.id DESC
        """).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                candidates = json.loads(item.pop('candidates_json') or '[]')
            except (TypeError, ValueError):
                candidates = []
                item.pop('candidates_json', None)
            filtered_candidates = _manual_review_candidates(candidates, item['series_title'], item['volume_number'])
            # Une validation manuelle doit rester visible même si aucun candidat ne
            # atteint le seuil borderline: l'automatisation a précisément signalé qu'une
            # décision humaine était nécessaire. Conserver au maximum 20 résultats.
            item['candidates'] = filtered_candidates or [
                candidate for candidate in candidates[:20]
                if candidate.get('filename') or candidate.get('title')
            ]
            if not item['candidates']:
                continue
            output.append(item)
        return output
    finally:
        conn.close()


def download_manual_review_candidate(review_id, candidate_index):
    """Send the selected candidate from the review queue to its configured client."""
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_manual_review_table(conn)
        row = conn.execute("SELECT * FROM auto_acquire_reviews WHERE id = ? AND status = 'pending'", (review_id,)).fetchone()
        if not row:
            return False, 'Validation introuvable ou déjà traitée'
        try:
            candidates = json.loads(row['candidates_json'] or '[]')
            candidate = candidates[int(candidate_index)]
        except (TypeError, ValueError, IndexError, KeyError):
            return False, 'Candidat introuvable'
        success, message = _download_result(current_app._get_current_object(), candidate, row['series_id'], row['series_title'], row['volume_number'])
        if success:
            conn.execute("UPDATE auto_acquire_reviews SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP WHERE id = ?", (review_id,))
            conn.commit()
        return success, message
    finally:
        conn.close()


def resolve_manual_review(review_id):
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    try:
        _ensure_manual_review_table(conn)
        cursor = conn.execute("UPDATE auto_acquire_reviews SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'", (review_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def match_manual_review_series(review_id, bedetheque_url, library_id=None):
    """Rattache une ligne "Série à matcher" (queue_series_match_review, series_id NULL) à
    une fiche Bédéthèque choisie manuellement sur /validation - "une fois matchée la ligne
    devrait s'afficher comme matché". Réutilise POST /api/bedetheque/add-series (via
    test_client, même pattern que _post_to_client/downloader.py pour un appel interne à un
    autre endpoint plutôt que dupliquer sa logique de création - nommage de dossier,
    univers, tomes manquants...) : une série déjà présente sous ce titre dans la
    bibliothèque cible est réutilisée telle quelle (already_exists), sinon une nouvelle
    ligne série est créée. Ne fait AUCUN téléchargement ici - une fois series_id posé,
    get_manual_reviews() renvoie cette ligne avec un series_id non nul et le frontend
    bascule de lui-même vers les actions de téléchargement par candidat (même logique que
    pour une ligne "Pack à confirmer"/"Tome N" qui avait déjà un series_id dès la mise en
    file).

    library_id: optionnel - si omis et qu'une seule bibliothèque existe, elle est utilisée
    automatiquement (comme index.js pour l'ouverture directe d'une bibliothèque unique) ;
    avec plusieurs bibliothèques configurées, l'appelant doit préciser laquelle."""
    from flask import current_app
    conn = sqlite3.connect(current_app.config['DATABASE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_manual_review_table(conn)
        row = conn.execute("SELECT * FROM auto_acquire_reviews WHERE id = ? AND status = 'pending'", (review_id,)).fetchone()
        if not row:
            return False, 'Validation introuvable ou déjà traitée', None

        if not library_id:
            libraries = conn.execute('SELECT id FROM libraries').fetchall()
            if len(libraries) == 1:
                library_id = libraries[0]['id']
            elif not libraries:
                return False, 'Aucune bibliothèque configurée', None
            else:
                return False, 'Plusieurs bibliothèques configurées : précisez laquelle utiliser.', None

        response = current_app.test_client().post(
            '/api/bedetheque/add-series',
            json={'url': bedetheque_url, 'library_id': library_id, 'skip_auto_acquire': True}
        )
        data = response.get_json(silent=True) or {}
        if not data.get('success'):
            return False, data.get('error') or 'Impossible de créer/associer la série sur Bédéthèque', None

        series_id = data['series_id']
        conn.execute('UPDATE auto_acquire_reviews SET series_id = ?, volume_label = NULL WHERE id = ?', (series_id, review_id))
        conn.commit()
        return True, None, series_id
    finally:
        conn.close()


def get_auto_acquire_status(series_id):
    """Retourne le dernier résultat de recherche automatique pour l'interface."""
    with _running_series_lock:
        if series_id in _running_series_ids:
            return {'running': True}
        return dict(_last_auto_acquire_results.get(series_id) or {'running': False})


# Formats reconnus dans un nom de RELEASE (pas un fichier réel sur disque) par simple
# mot-clé, PAS par extension - un titre Prowlarr/torrent n'a presque jamais de vraie
# extension de fichier ("Videur (T01- a T12) FR CBZ & PDF", "...[PDF]-NOTAG": le dernier
# segment après un point serait "notag"/"pdf-notag", pas un format exploitable). Même
# raisonnement déjà établi côté frontend pour ce problème identique sur les résultats
# Prowlarr (voir detectResultFormat/SEARCH_FORMAT_PRIORITY_DEFAULT,
# static/js/search-results-table.js, "pour prowlarr les fichiers ne sont pas retournés
# avec leur extension") - ordre de préférence identique (cbz/zip > cbr/rar > pdf), aligné
# sur FORMAT_PRIORITY (blueprints/library/routes.py) pour le reste de l'app. Une release
# qui mentionne plusieurs formats à la fois ("CBZ & PDF") retient le meilleur des deux.
_PACK_FORMAT_KEYWORDS = (
    ('cbz', re.compile(r'\bcbz\b', re.IGNORECASE)),
    ('zip', re.compile(r'\bzip\b', re.IGNORECASE)),
    ('cbr', re.compile(r'\bcbr\b', re.IGNORECASE)),
    ('rar', re.compile(r'\brar\b', re.IGNORECASE)),
    ('pdf', re.compile(r'\bpdf\b', re.IGNORECASE)),
)


def _detect_pack_format(item_title):
    """Meilleur format mentionné dans un nom de release, par mot-clé (voir
    _PACK_FORMAT_KEYWORDS) - None si aucun des formats connus n'y apparaît."""
    for fmt, pattern in _PACK_FORMAT_KEYWORDS:
        if pattern.search(item_title or ''):
            return fmt
    return None


# Délai entre deux tomes cherchés, au-delà du throttling déjà interne à
# MissingVolumeSearcher pour Prowlarr - une série ajoutée vide peut avoir des dizaines de
# tomes manquants d'un coup, pas de raison de marteler EBDZ/Telegram/fourtoutici non plus.
_DELAY_BETWEEN_VOLUMES_SECONDS = 3

_ONESHOT_TITLE_MATCH_THRESHOLD = 0.8

_PACK_TITLE_MATCH_THRESHOLD = 0.8
# Même seuil strict pour les tomes numérotés : si l'identité exacte avant le numéro
# n'est pas prouvée par _series_identity_matches, une similarité seulement moyenne doit
# rester dans /validation plutôt que déclencher un téléchargement automatique.
_VOLUME_TITLE_MATCH_THRESHOLD = 0.8

_SOURCE_LABELS = {'ebdz': 'EBDZ', 'prowlarr': 'Prowlarr', 'telegram': 'Telegram', 'fourtoutici': 'fourtoutici'}


def _result_source_link(result):
    """Lien vers la PAGE de la release (fil de forum EBDZ, page Prowlarr, message
    Telegram), PAS le lien de téléchargement direct - même calcul que
    _searchResultSourceLinkUrl côté static/js/search-results-table.js (équivalent Python,
    utile ici car ce module tourne côté serveur sans accès à ce JS). fourtoutici: essayé
    avec le lien de téléchargement direct, abandonné - "Direct hotlinking not allowed",
    leur serveur bloque toute navigation directe vers ce lien (contrôle de Referer), seul
    le téléchargement propre de l'app (requête serveur à serveur) fonctionne. Aucun lien
    utilisable n'existe pour cette source."""
    source = result.get('source')
    if source == 'prowlarr':
        return result.get('info_url') or ''
    if source == 'telegram':
        channel, message_id = result.get('channel'), result.get('message_id')
        return f"https://t.me/{channel}/{message_id}" if channel and message_id else ''
    if source == 'fourtoutici':
        return ''
    return result.get('thread_url') or ''


def _download_result(app, result, series_id, title, vol_num):
    ""
    filename = result.get('filename') or result.get('title')

    # Ne pas soumettre une seconde fois un fichier déjà accepté par un client mais
    # encore en cours ou en attente dans active_downloads.
    from blueprints.missing_monitor.downloader import find_pending_download_duplicate
    with app.app_context():
        duplicate = find_pending_download_duplicate(filename or title, series_id, vol_num)
    if duplicate:
        client_label = duplicate.get('client') or 'client'
        return False, f"Déjà ajouté ({client_label}), téléchargement en cours ou en attente"

    source = result.get('source')
    if source == 'telegram':
        from blueprints.telegram_channels.routes import _require_connected_config
        from blueprints.telegram_channels.scraper import download_channel_file_background

        config = _require_connected_config()
        if not config:
            return False, "Telegram non connecté"
        target_dir = app.config.get('TELEGRAM_IMPORT_DIRECTORY')
        if not target_dir:
            return False, "Répertoire d'import Telegram non configuré"
        download_channel_file_background(
            config['api_id'], config['api_hash_decrypted'], config['session_decrypted'],
            result.get('channel'), result.get('message_id'), target_dir,
            channel_title=result.get('channel_title'), app=app,
            pending_title=filename, series_id=series_id, volume_number=vol_num
        )
        return True, f"{filename or title} : téléchargement Telegram démarré"

    if source == 'fourtoutici':
        from blueprints.fourtoutici.scraper import download_fourtoutici_file_background, get_fourtoutici_base_url

        target_dir = app.config.get('FOURTOUTICI_IMPORT_DIRECTORY')
        if not target_dir:
            return False, "Répertoire d'import fourtoutici non configuré"
        download_fourtoutici_file_background(
            result.get('file_id'), filename, target_dir, get_fourtoutici_base_url(), app=app,
            pending_title=filename, series_id=series_id, volume_number=vol_num
        )
        return True, f"{filename or title} : téléchargement fourtoutici démarré"

    # EBDZ (ed2k) / Prowlarr (magnet/torrent) - auto-détection du client par
    # send_torrent_download selon le schéma du lien. download_url en repli - "aussi on
    # dirait que la recherche de prowlarr ne s'affiche plus" (voir _deduplicate_and_rank,
    # missing_monitor/searcher.py): certains indexeurs Prowlarr ne renseignent que
    # 'downloadUrl', jamais 'link' - constaté en réel sur le choix de pack Videur
    # (Torr9, 'link': '', seul 'download_url' rempli). Ce repli existait déjà pour la clé
    # de dédup mais pas ici: sans lui, un résultat sélectionné avec 'link' vide échouait
    # silencieusement au téléchargement ("Aucun lien de téléchargement") malgré un choix
    # par ailleurs correct.
    link = result.get('link') or result.get('download_url')
    if not link:
        return False, "Aucun lien de téléchargement"
    from blueprints.missing_monitor.downloader import MissingVolumeDownloader
    return MissingVolumeDownloader().send_torrent_download(
        link, title, vol_num, series_id=series_id, filename=filename,
        source=source, source_link=_result_source_link(result)
    )


# Articles français déplacés en fin de nom par beaucoup de releases/Telegram
# ("Tueur (le)", "Obsession du pouvoir, L'") - partagé par _series_identity_matches et
# _oneshot_title_contained, un seul jeu d'articles plutôt que deux copies.
_LEADING_ARTICLES = {'le', 'la', 'les', 'l', 'un', 'une', 'du', 'des', 'de'}

# Préfixe de release courant sur EBDZ avant le vrai titre ("BD.FR.-.Guerre d'Alan...",
# "1BD.FR.-.Kenya...", "[BD] L'Or Des Marées...", "[BD Fr] - Kenya...") - un chiffre de
# tri de forum optionnel, puis un tag "BD"/"BD FR" entre crochets ou suivi de points/tiret.
# _series_identity_matches exige que TOUT ce qui précède le numéro de tome soit
# EXACTEMENT le titre (voir sa docstring) - ce tag, présent sur une grosse partie des
# releases réelles, cassait cette égalité alors que le fichier est le bon (mesuré: 48
# imports réels rejetés à tort pour cette seule raison, sur l'historique complet).
#
# "[EBOOK] Boule et Bill - tome 26 - ..." / "[EBOOK] BANDE DESSINEE - Boule et Bill - T31
# - ..." (constaté en réel, fourtoutici) - même besoin que "[BD]"/"BD FR" ci-dessus, tag
# différent, avec en plus un "BANDE DESSINEE -" parfois intercalé entre le tag et le vrai
# titre.
_RELEASE_PREFIX_RE = re.compile(
    r'^\d*(?:\[bd(?:\s*fr)?\]\s*-?\s*|bd[\s.]*fr[\s.]*[-.]+\s*'
    r'|\[e-?book\]\s*(?:bande[\s._]*dessin[eé]e\s*-\s*)?)',
    re.IGNORECASE
)


def _normalize_release_text(text):
    """normalize_search_text (blueprints/search/routes.py) ne traite pas "_" comme un
    séparateur de mots - elle est réutilisée telle quelle par le scraping EBDZ/Telegram
    pour des colonnes déjà pré-calculées en base (thread_title_normalized/
    filename_normalized), la modifier casserait ces colonnes stockées sans un backfill.
    Un nom de release entièrement en underscores ("Journal_inquiet_d'Istanbul_Tome_02_
    fr.cbz") ne se découpait donc jamais en mots distincts ici, où ce risque n'existe pas
    (rien n'est stocké) - remplacé par un espace localement avant normalize_search_text."""
    from blueprints.search.routes import normalize_search_text
    return normalize_search_text((text or '').replace('_', ' '))


def _strip_trailing_qualifier(title):
    """Retire un qualificatif de désambiguïsation local en fin de titre de série - un
    groupe parenthésé final ("Le Gaulois (Autres)") et/ou un marqueur "-NN-" isolé
    ("Boule et Bill -02- (Édition actuelle)", vu en réel: distingue localement plusieurs
    éditions/collections de la même série) - avant comparaison à un nom de release réel.

    "check verron and tell me what you would pick... go for each pattern" - bug réel
    trouvé lors d'un test à blanc: `_identity_tokens(title)` inclut ces mots/jetons tels
    quels, qui ne peuvent PAR CONSTRUCTION jamais apparaître dans un vrai nom de release
    ("Boule et Bill - Tome 46 - ...", jamais "... -02- (Édition actuelle)..." - ce
    qualificatif n'existe que pour distinguer deux entrées LOCALES de cette bibliothèque,
    pas pour décrire l'œuvre elle-même sur une release). Résultat mesuré sur "Boule et
    Bill -02- (Édition actuelle)": 22 des 35 tomes manquants avaient un candidat au bon
    numéro, correctement confirmé, mais rejeté par _series_identity_matches/score faute
    de ce nettoyage - le même mécanisme touche "Le Gaulois (Autres)" (0/32 matchés).

    Retourne le titre nettoyé (peut être inchangé si aucun des deux motifs n'est présent)
    - jamais None: l'appelant compare toujours ce résultat EN PLUS du titre complet
    d'origine (voir ses appelants), jamais à sa place, pour ne pas perdre un titre dont le
    groupe parenthésé fait légitimement partie du nom (ex. un vrai sous-titre BD)."""
    stripped = re.sub(r'\s*\([^)]*\)\s*$', '', title or '').strip()
    stripped = re.sub(r'\s*-\s*\d{1,3}\s*-\s*$', '', stripped).strip()
    return stripped


def _identity_tokens(value):
    """Liste de mots "propres" d'un titre/nom de fichier, pour _series_identity_matches
    et _oneshot_title_contained: crochets/parenthèses remplacés par un espace AVANT de
    découper (pas un strip() par mot, qui ne touche que les bords - "Crown(Le)" collé
    sans espace, ex. série "Testament du Capitaine Crown(Le)", gardait son "(" au milieu
    du mot), articles français retirés (déterminants, jamais distinctifs entre deux
    séries - et un nom de release n'a pas forcément le même nombre/ordre d'articles que
    le titre local, voir _LEADING_ARTICLES), et jetons sans aucun caractère alphanumérique
    filtrés ("-" isolé d'un titre à sous-titre séparé autrement côté fichier)."""
    tokens = re.sub(r'[()\[\]{}]', ' ', _normalize_release_text(value)).split()
    return [token for token in tokens if token not in _LEADING_ARTICLES and any(c.isalnum() for c in token)]


def _series_identity_matches(filename, title, volume_number):
    ""
    if volume_number is None:
        return False
    text = unquote(filename or '')
    text = re.sub(r'(?i)\.(?:cbz|cbr|cb7|zip|rar|pdf|epub)$', '', text)
    text = _RELEASE_PREFIX_RE.sub('', text)
    marker = re.search(
        rf'(?i)(?:^|[\s._-])(?:t(?:ome)?|vol(?:ume)?|int|hs|part(?:ie)?|#)?[\s._-]*0*{int(volume_number)}(?=$|[\s._-])',
        text,
    )
    if not marker:
        return False
    prefix = text[:marker.start()].strip(' ._-')

    # Articles retirés entièrement plutôt que déplacés en fin de liste: "Guerre d'Alan"
    # (titre local, sans article) vs "Guerre d'Alan (La)" (nom de release, article ajouté)
    # ne partagent aucun article en commun à réordonner - un des deux côtés n'en a
    # simplement pas. Et "Les Passagers du Vent" vs "Passagers du Vent (Les)" (deux
    # articles, "les"+"du", mais pas dans le même ordre relatif) posait le même problème
    # même quand les DEUX côtés avaient des articles - voir _identity_tokens.
    if not prefix:
        return False
    prefix_tokens = tuple(_identity_tokens(prefix))
    # _strip_trailing_qualifier: voir sa docstring - comparé EN PLUS du titre complet,
    # jamais à sa place (un titre sans qualificatif local reste inchangé par cet appel).
    return prefix_tokens == tuple(_identity_tokens(title)) \
        or prefix_tokens == tuple(_identity_tokens(_strip_trailing_qualifier(title)))


def _oneshot_title_contained(filename, title):
    ""
    unprefixed = _RELEASE_PREFIX_RE.sub('', filename or '')
    leading_segment = re.split(r'[\(\[]', unprefixed, maxsplit=1)[0]
    filename_tokens = _identity_tokens(leading_segment)

    # Comparer des séquences de jetons, jamais une sous-chaîne concaténée. L'ancien
    # `" ".join(title_tokens) in " ".join(filename_tokens)` validait « Ravage » dans
    # « Caravage »: les séparateurs avaient disparu après concaténation, et un pack
    # Caravage a donc été considéré comme un pack de Ravage. Une identité ne peut être
    # confirmée que si les jetons du titre apparaissent comme mots contigus dans le
    # segment initial du nom de release.
    def contains_token_sequence(candidate_tokens, expected_tokens):
        if not candidate_tokens or len(expected_tokens) < 2:
            return False
        width = len(expected_tokens)
        return any(candidate_tokens[i:i + width] == expected_tokens
                   for i in range(len(candidate_tokens) - width + 1))

    if contains_token_sequence(filename_tokens, _identity_tokens(title)):
        return True
    stripped_tokens = _identity_tokens(_strip_trailing_qualifier(title))
    return contains_token_sequence(filename_tokens, stripped_tokens)


def _availability_values(result):
    """Valeurs de disponibilité explicitement mesurées sur un résultat."""
    values = []
    for key in ('seeders', 'peers', 'sources', 'source_count', 'availability'):
        value = result.get(key)
        if value is None or value == '':
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _availability_count(result):
    """Meilleur indicateur disponible, 0 si aucune mesure n'est connue."""
    return max(_availability_values(result), default=0.0)


def _is_explicitly_unavailable(result):
    """True seulement si une mesure existe et confirme zéro source/peer/seeder.

    L'absence de mesure reste « inconnue », jamais transformée en faux zéro. La règle
    d'automatisation propre à chaque source est appliquée par _is_auto_download_eligible.
    """
    values = _availability_values(result)
    return bool(values) and max(values) <= 0


# "Fille du destin T2 Les disparus de Nanzy... epub" téléchargé automatiquement pour un
# one-shot BD - un ebook texte (roman) n'est jamais le bon fichier pour une BD, quel que
# soit le score de correspondance du titre. monitored_extensions (library_import_config.json)
# ne liste même pas .epub: un tel fichier ne sera de toute façon jamais importé, seulement
# téléchargé pour rien. Vérifié sur le NOM du résultat (filename/title), pas sur un champ
# de format structuré - aucune source (EBDZ/Prowlarr/Telegram/fourtoutici) n'en fournit un.
_NON_COMIC_EXTENSIONS_RE = re.compile(r'(?i)\.(?:epub|mobi|azw3?|djvu|txt)$')


def _is_non_comic_format(result):
    filename = result.get('filename') or result.get('title') or ''
    return bool(_NON_COMIC_EXTENSIONS_RE.search(unquote(filename)))


def _is_auto_download_eligible(result):
    """Disponibilité suffisante pour un téléchargement sans supervision.

    EBDZ est activement sondé : zéro est indisponible et une réponse inconnue signifie
    que Bullarr n'est pas assez sûr pour automatiser ; le candidat reste destiné à
    /validation. Un torrent (Prowlarr) n'est prouvé disponible que par un compte de
    SEEDS positif - `peers`/`leechers` compte des téléchargeurs, pas des sources qui
    servent réellement le fichier, et Prowlarr renseigne systématiquement `leechers=0`
    quand l'indexeur ne le fournit pas (voir blueprints/prowlarr/search.py) : l'accepter
    comme preuve de disponibilité ferait passer un torrent à 0 seed/N leechers. Pour les
    autres sources, l'absence de métrique reste admissible.
    """
    if _is_non_comic_format(result):
        return False
    if result.get('source') in ('prowlarr', 'torrent'):
        try:
            seeders = float(result.get('seeders') or 0)
        except (TypeError, ValueError):
            seeders = 0
        return seeders > 0
    if _is_explicitly_unavailable(result):
        return False
    if result.get('source') != 'ebdz':
        return True
    value = result.get('availability')
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _enrich_ed2k_availability(results):
    """Ajoute la disponibilité EBDZ avant le classement automatique, en un appel groupé."""
    links = list(dict.fromkeys(
        result.get('link') for result in (results or [])
        if result.get('source') == 'ebdz' and result.get('link')
    ))
    if not links:
        return results
    try:
        from blueprints.emule.ed2k_stats import get_ed2k_availability_bulk
        availability = get_ed2k_availability_bulk(links)
    except Exception:
        return results
    for result in results:
        link = result.get('link')
        if result.get('source') == 'ebdz' and link in availability:
            result['availability'] = availability[link]
    return results


_BLACK_AND_WHITE_RE = re.compile(
    r'(?i)(?:\bN[\s._&+-]*B\b|\bnoir[\s._-]*(?:et|&)[\s._-]*blanc\b|\bblack[\s._-]*(?:and|&)[\s._-]*white\b)'
)


def _is_black_and_white_release(item_title):
    """Détecte un marqueur explicite NB/noir et blanc dans un nom de release."""
    return bool(_BLACK_AND_WHITE_RE.search(item_title or ''))

def _is_live_torrent(result):
    """Un torrent Prowlarr réellement disponible pour téléchargement."""
    return result.get('source') in ('prowlarr', 'torrent') and _availability_count(result) > 0


def _best_confident_result(results, vol_num, title, source_order=None):
    """Résultat à télécharger automatiquement, ou None si aucun n'est assez confiant.

    Pour un tome numéroté (vol_num défini), réutilise le flag unconfirmed_volume déjà
    calculé par MissingVolumeSearcher (comparaison stricte au numéro demandé, voir
    _confirms_requested_volume). Pour une recherche SANS numéro (one-shot sans intégrale/
    HS/tome identifiable), ce flag ne prouve rien: _confirms_requested_volume renvoie
    confirmed=True dès qu'aucun numéro précis n'est demandé - pensé pour la recherche
    MANUELLE, où l'utilisateur choisit ensuite lui-même parmi les résultats affichés, pas
    pour un téléchargement sans supervision. Repli sur une similarité de titre (même
    fonction que le matching de série Bédéthèque, BedethequeScraper._match_score) entre
    le titre recherché et celui/le nom de fichier du résultat, pour ne jamais télécharger
    à l'aveugle le premier résultat venu sur un titre générique.

    source_order est l'ordre de préférence configuré par l'utilisateur. Il ne départage
    que des candidats de confiance équivalente : la disponibilité d'une source ne doit
    pas annuler une préférence explicite (ex. Telegram avant EBDZ)."""
    from blueprints.bedetheque.scraper import BedethequeScraper
    best, best_score = None, 0.0
    best_rank = None
    scored_results = []
    source_priority = {
        source: (len(source_order) - index)
        for index, source in enumerate(source_order or [])
    }
    for r in results:
        if not _is_auto_download_eligible(r):
            continue
        # is not None (pas la simple vérité de vol_num) - un tome 0 réel (voir
        # _series_identity_matches) doit filtrer les résultats non confirmés exactement
        # comme n'importe quel autre numéro, pas comme une recherche série entière.
        if vol_num is not None and r.get('unconfirmed_volume'):
            continue
        candidate = unquote(r.get('filename') or r.get('title') or '')
        if vol_num is None:
            numbered_marker = re.search(
                r'(?i)(?:^|[\s._-])(?:t(?:ome)?|vol(?:ume)?|int|hs|part(?:ie)?|#)[\s._-]*0*(\d+)(?=$|[\s._-])',
                candidate,
            )
            if numbered_marker and (
                int(numbered_marker.group(1)) != 1
                or not _series_identity_matches(candidate, title, int(numbered_marker.group(1)))
            ):
                continue
        # Les noms de releases ajoutent souvent une extension, l'année, le format
        # et des marqueurs OS/qualité. Ces suffixes ne font pas partie du titre et
        # diluaient le score d'un one-shot pourtant exact (ex. « Le télescope
        # (2009).cbr » passait de 1.0 à 0.5). On les retire uniquement pour le
        # calcul de confiance ; le nom original reste celui envoyé au client.
        score_candidate = re.sub(r'(?i)\.(?:cbz|cbr|cb7|zip|rar|pdf|epub)$', '', candidate)
        score_candidate = re.sub(r'(?i)\b(?:19|20)\d{2}\b', ' ', score_candidate)
        score_candidate = re.sub(r'(?i)\b(?:one[ -]?shot|os|digital|scan|ebook|cbz|cbr|cb7|zip|rar|pdf)\b', ' ', score_candidate)
        score = BedethequeScraper._match_score(title, score_candidate)
        # Une identité de série exacte remplace le score lexical par la confiance
        # maximale: les sous-titres et articles déplacés (tome numéroté) ou les infos de
        # release ajoutées après le titre (one-shot, voir _oneshot_title_contained) ne
        # doivent pas bloquer un bon fichier.
        identity_match = _series_identity_matches(candidate, title, vol_num) if vol_num is not None else _oneshot_title_contained(candidate, title)
        effective_score = 1.0 if identity_match else score
        scored_results.append((r, effective_score))
        rank = (
            effective_score,
            source_priority.get(r.get('source'), 0),
            not _is_black_and_white_release(candidate),
            _availability_count(r),
        )
        if best_rank is None or rank > best_rank:
            best, best_score, best_rank = r, effective_score, rank

    threshold = _VOLUME_TITLE_MATCH_THRESHOLD if vol_num is not None else _ONESHOT_TITLE_MATCH_THRESHOLD
    if best is None or best_score < threshold:
        return None

    # Un EBDZ arrivé jusque-là possède désormais une disponibilité positive confirmée
    # (_is_auto_download_eligible). Un torrent Prowlarr suffisamment bien matché et actif
    # peut encore passer devant lui ; le score et la préférence couleur restent toutefois
    # prioritaires, puis la disponibilité départage des résultats équivalents.
    if best.get('source') == 'ebdz':
        live_torrents = [
            (result, score) for result, score in scored_results
            if score >= threshold and _is_live_torrent(result)
        ]
        if live_torrents:
            live_torrents.sort(
                key=lambda item: (
                    item[1],
                    not _is_black_and_white_release(item[0].get('filename') or item[0].get('title') or ''),
                    _availability_count(item[0]),
                ),
                reverse=True,
            )
            live_best = live_torrents[0][0]
            # La disponibilité torrent ne doit pas réintroduire la priorité NB après le
            # classement qualité effectué plus haut : une version couleur EBDZ valide
            # reste préférable à un torrent noir et blanc, même immédiatement disponible.
            if _is_black_and_white_release(live_best.get('filename') or live_best.get('title') or '')                     and not _is_black_and_white_release(best.get('filename') or best.get('title') or ''):
                return best
            return live_best

    return best


def _has_pack_keyword(item_title):
    """Le mot "PACK" apparaît-il explicitement dans ce nom (ex.
    "Videur.BD.HD.PACK.2024.FR.PDF-STCTEAM") - réutilise `LibraryScanner.parse_filename`
    (`is_pack`, scanner.py) plutôt qu'une regex séparée: même détection que celle qui
    alimente la colonne Volume des résultats de recherche manuelle
    (`_parsed_volume_label`, searcher.py, "PACK" affiché en premier)."""
    from blueprints.library.scanner import LibraryScanner
    parsed = LibraryScanner.parse_filename(item_title or '')
    # Un résultat nommé « INTEGRALE » peut désigner le pack complet de la série,
    # même sans plage explicite de tomes. Le parseur commun le marque alors comme
    # candidat pack ; les intégrales numérotées restent traitées comme un album ciblé.
    return bool(parsed.get('is_pack'))


def _detect_pack_size(item_title):
    """Nombre de tomes couverts par un résultat "pack" (une seule release qui regroupe
    plusieurs tomes, ex. "Le Scorpion T01 a T14 et 01HS [CBZ]") - None si le titre ne
    précise aucune plage exploitable (pas un pack, ou nommage trop ambigu pour l'affirmer).

    Réutilise `LibraryScanner._parse_integral_tome_range` (scanner.py) tel quel plutôt
    qu'un nouveau parseur : une plage "T1 à T14" a exactement la même forme dans un titre
    d'intégrale que dans le nom d'un pack multi-tomes - même garde-fous déjà éprouvés
    (exige le mot "tome"/l'abréviation "T" pour ne pas confondre avec une plage d'années
    ou un numéro de sous-titre, "Marc Rallier - T66 - 100.000.000 $" ne devient jamais
    "tomes 66 à 100")."""
    from blueprints.library.scanner import LibraryScanner
    tome_range = LibraryScanner._parse_integral_tome_range(item_title or '')
    if not tome_range:
        return None
    start, end = tome_range
    return end - start + 1


def _best_pack_result(results, title):
    """Meilleur "pack" parmi des résultats de recherche série entière (pas un tome
    précis) - "au lieu de faire une recherche pour chaque volume fait une recherche pour
    la serie entiere si il y a l'option pack active... prends les fichiers qui ont le
    plus de volumes (peu importe la preference des sources)". Classement dans cet ordre
    (chaque critère ne départage qu'à égalité du précédent) :
    1. Taille de la plage CONFIRMÉE (`_detect_pack_size`, un résultat sans plage
       détectée compte comme inconnu, PAS comme 0 - voir plus bas pourquoi ce n'est pas
       la même chose) - une plage vérifiée ("T01 à T41", 41 tomes prouvés) est une preuve
       strictement plus fiable qu'une simple étiquette "PACK" sans le moindre chiffre.
    2. Présence du mot "PACK" dans le nom (`_has_pack_keyword`) - ne départage donc plus
       qu'à taille CONFIRMÉE égale (y compris "aucune des deux plages n'est connue").
    3. Couleur avant un marqueur explicite NB/noir et blanc, sans exclure NB si c'est
       le seul candidat valide.
    4. Format (cbz/zip > cbr/rar > pdf) - même hiérarchie que le reste de l'app
       (`FORMAT_PRIORITY`/`get_format_priority`, blueprints/library/routes.py).
    5. Disponibilité mesurée - départage final ; zéro est toujours exclu. Pour EBDZ,
       une mesure absente est trop incertaine pour l'automatique et part en /validation.

    "check verron and tell me what you would pick... go for each pattern" - l'ordre
    ci-dessus était inversé à l'origine ("PACK est mieux que T1-TXX", un choix de design
    initial de la fonctionnalité, jamais remis en cause depuis) : `has_pack` passait
    AVANT la taille, si bien qu'un simple mot "PACK" sans aucun chiffre associé battait
    systématiquement une plage de tomes pourtant vérifiée, même bien plus large. Constaté
    en réel sur 3 séries lors d'un test à blanc sur un échantillon de 20 séries : Nordheim
    (un pack "STC Team PACK" de 943 Mo sans plage battait un pack étiqueté "T01 à T41 +
    4HS" de 8,9 Go), Le cycle de Cyann (un "STC Team PACK" de 1,06 Go sans plage battait
    "T01 a T06+01HS" explicite) et Cubitus - Les nouvelles aventures (même schéma). Une
    plage confirmée est un fait vérifiable ; un simple mot-clé "PACK" sans le moindre
    chiffre ne l'est pas - même philosophie que le reste de l'app ("ne jamais deviner
    quand on peut vérifier", voir CLAUDE.md sur le matching Bédéthèque). `size_known`
    (booléen séparé du `size` numérique lui-même) est nécessaire pour ce tri : sans lui,
    un résultat SANS plage détectée (`size=0`) se classerait à tort SOUS un résultat avec
    une VRAIE plage de 1 seul tome (`size=1`, in fine exclu du tri par le filtre
    `size and size >= 2` plus bas de toute façon, mais gardé explicite par clarté).

    L'ordre de auto_acquire_sources (préférence de source) ne s'applique volontairement
    PAS ici, contrairement au mode tome par tome (voir run_auto_acquire_for_series). Un
    résultat qui n'a ni le mot PACK ni une plage d'au moins 2 tomes détectée n'est pas un
    pack, jamais retenu ici. None si aucun résultat n'est un pack - l'appelant retombe
    alors sur la recherche tome par tome habituelle plutôt que de rater toute la série
    faute de pack disponible.

    "PACK.5183.BANDES.DESSINEES.FRENCH.HYBRiD.eBOOK-DDD -> this is not matching the name
    Le chateau des animaux. so you should not download this one" - bug réel: cette
    fonction ne filtrait QUE par mot-clé PACK/plage de tomes, jamais par rapport avec la
    série recherchée elle-même. Un résultat Prowlarr n'a besoin que d'un score de
    pertinence non-nul pour arriver jusqu'ici (voir search_prowlarr_raw) - un mot commun
    du titre recherché ("des") apparaissant comme sous-chaîne d'un mot sans rapport du
    candidat ("bandes") suffisait à le faire passer ce filtre, avant même d'arriver ici.
    `title` (le titre de la série recherchée) est donc requis, et chaque candidat
    doit atteindre _PACK_TITLE_MATCH_THRESHOLD de similarité avec lui
    (BedethequeScraper._match_score, même fonction que _best_confident_result), sauf si
    _oneshot_title_contained confirme une séquence exacte d'au moins deux mots distinctifs.
    Un titre d'un seul mot noyé dans un titre plus long n'est volontairement plus une
    preuve automatique : même un pack probablement valable comme « Les Verron ... »
    passe en /validation si le bruit de release fait tomber son score sous 0.8. Ce faux
    négatif prudent est préférable au téléchargement d'une autre série."""
    from blueprints.library.routes import get_format_priority
    from blueprints.bedetheque.scraper import BedethequeScraper

    packs = []
    for r in results:
        if not _is_auto_download_eligible(r):
            continue
        item_title = r.get('filename') or r.get('title') or ''
        if (BedethequeScraper._match_score(title, item_title) < _PACK_TITLE_MATCH_THRESHOLD
                and not _oneshot_title_contained(item_title, title)):
            continue
        has_pack = _has_pack_keyword(item_title)
        size = _detect_pack_size(item_title)
        if not has_pack and not (size and size >= 2):
            continue
        # Détection par MOT-CLÉ (_detect_pack_format), pas par extension réelle - un nom
        # de release n'en a presque jamais une exploitable, voir sa docstring/le
        # commentaire en tête de module.
        fmt = _detect_pack_format(item_title)
        # get_format_priority: plus PETIT = meilleur (0=cbz/zip, 1=cbr/rar, 2=pdf) -
        # négation pour retomber sur le même sens "plus grand = meilleur" que les 3
        # autres critères du tuple, trié en un seul reverse=True ci-dessous.
        #
        # size_known avant size elle-même: voir docstring - une plage CONFIRMÉE doit
        # primer sur has_pack, mais deux résultats sans AUCUNE plage détectée (size=0
        # tous les deux) ne doivent pas se départager sur cette taille égale avant que
        # has_pack n'ait eu sa chance de trancher.
        packs.append((
            bool(size), size or 0, has_pack,
            not _is_black_and_white_release(item_title),
            -get_format_priority(fmt), _availability_count(r), r,
        ))

    if not packs:
        return None
    packs.sort(key=lambda t: t[:6], reverse=True)
    return packs[0][6]


def run_auto_acquire_for_series(app, series_id, title, missing_volumes, gate_on_global_setting=False, label=None, search_mode='series'):
    ""
    if not missing_volumes:
        with _running_series_lock:
            _last_auto_acquire_results[series_id] = {'running': False, 'completed': True, 'downloaded_count': 0, 'total': 0, 'summary': 'Aucun tome manquant.'}
        return

    with _running_series_lock:
        _last_auto_acquire_results[series_id] = {'running': True}
        if series_id in _running_series_ids:
            print(f"⏭️ Acquisition automatique déjà en cours pour {title} (#{series_id}), 2e déclenchement ignoré")
            return
        _running_series_ids.add(series_id)

    try:
        _run_auto_acquire_for_series_locked(app, series_id, title, missing_volumes, gate_on_global_setting, label, search_mode)
    finally:
        with _running_series_lock:
            _running_series_ids.discard(series_id)


def _run_auto_acquire_for_series_locked(app, series_id, title, missing_volumes, gate_on_global_setting, label=None, search_mode='series'):
    with app.app_context():
        from blueprints.library.routes import load_library_import_config, get_db_connection
        from blueprints.library.action_history import log_action
        from blueprints.missing_monitor.searcher import MissingVolumeSearcher

        config = load_library_import_config()
        sources = config.get('auto_acquire_sources') or []
        if not sources:
            with _running_series_lock:
                _last_auto_acquire_results[series_id] = {'running': False, 'completed': True, 'downloaded_count': 0, 'total': len(missing_volumes), 'summary': 'Aucune source active.'}
            return
        pack_search_enabled = config.get('auto_acquire_pack_search_enabled', False)

        # Quand la série est déjà reliée à un thread EBDZ, transmettre cette
        # correspondance au chercheur. Sans cette restriction, une recherche de
        # one-shot ou de tome peut trouver un résultat d'un autre thread partageant
        # un mot du titre (ex. « D'Artagnan » dans « Le Fou du Roy »).
        db_conn = get_db_connection()
        series_row = db_conn.execute(
            'SELECT ebdz_thread_id FROM series WHERE id = ?', (series_id,)
        ).fetchone()
        db_conn.close()
        ebdz_thread_id = series_row['ebdz_thread_id'] if series_row else None

        def _still_enabled():
            return not gate_on_global_setting or load_library_import_config().get('auto_acquire_on_add_enabled', False)

        searcher = MissingVolumeSearcher()

        print(f"🔎 Acquisition automatique: {title} - {len(missing_volumes)} tome(s) manquant(s) à chercher (sources: {sources})")

        downloaded_count = 0
        download_lines = []
        stopped_early = False
        remaining_volumes = missing_volumes

        # "il ne devrait y avoir qu'une seule recherche pour toute la série [...] so only
        # need to do 1 single search. there should not be any fallback. unless there is
        # one volume to search" - une recherche titre seul (volume_num=None, comme le
        # one-shot) remonte déjà TOUS les résultats correspondant à la série sur les 4
        # sources (chaque tome individuellement publié y apparaît comme un résultat à
        # part) - l'ancienne version relançait EN PLUS un search_for_volume(title, vol_num,
        # ...) complet PAR TOME MANQUANT (une série de 9 tomes = 9 recherches, chacune sur
        # 4 sources) alors que cette unique recherche contenait déjà tout ce qu'il fallait.
        # len > 1 seulement: une recherche ciblée sur UN tome précis (molette d'un volume,
        # voir runAutoAcquireNowForVolume côté library.js) ou un one-shot
        # (missing_volumes=[None]) reste une recherche directe pour CE tome (bloc `else`
        # plus bas), aucune raison de passer par une recherche série entière pour un seul
        # tome explicitement demandé.
        # Mode série : une recherche globale sur le titre, puis sélection des fichiers
        # correspondant aux tomes manquants. Le mode volume (branche else) lance au
        # contraire exactement la recherche ciblée affichée par l'interface manuelle.
        if search_mode == 'series' and len(missing_volumes) > 1 and _still_enabled():
            try:
                results = searcher.search_for_volume(
                    title, None, sources=sources, thread_id=ebdz_thread_id,
                    source_order=sources
                )
            except Exception as e:
                print(f"Erreur recherche série auto-acquire {title}: {e}")
                results = []

            # La disponibilité EBDZ affichée par le frontend était auparavant calculée
            # seulement après rendu du tableau manuel. L'automatisation doit effectuer la
            # même vérification AVANT tout classement. Les zéros confirmés sont
            # retirés ; les EBDZ inconnus restent présents pour aller dans /validation,
            # mais _is_auto_download_eligible interdit leur téléchargement automatique.
            results = _enrich_ed2k_availability(results)

            if pack_search_enabled:
                best_pack = _best_pack_result(results, title)
                if best_pack:
                    # Un pack repéré au mot-clé "PACK" seul (pas de plage détectée, ex.
                    # "Videur.BD.HD.PACK...") n'a pas de compte de tomes exploitable - "N
                    # tomes" avec N=None serait affiché tel quel dans les logs/l'Historique.
                    pack_size = _detect_pack_size(best_pack.get('filename') or best_pack.get('title') or '')
                    pack_label = f"Pack ({pack_size} tomes)" if pack_size else "Pack"
                    try:
                        success, msg = _download_result(app, best_pack, series_id, title, None)
                        print(f"  {pack_label}: {msg}")
                        if success:
                            downloaded_count += 1
                            filename = best_pack.get('filename') or best_pack.get('title') or '?'
                            source_label = _SOURCE_LABELS.get(best_pack.get('source'), best_pack.get('source') or '?')
                            download_lines.append(f"{pack_label} : {filename} ({source_label})")
                            remaining_volumes = []
                    except Exception as e:
                        print(f"Erreur téléchargement pack auto-acquire {title}: {e}")
                # Les packs incertains doivent rester visibles dans /validation même
                # lorsqu'un AUTRE pack suffisamment fiable a été sélectionné. L'ancienne
                # condition `if not best_pack` les faisait disparaître dès qu'un bon
                # candidat coexistait dans les résultats. Ne retenir ici que les vrais
                # candidats pack/plage qui échouent individuellement au seuil automatique ;
                # un autre pack valide mais simplement moins bien classé n'est pas ambigu.
                rejected_packs = []
                for result in results:
                    item_title = result.get('filename') or result.get('title') or ''
                    is_pack_candidate = _has_pack_keyword(item_title)                         or ((_detect_pack_size(item_title) or 0) >= 2)
                    if is_pack_candidate and result is not best_pack                             and _best_pack_result([result], title) is None:
                        rejected_packs.append(result)
                if rejected_packs:
                    queue_manual_review(
                        series_id, title, None, 'Pack à confirmer', rejected_packs,
                        'Pack trouvé, mais identité de la série insuffisamment fiable.',
                        force_candidates=True,
                    )
                    # Un pack déjà mis en revue ne doit pas être répété ensuite comme
                    # candidat de chaque Tome N manquant. Conserver les autres résultats
                    # individuels pour le repli tome par tome.
                    results = [
                        result for result in results
                        if all(result is not rejected for rejected in rejected_packs)
                    ]

                # Aucun pack trouvé: remaining_volumes reste = missing_volumes, chaque
                # tome est résolu ci-dessous DANS ces mêmes résultats déjà récupérés.

            for vol_num in remaining_volumes:
                if not _still_enabled():
                    stopped_early = True
                    break
                candidates = [
                    r for r in results
                    if searcher._confirms_requested_volume(r.get('filename') or r.get('title') or '', vol_num, None)[0]
                ]
                # Le premier résultat n'est pas une preuve d'identité. L'automatisation
                # applique un score de titre renforcé; les cas ambigus sont mis en revue.
                best = _best_confident_result(candidates, vol_num, title, source_order=sources) if candidates else None

                if best:
                    try:
                        success, msg = _download_result(app, best, series_id, title, vol_num)
                        print(f"  Tome {vol_num}: {msg}")
                        if success:
                            downloaded_count += 1
                            filename = _decode_display_filename(best.get('filename') or best.get('title') or '?')
                            source_label = _SOURCE_LABELS.get(best.get('source'), best.get('source') or '?')
                            vol_label = f"Tome {vol_num}" if vol_num is not None else "Album"
                            download_lines.append(f"{vol_label} : {filename} ({source_label})")
                    except Exception as e:
                        print(f"Erreur téléchargement auto-acquire {title} vol {vol_num}: {e}")
                elif results:
                    # Des résultats existent (pour la série) mais aucun assez confiant
                    # pour être LE tome demandé - ne jamais télécharger à l'aveugle (voir
                    # docstring _confirms_requested_volume). La vérification reste
                    # volontairement manuelle et visible dans la file de validation.
                    queue_manual_review(series_id, title, vol_num, f'Tome {vol_num}', candidates or results,
                                        'Aucun résultat n’atteint le niveau de confiance requis pour un téléchargement automatique.',
                                        force_candidates=True)
                # Plus de délai ici: aucune requête réseau supplémentaire par tome
                # (tout vient de `results`, déjà récupéré une seule fois ci-dessus).
        else:
            # Un seul tome manquant (ou recherche ciblée molette/one-shot) - recherche
            # directe pour CE tome précis. label transmis à search_for_volume (voir sa
            # docstring: restreint _confirms_requested_volume à vérifier le bon TYPE de
            # tome, pas seulement le numéro - "Recherche automatique" pour une intégrale/
            # HS/épisode précis).
            for vol_num in missing_volumes:
                if not _still_enabled():
                    stopped_early = True
                    break
                try:
                    results = searcher.search_for_volume(
                        title, vol_num, sources=sources, thread_id=ebdz_thread_id,
                        source_order=sources, label=label
                    )
                except Exception as e:
                    print(f"Erreur recherche auto-acquire {title} vol {vol_num}: {e}")
                    continue

                results = _enrich_ed2k_availability(results)

                # Même recherche que l'interface manuelle, mais l'automatisation exige
                # un score de confiance renforcé avant d'envoyer un fichier.
                best = _best_confident_result(results, vol_num, title, source_order=sources) if results else None

                if best:
                    try:
                        success, msg = _download_result(app, best, series_id, title, vol_num)
                        print(f"  Tome {vol_num}: {msg}")
                        if success:
                            downloaded_count += 1
                            filename = _decode_display_filename(best.get('filename') or best.get('title') or '?')
                            source_label = _SOURCE_LABELS.get(best.get('source'), best.get('source') or '?')
                            vol_label = label or (f"Tome {vol_num}" if vol_num is not None else "Album")
                            download_lines.append(f"{vol_label} : {filename} ({source_label})")
                    except Exception as e:
                        print(f"Erreur téléchargement auto-acquire {title} vol {vol_num}: {e}")
                elif results:
                    # Résultats présents mais non confirmés : aucun téléchargement
                    # automatique et aucune notification Telegram ; vérification manuelle
                    # possible depuis la file de validation manuelle.
                    queue_manual_review(series_id, title, vol_num, label or (f'Tome {vol_num}' if vol_num is not None else 'Album'), results,
                                        'Résultats trouvés, mais aucun candidat suffisamment fiable pour le téléchargement automatique.',
                                        force_candidates=True)

        téléchargement_mot = "téléchargement envoyé" if downloaded_count == 1 else "téléchargements envoyés"
        tome_mot = "tome recherché" if len(missing_volumes) == 1 else "tomes recherchés"
        summary = f"Recherche terminée : {downloaded_count} {téléchargement_mot} sur {len(missing_volumes)} {tome_mot}."
        if stopped_early:
            summary += " Arrêtée en cours de route (réglage « Téléchargement automatique à l'ajout » désactivé entre-temps)."
        detail = summary + ("\n" + "\n".join(download_lines) if download_lines else "")
        with _running_series_lock:
            _last_auto_acquire_results[series_id] = {
                'running': False, 'completed': True, 'downloaded_count': downloaded_count,
                'total': len(missing_volumes), 'summary': summary, 'detail': detail
            }
        print(f"✓ Acquisition automatique terminée: {title} - {summary}")
        try:
            log_action('auto_acquire', series_id, title, detail, success=True)
        except Exception as e:
            print(f"Erreur journalisation auto-acquire ({title}): {e}")
