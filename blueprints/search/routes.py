"""
Routes pour la recherche de liens ED2K
"""
from flask import render_template, request, jsonify, current_app
from . import search_bp
import sqlite3
import re
import unicodedata
from urllib.parse import unquote


def get_reordered_query_variants(query):
    """
    Les titres EBDZ stockent l'article en fin de titre pour l'alphabétisation
    (ex: "L'Âge d'eau" est stocké "Âge d'eau, L'" ou "Âge d'eau (L')").
    Renvoie les variantes réordonnées d'une requête commençant par un article français,
    ou une liste vide si la requête ne commence pas par un article connu.
    """
    match = re.match(r"^(L'|D'|Le\s+|La\s+|Les\s+|Du\s+|Des\s+|Un\s+|Une\s+)(.+)$", query, re.IGNORECASE)
    if not match:
        return []

    article = match.group(1).strip()
    rest = match.group(2).strip()
    if not rest:
        return []

    return [f'{rest}, {article}', f'{rest} ({article})']


def get_suffix_article_variants(title):
    """
    Certains titres locaux stockent déjà l'article en fin de titre entre parenthèses
    (ex: "Âge D'Eau (L')"), mais EBDZ peut stocker la même série sous une forme
    différente ("Âge d'eau, L'" avec une virgule, ou "L'Âge d'eau" en préfixe).
    Renvoie les variantes possibles pour un titre se terminant par "(Article)",
    ou une liste vide si le titre ne se termine pas par un article connu.
    """
    match = re.match(r"^(.+?)\s*\((Le|La|L'|Les|Un|Une|Du|Des)\)$", title.strip(), re.IGNORECASE)
    if not match:
        return []

    rest = match.group(1).strip()
    article = match.group(2)
    if not rest:
        return []

    prefix_form = f"{article}{rest}" if article.lower() == "l'" else f"{article} {rest}"

    # Espacement normalisé avant la parenthèse ("Chats en BD(Les)" sans espace, tel que
    # renvoyé par certaines listes de candidats Bédéthèque, vs "Chats en BD (Les)" avec
    # espace, convention des noms de fichiers EBDZ/Telegram) - normalize_search_text ne
    # touche pas à l'espacement autour de la ponctuation, donc sans cette variante une
    # requête sans espace ne matche jamais un nom de fichier qui en a un, même une fois
    # les deux normalisés.
    return [prefix_form, f'{rest}, {article}', f'{rest} ({article})']


def ebdz_core_title(thread_title):
    """
    Retire le(s) suffixe(s) entre parenthèses/crochets qu'EBDZ ajoute en fin de titre
    de thread (auteur(s), article déplacé pour le tri...), ex: "Echecs [Victor L. Pinel]"
    -> "Echecs", ou "Années rouge & noir (Les) [Boisserie Convard]" -> "Années rouge & noir"
    (deux suffixes à la suite). Utilisé pour distinguer un thread qui correspond
    réellement au titre recherché d'un thread qui ne matche que par un mot du titre
    apparaissant ailleurs (nom de fichier, sous-titre d'un tome...).
    """
    text = (thread_title or '').strip()
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]\s*$', '', text).strip()
    return text


def ebdz_title_variants(title):
    """Toutes les variantes de recherche EBDZ pour un titre de série - point d'entrée
    UNIQUE pour cette logique. "check that the search is the same for every function
    that do a search in the app": avant cette fonction, /api/search (plus bas dans ce
    fichier), search_ebdz_threads (ci-dessus) et le matching EBDZ automatique
    (_ebdz_enrich_series/_bulk_ebdz_autodetect, blueprints/library/routes.py) avaient
    chacun leur propre sous-ensemble de variantes, et un titre trouvé par l'un restait
    introuvable par l'autre - "Le grand vide (Murawiec) could not match ebdz only if i
    ask about grand vide without le": le matching automatique n'utilisait que
    get_suffix_article_variants (article en FIN de titre, "Titre (Le)") et jamais
    get_reordered_query_variants (article en TÊTE de titre, "Le Titre" -> "Titre, Le" /
    "Titre (Le)") - un article en tête ne générait donc aucune variante réordonnée là où
    /api/search savait déjà le faire.

    Essaie aussi ces deux transformations sur le "cœur" du titre (ebdz_core_title,
    suffixe parenthèses/crochets retiré: auteur collé au nom du dossier par erreur, ex:
    "Virus (RicardRica)") en plus du titre complet - un article en tête du titre local
    ne serait sinon jamais réordonné une fois ce suffixe retiré.
    """
    core = ebdz_core_title(title)
    bases = [title] if core == title else [title, core]
    variants = list(bases)
    for base in bases:
        variants.extend(get_reordered_query_variants(base))
        variants.extend(get_suffix_article_variants(base))
    return list(dict.fromkeys(v for v in variants if v))


def _search_words(query):
    """Découpe une requête en mots normalisés pour le ET logique mot-à-mot (voir
    _word_and_match_ids ci-dessous) - mots de 1 caractère ignorés (restes d'article après
    élision de l'apostrophe, sans aucun pouvoir filtrant, voir normalize_search_text)."""
    return [w for w in normalize_search_text(query).split() if len(w) >= 2]


def _word_and_match_ids(cursor, columns, words, table, fts_table):
    """Point d'entrée UNIQUE pour "chercher tous les mots du titre, dans n'importe quel
    ordre, avec n'importe quoi entre eux" (voir search_telegram_files_local,
    blueprints/telegram_channels/routes.py, où ce principe a été introduit en premier) -
    partagé ici par search_ebdz_threads et /api/search (search_ed2k) plutôt que dupliqué
    une seconde fois (voir ebdz_title_variants' docstring pour l'historique de ce genre de
    duplication ayant déjà mordu ce fichier une fois).

    "pourquoi tu ne fais pas simplement cherche tous les mots qui sont dans le titre de
    l'album... je ne sais pas ce que tu fais avec un parsing complique du L, etc." -
    remplace l'ancienne approche (ebdz_title_variants: quelques variantes de la PHRASE
    ENTIÈRE réordonnée, exigée contiguë dans le texte cible) par un ET logique mot à mot:
    chaque mot doit apparaître QUELQUE PART dans une des colonnes de `columns` (n'importe
    laquelle, n'importe où) - couvre nativement le réordonnancement d'article ET les mots
    intercalés (numéro de tome, doublon de titre...) qui cassaient la correspondance de
    phrase contiguë, sans la moindre variante à générer.

    columns: colonnes normalisées à interroger (ex: ['thread_title_normalized'] pour ne
    chercher QUE dans le titre, ou les deux ensemble pour chercher dans l'un OU l'autre).
    `table`/`fts_table` (table brute / table FTS5 trigramme correspondante) et un nom de
    colonne id différent selon l'appelant (id vs rowid) - pas de valeur par défaut, pour
    forcer chaque appelant à les préciser plutôt que de deviner silencieusement.

    Retourne un ET logique sur des mots (>=3 caractères: recherche indexée FTS5
    trigramme; <3: pas de trigramme possible, revérifié via un post-filtre Python sur les
    colonnes déjà normalisées à l'écriture, donc sans coût de scan supplémentaire).
    """
    if not words:
        return []

    id_column = 'rowid' if table == fts_table else 'id'
    select_cols = ', '.join(columns)
    fts_words = [w for w in words if len(w) >= 3]

    if fts_words:
        col_group = '{' + ' '.join(columns) + '}' if len(columns) > 1 else columns[0]
        fts_query = col_group + ': ' + ' AND '.join(
            '"' + w.replace('"', '""') + '"' for w in fts_words
        )
        cursor.execute(
            f'SELECT rowid, {select_cols} FROM {fts_table} WHERE {fts_table} MATCH ?',
            (fts_query,)
        )
    else:
        # Repli sans trigramme possible (uniquement des mots de 1-2 caractères, rare):
        # chaque mot doit matcher au moins une des colonnes (OR), tous les mots requis
        # (AND) - toujours sur les colonnes déjà normalisées à l'écriture.
        where = ' AND '.join(
            '(' + ' OR '.join(f'{c} LIKE ?' for c in columns) + ')' for _ in words
        )
        like_params = [f'%{w}%' for w in words for _ in columns]
        cursor.execute(
            f'SELECT {id_column}, {select_cols} FROM {table} WHERE {where}',
            like_params
        )

    matched_ids = []
    for row in cursor.fetchall():
        row_id, texts = row[0], row[1:]
        combined = ' '.join(t for t in texts if t)
        if all(w in combined for w in words):
            matched_ids.append(row_id)
    return matched_ids


def normalize_search_text(text):
    """
    Normalise un texte pour une comparaison insensible aux accents, à la casse
    et à la ponctuation (apostrophes droites/courbes, virgules, points...).
    """
    if not text:
        return ''
    try:
        text = unquote(text)
    except Exception:
        pass
    # Les ligatures œ/æ ne sont pas décomposées par NFKD (ce ne sont pas des lettres
    # accentuées mais des caractères à part entière): sans ce remplacement explicite,
    # "cœur" et "coeur" ne seraient jamais reconnus comme équivalents
    delig_text = text.replace('œ', 'oe').replace('Œ', 'OE').replace('æ', 'ae').replace('Æ', 'AE')
    without_accents = ''.join(
        c for c in unicodedata.normalize('NFKD', delig_text) if not unicodedata.combining(c)
    )
    lowered = without_accents.lower()
    without_punctuation = re.sub(r"[',.\"`’‘_]", ' ', lowered)
    return re.sub(r'\s+', ' ', without_punctuation).strip()


def search_ebdz_threads(query, limit=40):
    """Recherche des threads EBDZ correspondant à un titre, regroupés par thread (un
    thread = une série/release), insensible aux accents/casse/ponctuation.
    Réutilisé par le matching manuel d'une série existante et par la page Découvrir
    (recherche d'une série qui n'est pas encore dans la bibliothèque).

    "pourquoi matcher manuellement sur ebdz c'est si long. ca devrait etre instantané" -
    même bug déjà corrigé plus bas dans ce fichier pour /api/search ("do the same as ebdz.
    this is fast. why?", voir son commentaire) mais jamais reporté ici : appelait
    search_normalize() (UDF Python) par LIGNE de la table (~84k) au lieu des colonnes déjà
    normalisées à l'écriture (thread_title_normalized/filename_normalized, voir save_to_db
    côté ebdz/scraper.py) + l'index FTS5 trigramme (ed2k_links_fts) - mesuré ~1s pour UN
    SEUL terme LIKE, plusieurs secondes en pratique une fois les variantes de titre OR-ées.
    Repli LIKE direct sur ces mêmes colonnes précalculées (toujours sans appel Python par
    ligne) pour un terme trop court pour le trigramme FTS5 (<3 caractères)."""
    ebdz_conn = sqlite3.connect(current_app.config['DB_FILE'], timeout=30.0)
    ebdz_cursor = ebdz_conn.cursor()

    ebdz_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
    if ebdz_cursor.fetchone() is None:
        ebdz_conn.close()
        raise LookupError('Base EBDZ non disponible')

    # _word_and_match_ids: même point d'entrée unique que /api/search (voir sa docstring)
    # - un mot par mot du titre recherché, dans n'importe quel ordre.
    words = _search_words(query)

    def _matching_ids(column):
        return _word_and_match_ids(ebdz_cursor, [column], words, 'ed2k_links', 'ed2k_links_fts')

    # En priorité: threads dont le TITRE correspond réellement à la recherche. Une
    # correspondance sur le nom de fichier seul est bien plus bruitée qu'on ne s'y
    # attend: beaucoup de séries appartiennent à une collection dont le nom (ex: "La
    # sagesse des mythes") est répété dans CHAQUE nom de fichier de la collection, sans
    # rapport avec le titre réel de la série - chercher un mot de ce nom de collection
    # remonterait alors des dizaines de séries sans rapport. On ne retombe sur une
    # correspondance "nom de fichier uniquement" que si AUCUN titre ne correspond, pas
    # simplement pour compléter la liste jusqu'à la limite (sinon le bruit se contente
    # de s'ajouter en dessous des vrais résultats plutôt que de disparaître)
    matched_ids = _matching_ids('thread_title_normalized')
    if not matched_ids:
        matched_ids = _matching_ids('filename_normalized')

    if not matched_ids:
        ebdz_conn.close()
        return []

    placeholders = ','.join('?' * len(matched_ids))
    ebdz_cursor.execute(f'''
        SELECT thread_id, thread_title, thread_url, forum_category,
               COUNT(*) AS file_count, GROUP_CONCAT(DISTINCT volume)
        FROM ed2k_links
        WHERE id IN ({placeholders})
        GROUP BY thread_id
        ORDER BY thread_title
        LIMIT ?
    ''', matched_ids + [limit])
    rows = ebdz_cursor.fetchall()

    ebdz_conn.close()

    candidates = []
    for row in rows:
        volumes = sorted({int(v) for v in (row[5] or '').split(',') if v.strip().lstrip('-').isdigit()})
        candidates.append({
            'thread_id': row[0],
            'thread_title': row[1],
            'thread_url': row[2],
            'forum_category': row[3],
            'file_count': row[4],
            'volumes': volumes
        })
    return candidates


def get_db_connection():
    """Retourne une connexion à la base ED2K"""
    conn = sqlite3.connect(current_app.config['DB_FILE'], timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


@search_bp.route('/discover')
def discover_page():
    """Page de découverte et ajout de séries"""
    return render_template('discover.html')


@search_bp.route('/ebdz-nouveautes')
def ebdz_latest_page():
    """Page listant les nouveaux épisodes/volumes du dernier scrape EBDZ"""
    return render_template('ebdz-latest.html')


@search_bp.route('/api/search')
def search_ed2k():
    """Recherche de liens ED2K et Prowlarr"""
    query = request.args.get('query', '').strip()
    volume = request.args.get('volume', '').strip()
    category = request.args.get('category', '').strip()
    thread_id = request.args.get('thread_id', '').strip()

    try:
        all_results = []

        # ===== RECHERCHE ED2K =====
        try:
            conn = sqlite3.connect(current_app.config['DB_FILE'], timeout=30.0)
            cursor = conn.cursor()

            # Vérifier si la table ed2k_links existe
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_links'")
            if cursor.fetchone() is not None:
                sql = '''
                    SELECT thread_id, thread_title, thread_url, forum_category, cover_image,
                           link, filename, filesize, volume, description
                    FROM ed2k_links
                    WHERE 1=1
                '''
                params = []
                # LIMIT seulement pour la recherche libre (query) plus bas - un thread_id
                # cible déjà un seul thread (peu de lignes), le tronquer n'aurait aucun sens.
                limit_clause = ''
                # Mis à True quand la recherche FTS5 (voir plus bas) ne remonte aucun id -
                # évite d'exécuter la requête finale avec un "AND id IN ()" (SQL invalide).
                no_match = False

                if thread_id:
                    # Série déjà matchée à un thread EBDZ précis: on cible ce thread
                    # directement plutôt que de deviner par titre (plus fiable, évite
                    # les faux positifs d'une recherche floue sur un titre ambigu)
                    sql += ' AND thread_id = ?'
                    params.append(thread_id)
                elif query:
                    words = _search_words(query)
                    matched_ids = _word_and_match_ids(
                        cursor, ['thread_title_normalized', 'filename_normalized'],
                        words, 'ed2k_links', 'ed2k_links_fts'
                    )
                    if not matched_ids:
                        no_match = True
                    else:
                        sql += ' AND id IN (' + ','.join('?' * len(matched_ids)) + ')'
                        params.extend(matched_ids)

                    limit_clause = ' LIMIT 300'

                if volume:
                    sql += ' AND volume = ?'
                    params.append(int(volume))

                if category:
                    sql += ' AND forum_category = ?'
                    params.append(category)

                sql += ' ORDER BY thread_id, volume' + limit_clause

                if no_match:
                    results = []
                else:
                    cursor.execute(sql, params)
                    results = cursor.fetchall()

                from blueprints.library.scanner import LibraryScanner
                from blueprints.missing_monitor.searcher import MissingVolumeSearcher

                for row in results:
                    filename = row[6]
                    # Métadonnées parsées depuis le nom de fichier (résolution, année,
                    # auteur, tags #INT/#HS...): les fichiers EBDZ ne numérotent pas
                    # toujours le volume en base (colonne `volume`), ce parsing sert de
                    # complément pour un affichage lisible (ex: tableau de résultats).
                    # Le nom brut est encore URL-encodé (ed2k): on décode une copie pour
                    # le parsing sans toucher au champ `filename` renvoyé (le front-end le
                    # décode lui-même via decodeFilename())
                    try:
                        decoded_filename = unquote(filename)
                    except Exception:
                        decoded_filename = filename
                    parsed = LibraryScanner.parse_filename(decoded_filename)
                    # "les fichiers sont les mêmes... pas besoin de différencier les
                    # parsers. unify tout" - un seul appel, strictement identique à
                    # Prowlarr (`prowlarr/routes.py`) et Telegram (`telegram_channels/
                    # routes.py`): aucune logique spécifique à EBDZ ici (le repli sur la
                    # colonne `volume` en base a été retiré - cette colonne est de toute
                    # façon renseignée par ce même parser dès l'ingestion, voir
                    # MyBBScraper.parse_volume_info, donc le repli ne faisait plus que
                    # dupliquer un cas déjà couvert par un vrai bug de parsing à corriger
                    # directement dans LibraryScanner.parse_filename le cas échéant,
                    # jamais contourner ici pour une seule des 3 sources).
                    parsed_volume_label = MissingVolumeSearcher._parsed_volume_label(decoded_filename)
                    all_results.append({
                        'source': 'ebdz',  # Identifier la source
                        'thread_id': row[0],
                        'thread_title': row[1],
                        'thread_url': row[2],
                        'forum_category': row[3],
                        'cover_image': row[4],
                        'link': row[5],
                        'filename': filename,
                        'filesize': row[7],
                        'volume': row[8],
                        'description': row[9],
                        'parsed_volume': parsed_volume_label,
                        'resolution': parsed['resolution'],
                        'author': parsed['author'],
                        'year': parsed['year'],
                        'format': parsed['format'],
                        'is_integral': parsed['is_integral'],
                        'integral_number': parsed['integral_number'],
                        'is_hs': parsed['is_hs'],
                        'hs_number': parsed['hs_number']
                    })

            conn.close()
        except Exception as e:
            print(f"Erreur recherche ED2K: {str(e)}")

        return jsonify({'results': all_results})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@search_bp.route('/api/search/prowlarr')
def search_prowlarr_api():
    """API pour rechercher dans Prowlarr (pour la page discover) - coeur de recherche
    partagé avec la page /search et le monitoring de volumes manquants, voir
    blueprints/prowlarr/search.py::search_prowlarr_raw (avant ce partage, cette copie
    n'incluait pas parsed_volume: la colonne Volume de /discover restait vide pour tout
    résultat Prowlarr alors que /search l'affichait correctement)."""
    from blueprints.prowlarr.search import search_prowlarr_raw

    query = request.args.get('q', '').strip()
    volume = request.args.get('volume', '').strip()

    if not query:
        return jsonify({
            'success': False,
            'error': 'Paramètre q requis'
        }), 400

    try:
        results = search_prowlarr_raw(query, volume_num=volume or None)
        return jsonify({
            'success': True,
            'results': results or []
        })

    except Exception as e:
        print(f"Erreur recherche Prowlarr: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
